"""Real Google verification against a delayed, synthetic certificate server.

Only the certificate URL is replaced. The Google requests adapter, HTTP socket,
signature validation and both production HTTP handlers remain in the path.
"""

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.oauth2 import id_token as google_id_token
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import mint_access_token
from app.db.models import Account, Device
from app.main import create_app
from app.services.google import RealGoogleVerifier
from tests.conftest import FakeRedis, make_settings

CLIENT_ID = "synthetic-client.apps.googleusercontent.com"


@pytest.fixture
def certificates(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def certificate_for(key):
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-google")])
        now = datetime.now(timezone.utc)
        return (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256())
            .public_bytes(serialization.Encoding.PEM).decode()
        )

    class Server:
        calls = 0
        delay = 0
        cache_control = "no-store"
        age = "0"
        status = 200
        arrived = threading.Event()
        release = None
        certs = {"synthetic-key": certificate_for(private_key)}
        signing_key = private_key
        key_id = "synthetic-key"

        def rotate(self):
            self.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            self.key_id = "rotated-key"
            self.certs = {self.key_id: certificate_for(self.signing_key)}

        def token(self, *, omit=(), **claims):
            defaults = {
                "sub": "synthetic-subject", "aud": CLIENT_ID,
                "iss": "https://accounts.google.com",
                "iat": int(time.time()) - 10, "exp": int(time.time()) + 3600,
            }
            payload = defaults | claims
            for key in omit:
                payload.pop(key, None)
            return jwt.encode(
                payload, self.signing_key, algorithm="RS256",
                headers={"kid": self.key_id},
            )

    state = Server()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.calls += 1
            state.arrived.set()
            time.sleep(state.delay)
            if state.release is not None:
                state.release.wait(2)
            data = json.dumps(state.certs).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", state.cache_control)
            self.send_header("Age", state.age)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The caller's real transport timeout closed the socket.

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        google_id_token, "_GOOGLE_OAUTH2_CERTS_URL",
        f"http://127.0.0.1:{server.server_port}/certs",
    )
    yield state
    if state.release is not None:
        state.release.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest_asyncio.fixture
async def real_api(db_engine):
    app = create_app(make_settings(google_client_id=CLIENT_ID))
    async with app.router.lifespan_context(app):
        app.state.sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
        app.state.redis = FakeRedis()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            client.app = app
            yield client


async def unrelated_session(api):
    """A synthetic account using the real bearer/sync authorization path."""
    async with api.app.state.sessionmaker() as session:
        account = Account(google_sub="unrelated-synthetic-subject")
        session.add(account)
        await session.flush()
        device = Device(id=uuid.uuid4(), account_id=account.id, platform="android")
        session.add(device)
        await session.commit()
        access, _ = mint_access_token(
            api.app.state.settings.jwt_secret, account.id, device.id,
            device.credential_generation,
        )
        return {"Authorization": f"Bearer {access}"}


def google_request(endpoint, token):
    if endpoint == "/v1/auth/google":
        return {"id_token": token, "device": {"id": str(uuid.uuid4()), "platform": "android"}}
    return {"id_token": token, "account_id": str(uuid.uuid4())}


@pytest.mark.parametrize("endpoint", ["/v1/auth/google", "/v1/account/delete-retry"])
async def test_slow_certificates_leave_heartbeat_health_and_sync_responsive(
    real_api, certificates, endpoint,
):
    headers = await unrelated_session(real_api)
    certificates.delay = 0.3
    certificates.status = 503
    ticks = []

    async def heartbeat():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    async def probes():
        # Include dispatch delay: on the old code this coroutine cannot even
        # wake until all three synchronous certificate requests have finished.
        started = time.monotonic()
        await asyncio.sleep(0.02)
        assert (await real_api.get("/health")).status_code == 200
        assert (await real_api.get("/health/ready")).status_code == 200
        assert (await real_api.get("/v1/sync/pull", headers=headers)).status_code == 200
        assert (await real_api.post("/v1/sync/push", headers=headers, json={})).status_code == 200
        return time.monotonic() - started

    beat = asyncio.create_task(heartbeat())
    requests = []
    try:
        await asyncio.sleep(0.03)
        probe = asyncio.create_task(probes())
        requests = [asyncio.create_task(real_api.post(
            endpoint, json=google_request(endpoint, "synthetic-invalid-token"),
        )) for _ in range(3)]
        elapsed = await probe
        responses = await asyncio.gather(*requests)
        await asyncio.sleep(0.02)
        assert certificates.calls == 3
        assert all(r.status_code == 401 for r in responses)
        assert all(r.json()["error"]["code"] == "invalid_google_token" for r in responses)
        max_gap = max(b - a for a, b in zip(ticks, ticks[1:]))
        assert max_gap < 0.15, f"event loop paused for {max_gap:.3f}s"
        assert elapsed < 0.2, f"unrelated HTTP work waited {elapsed:.3f}s"
    finally:
        beat.cancel()
        await asyncio.gather(beat, *requests, return_exceptions=True)


