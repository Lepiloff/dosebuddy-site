"""Host nginx + actual uvicorn runtime, synthetic fault; never deploys services."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import httpx
import pytest

from tests.conftest import TEST_KEY

ROOT = Path(__file__).resolve().parents[2]
MARKER = "private-cursor-auth-body-marker"


def port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def headers(response):
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache" and response.headers["vary"] == "Authorization"
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "etag" not in response.headers and "last-modified" not in response.headers


async def ready(url, process):
    async with httpx.AsyncClient() as client:
        for _ in range(200):
            assert process.poll() is None
            try:
                await client.get(url)
                return
            except httpx.TransportError:
                await asyncio.sleep(.025)
    raise AssertionError("local test service not ready")


@pytest.mark.asyncio
async def test_real_proxy_429_502_private_headers_and_uvicorn_query_error_log_suppression(tmp_path):
    nginx = shutil.which("nginx")
    if nginx is None:
        pytest.skip("host nginx unavailable; local proxy runtime gate not run")
    upstream_port, proxy_port, plain_port = port(), port(), port()
    # Same factory/middleware and real Uvicorn CLI as the image, with one
    # synthetic handler fault and caller override. No PostgreSQL/Redis calls.
    (tmp_path/"fault_app.py").write_text('''import uuid
from app.main import create_app
from app.api.deps import sync_caller, Caller
from app.api import caregiver_events
from app.db.models import Account
from tests.conftest import make_settings

def factory():
    app = create_app(make_settings())
    app.dependency_overrides[sync_caller] = lambda: Caller(Account(id=uuid.uuid4()), uuid.uuid4())
    async def fail(*args):
        raise RuntimeError("private-cursor-auth-body-marker")
    caregiver_events.lock_caller = fail
    return app
''')
    env = {**os.environ, "ENCRYPTION_KEY": TEST_KEY, "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(ROOT/"api")}
    docker = (ROOT/"api/Dockerfile").read_text()
    assert '"--no-access-log"' in docker and 'tools/caregiver_recovery.py' in docker
    log = (tmp_path/"uvicorn.log").open("w")
    upstream = subprocess.Popen([sys.executable, "-m", "uvicorn", "fault_app:factory", "--factory",
        "--app-dir", str(tmp_path), "--host", "127.0.0.1", "--port", str(upstream_port),
        "--no-access-log"], env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT/"api")
    proxy = None
    try:
        await ready(f"http://127.0.0.1:{upstream_port}/health", upstream)
        snippets = tmp_path/"snippets"
        snippets.mkdir()
        for name in ("api-proxy.conf", "api-upstream.conf", "caregiver-privacy.conf"):
            source = (ROOT/"deploy/nginx/snippets"/name).read_text()
            # Only local include paths/upstream address differ from production.
            source = source.replace("/etc/nginx/snippets/", str(snippets)+"/")
            source = source.replace("api:8000", f"127.0.0.1:{upstream_port}")
            (snippets/name).write_text(source)
        plain = (ROOT/"deploy/nginx/conf.d/20-api.conf").read_text().split("\n}\n\nserver", 1)[0] + "\n}"
        plain = plain[plain.index("server {"):].replace("listen 80;", f"listen 127.0.0.1:{plain_port};")
        plain = plain.replace("/etc/nginx/snippets/", str(snippets)+"/")
        config = tmp_path/"nginx.conf"
        config.write_text(f'''pid {tmp_path}/nginx.pid;
error_log {tmp_path}/error.log notice;
events {{ worker_connections 256; }}
http {{
 access_log {tmp_path}/access.log combined;
 limit_req_zone $binary_remote_addr zone=site_limit:1m rate=10r/s;
 limit_req_zone $binary_remote_addr zone=sync_limit:1m rate=30r/s;
 limit_conn_zone $binary_remote_addr zone=site_conn:1m;
 limit_req_status 429;
 limit_req_log_level warn;
 server {{ listen 127.0.0.1:{proxy_port}; include {snippets}/api-proxy.conf; }}
 {plain}
}}
''')
        checked = subprocess.run([nginx, "-t", "-p", str(tmp_path)+"/", "-c", str(config)], capture_output=True)
        assert checked.returncode == 0, checked.stderr.decode()
        proxy = subprocess.Popen([nginx, "-p", str(tmp_path)+"/", "-c", str(config), "-g", "daemon off;"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await ready(f"http://127.0.0.1:{proxy_port}/health", proxy)
        url = f"http://127.0.0.1:{proxy_port}/v1/caregiver-events?cursor={MARKER}"
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=10)) as client:
            failure = await client.request("GET", url, headers={"Authorization": "Bearer "+MARKER}, content=MARKER)
            assert failure.status_code == 500 and failure.json()["error"]["code"] == "internal_error"
            headers(failure)
            replies = await asyncio.gather(*(client.get(url, headers={"Authorization": "Bearer "+MARKER}) for _ in range(50)))
            throttled = [response for response in replies if response.status_code == 429]
            assert throttled
            for response in throttled:
                headers(response)
                assert response.headers["retry-after"] == "5"
                assert response.json()["error"]["code"] == "rate_limited"
            plaintext = await client.get(f"http://127.0.0.1:{plain_port}/v1/caregiver-events?cursor={MARKER}")
            assert plaintext.status_code == 404 and "location" not in plaintext.headers
            headers(plaintext)
            upstream.terminate()
            await asyncio.to_thread(upstream.wait, 5)
            await asyncio.sleep(3)  # drain the existing default request bucket
            unavailable = await client.get(url)
            assert unavailable.status_code == 502
            headers(unavailable)
        log.flush()
        for name in ("uvicorn.log", "access.log", "error.log"):
            assert MARKER not in (tmp_path/name).read_text(), name
        # Retain concise runtime evidence on stdout/JUnit; temp-prefix services
        # and all their logs are owned by this fixture and stopped below.
        print(json.dumps({"nginx": "native host", "uvicorn": "host venv, not lock-identical",
                          "private_statuses": [404, 429, 500, 502], "throttled": len(throttled), "log_privacy": "PASS"}))
    finally:
        if proxy is not None:
            proxy.terminate()
            await asyncio.to_thread(proxy.wait, 5)
        if upstream.poll() is None:
            upstream.terminate()
            await asyncio.to_thread(upstream.wait, 5)
        log.close()
