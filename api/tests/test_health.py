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


async def test_a_rejected_request_says_which_field_and_why(api, caplog):
    """A 422 that leaves no trace is a client bug nobody can diagnose.

    A client sent `push_token` where the contract says `fcm_token`, so every
    attempt to register for push was refused. Thirteen times, over two days,
    with nothing on this side recording which field was wrong — the reason
    existed only in a response body nobody kept. The caregiver alerts that
    depended on it were silent, and tracing it back took a bug report.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        r = await api.post("/v1/auth/google", json={"device": {"platform": "android"}})

    assert r.status_code == 422
    logged = " ".join(rec.getMessage() for rec in caplog.records)
    assert "request.rejected" in logged
    # The field that was wrong, so the client can be fixed from the log alone.
    assert "id_token" in logged


async def test_a_rejected_request_never_logs_the_value(api, caplog):
    """Names and reasons, never values — the same rule as the response.

    A validation log that echoed input would put medication names into the log
    file, which is the thing the response handler already refuses to do.
    """
    import logging

    secret = "Enalapril-10mg-do-not-log-this"
    with caplog.at_level(logging.WARNING):
        await api.post(
            "/v1/auth/google",
            json={"id_token": {"nested": secret}, "device": {"platform": "android"}},
        )

    logged = " ".join(str(rec.__dict__) for rec in caplog.records)
    assert secret not in logged