@pytest.mark.parametrize("endpoint", ["/v1/auth/google", "/v1/account/delete-retry"])
async def test_cached_certificates_still_validate_signature_and_every_token(
    real_api, certificates, endpoint,
):
    certificates.cache_control = "public, max-age=3600"
    good = certificates.token()
    response = await real_api.post(endpoint, json=google_request(endpoint, good))
    assert response.status_code == (200 if endpoint.endswith("google") else 204)
    assert certificates.calls == 1

    # The cert cache never caches an identity/result, even for the same sub.
    invalid = [
        certificates.token(aud="another-client"),
        certificates.token(iss="https://another-issuer.example"),
        certificates.token(exp=int(time.time()) - 1),
        certificates.token(iat=int(time.time()) + 100),
        certificates.token(omit=("sub",)),
        certificates.token(sub=""),
        certificates.token(sub=["subject-is-not-a-string"]),
        "malformed-token",
    ]
    header, payload, signature = good.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    invalid.append(".".join((header, payload, replacement + signature[1:])))
    for token in invalid:
        response = await real_api.post(endpoint, json=google_request(endpoint, token))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_google_token"
    assert certificates.calls == 1


async def test_certificate_cache_respects_age_expiry_and_key_rotation(real_api, certificates):
    certificates.cache_control = "public, max-age=3600"
    certificates.age = "3599"  # Only one second of freshness remains.
    token = certificates.token()
    assert (await real_api.post(
        "/v1/auth/google", json=google_request("/v1/auth/google", token),
    )).status_code == 200
    assert certificates.calls == 1
    assert (await real_api.post(
        "/v1/auth/google", json=google_request("/v1/auth/google", token),
    )).status_code == 200
    assert certificates.calls == 1

    await asyncio.sleep(1.05)
    # Rotate the kid at the cert endpoint and sign a new token with that kid.
    certificates.rotate()
    certificates.age = "0"
    rotated = certificates.token()
    response = await real_api.post(
        "/v1/auth/google", json=google_request("/v1/auth/google", rotated),
    )
    assert response.status_code == 200, response.text
    assert certificates.calls == 2
    response = await real_api.post(
        "/v1/auth/google", json=google_request("/v1/auth/google", token),
    )
    assert response.status_code == 401  # The removed key is no longer trusted.
    assert certificates.calls == 2


async def test_expired_certificates_are_not_used_when_refresh_fails(real_api, certificates):
    certificates.cache_control = "max-age=1"
    token = certificates.token()
    assert (await real_api.post(
        "/v1/auth/google", json=google_request("/v1/auth/google", token),
    )).status_code == 200
    await asyncio.sleep(1.05)
    certificates.status = 503
    response = await real_api.post(
        "/v1/auth/google", json=google_request("/v1/auth/google", token),
    )
    assert response.status_code == 401
    assert certificates.calls == 2


@pytest.mark.parametrize("cache_control", [
    "max-age=3600, no-store", "max-age=3600, no-cache", "", "max-age=invalid",
])
async def test_certificates_without_a_permitted_lifetime_are_fetched_again(
    real_api, certificates, cache_control,
):
    certificates.cache_control = cache_control
    token = certificates.token()
    for _ in range(2):
        response = await real_api.post(
            "/v1/auth/google", json=google_request("/v1/auth/google", token),
        )
        assert response.status_code == 200
    assert certificates.calls == 2


async def wait_for_certificate_request(certificates):
    async def wait():
        while not certificates.arrived.is_set():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), 1)


async def configure_verifier(api, **kwargs):
    await api.app.state.google_verifier.aclose()
    api.app.state.google_verifier = RealGoogleVerifier(CLIENT_ID, **kwargs)


async def sign_in_when_available(api, token):
    endpoint = "/v1/auth/google"
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        response = await api.post(endpoint, json=google_request(endpoint, token))
        if response.status_code == 200:
            return response
        assert response.status_code == 401
        await asyncio.sleep(0.005)
    pytest.fail("verifier did not recover after transport finished")


