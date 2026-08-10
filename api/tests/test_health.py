"""What the deploy relies on.

The deploy treats readiness as a gate, so the case worth testing hardest is the
one where a dependency is down: an endpoint that reports "ready" regardless is
worse than no endpoint, because it turns a broken release into a green one.
"""

import pytest

from tests.conftest import FakeRedis, FakeSessionmaker

pytestmark = pytest.mark.asyncio


async def test_health_is_up_and_reports_version(client, app):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": app.state.settings.version}


async def test_health_does_not_touch_dependencies(client, app):
    """Liveness must survive a dead database, or a Postgres restart reads as a
    dead process and something upstream starts killing containers."""
    app.state.sessionmaker = FakeSessionmaker(fail=True)
    app.state.redis = FakeRedis(fail=True)

    r = await client.get("/health")
    assert r.status_code == 200


async def test_ready_when_dependencies_answer(client):
    r = await client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": "ok", "redis": "ok"}


async def test_not_ready_when_database_is_down(client, app):
    app.state.sessionmaker = FakeSessionmaker(fail=True)

    r = await client.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not ready"
    assert body["checks"]["database"].startswith("unavailable")
    assert body["checks"]["redis"] == "ok"


async def test_not_ready_when_redis_is_down(client, app):
    app.state.redis = FakeRedis(fail=True)

    r = await client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["checks"]["redis"].startswith("unavailable")


async def test_failure_detail_never_leaks_the_connection_string(client, app):
    """A connection error can carry the DSN, and the DSN carries the password."""
    app.state.sessionmaker = FakeSessionmaker(fail=True)

    body = (await client.get("/health/ready")).text
    assert "postgresql" not in body
    assert "@" not in body


async def test_docs_are_off_by_default(client):
    assert (await client.get("/docs")).status_code == 404
    assert (await client.get("/openapi.json")).status_code == 404