async def test_saturation_and_queue_deadline_do_not_start_work_after_timeout(
    real_api, certificates,
):
    await configure_verifier(real_api, timeout=0.1, transport_timeout=0.5, max_pending=2)
    certificates.delay = 0.35
    token = certificates.token()
    endpoint = "/v1/auth/google"
    first = asyncio.create_task(real_api.post(endpoint, json=google_request(endpoint, token)))
    await wait_for_certificate_request(certificates)
    started = time.monotonic()
    queued = asyncio.create_task(real_api.post(endpoint, json=google_request(endpoint, token)))
    await asyncio.sleep(0.01)
    rejected = await real_api.post(endpoint, json=google_request(endpoint, token))
    assert rejected.status_code == 401
    assert time.monotonic() - started < 0.07  # Queue capacity rejects immediately.
    first_response, queued_response = await asyncio.gather(first, queued)
    assert first_response.status_code == queued_response.status_code == 401
    assert time.monotonic() - started < 0.2  # Queue wait is inside the budget.

    # The HTTP callers are gone; their one actual HTTP request is still asleep.
    # More admissions must not enqueue executor work or grow worker threads.
    responses = await asyncio.gather(*[
        real_api.post(endpoint, json=google_request(endpoint, token)) for _ in range(8)
    ])
    assert all(response.status_code == 401 for response in responses)
    assert certificates.calls == 1
    await asyncio.sleep(0.3)
    async with real_api.app.state.sessionmaker() as session:
        assert (await session.execute(select(Account).where(
            Account.google_sub == "synthetic-subject",
        ))).scalar_one_or_none() is None
    certificates.delay = 0
    response = await real_api.post(endpoint, json=google_request(endpoint, token))
    assert response.status_code == 200
    assert certificates.calls == 2  # Timed-out queue jobs were never executed.


async def test_cancelled_request_keeps_worker_occupied_until_transport_finishes(
    real_api, certificates,
):
    await configure_verifier(real_api, timeout=0.15, transport_timeout=1, max_pending=1)
    certificates.release = threading.Event()
    endpoint = "/v1/auth/google"
    token = certificates.token()
    # This token will fail only after the HTTP response arrives. Its cancelled
    # caller must not leave an unhandled Future exception on the event loop.
    loop = asyncio.get_running_loop()
    previous_handler, errors = loop.get_exception_handler(), []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        pending = asyncio.create_task(real_api.post(
            endpoint, json=google_request(endpoint, "synthetic-invalid-token"),
        ))
        await wait_for_certificate_request(certificates)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        responses = await asyncio.gather(*[
            real_api.post(endpoint, json=google_request(endpoint, token)) for _ in range(5)
        ])
        assert all(response.status_code == 401 for response in responses)
        assert certificates.calls == 1
        assert (await real_api.get("/health")).status_code == 200
        certificates.release.set()
        assert (await sign_in_when_available(real_api, token)).status_code == 200
        assert certificates.calls == 2
    finally:
        certificates.release.set()
        loop.set_exception_handler(previous_handler)
    assert errors == []


async def test_cancelled_queue_waiter_does_not_consume_admission(real_api, certificates):
    await configure_verifier(real_api, timeout=0.1, transport_timeout=1, max_pending=2)
    certificates.release = threading.Event()
    endpoint = "/v1/auth/google"
    token = certificates.token()
    running = asyncio.create_task(real_api.post(endpoint, json=google_request(endpoint, token)))
    await wait_for_certificate_request(certificates)
    queued = asyncio.create_task(real_api.post(endpoint, json=google_request(endpoint, token)))
    await asyncio.sleep(0.01)
    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    started = time.monotonic()
    next_response = await real_api.post(endpoint, json=google_request(endpoint, token))
    assert next_response.status_code == 401
    assert time.monotonic() - started >= 0.08  # Admitted, then queue deadline.
    assert (await running).status_code == 401
    assert certificates.calls == 1
    certificates.release.set()
    assert (await sign_in_when_available(real_api, token)).status_code == 200
    assert certificates.calls == 2


async def test_actual_transport_has_a_finite_socket_timeout(real_api, certificates):
    await configure_verifier(real_api, timeout=0.5, transport_timeout=0.08)
    certificates.delay = 0.35
    endpoint = "/v1/auth/google"
    started = time.monotonic()
    response = await real_api.post(endpoint, json=google_request(endpoint, certificates.token()))
    elapsed = time.monotonic() - started
    assert response.status_code == 401
    assert 0.05 < elapsed < 0.2, f"socket timeout was not enforced: {elapsed:.3f}s"
    assert certificates.calls == 1


async def test_shutdown_rejects_waiters_without_blocking_or_accepting_late_identity(
    real_api, certificates,
):
    await configure_verifier(real_api, timeout=1, transport_timeout=1)
    certificates.release = threading.Event()
    endpoint = "/v1/auth/google"
    token = certificates.token()
    running = asyncio.create_task(real_api.post(endpoint, json=google_request(endpoint, token)))
    await wait_for_certificate_request(certificates)
    queued = asyncio.create_task(real_api.post(endpoint, json=google_request(endpoint, token)))
    await asyncio.sleep(0.01)
    started = time.monotonic()
    await real_api.app.state.google_verifier.aclose()
    assert time.monotonic() - started < 0.1
    assert (await asyncio.wait_for(queued, 0.1)).status_code == 401
    assert (await real_api.post(endpoint, json=google_request(endpoint, token))).status_code == 401
    assert not running.done()
    certificates.release.set()
    assert (await asyncio.wait_for(running, 0.2)).status_code == 401
    assert certificates.calls == 1
