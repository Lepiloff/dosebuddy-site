"""Committed PostgreSQL pending survives sender/session recreation, not a process kill.

The API, scan_once, PostgreSQL and FcmPush/httpx encoder are real. OAuth and
provider responses are synthetic; accepted here does not prove phone delivery.
"""

import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import AlertDelivery, AlertKind, AlertState, Device
from app.worker import scan_once
from tests.test_alert_eligibility import _pending
from tests.test_alerts import _with_watcher
from tests.test_push_lifecycle import fcm  # noqa: F401

pytestmark = pytest.mark.asyncio


async def pending(api, session, fcm):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    _, delivery_id, _ = await _pending(session, caregiver, pid, AlertKind.dose_missed,
                                      subject="same-event", now=fcm.now)
    deadline = fcm.now + timedelta(seconds=65.5)
    await session.execute(update(AlertDelivery).where(AlertDelivery.id == delivery_id)
                          .values(expires_at=deadline))
    await session.commit()
    return caregiver, delivery_id, deadline


async def saved_row(db_engine, delivery_id):
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        return await session.get(AlertDelivery, delivery_id)


@pytest.mark.parametrize("failure", ["503", "transport", "refresh"])
async def test_saved_pending_retries_with_new_sender_and_original_deadline(
    api, session, db_engine, fcm, failure,
):
    _, delivery_id, deadline = await pending(api, session, fcm)
    first = fcm.sender()
    fcm.response = 503 if failure == "503" else httpx.ConnectError("synthetic transport failure")
    fcm.fail_refresh = failure == "refresh"
    if failure == "refresh":
        with pytest.raises(ValueError, match="synthetic refresh failure"):
            await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), first,
                            now=fcm.now, clock=fcm.clock)
    else:
        assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), first,
                               now=fcm.now, clock=fcm.clock) == 0
    queued = await saved_row(db_engine, delivery_id)
    assert queued.state == AlertState.pending.value and queued.sent_at is None
    assert queued.attempts == (0 if failure == "refresh" else 1)
    assert queued.expires_at == deadline
    if failure == "refresh":
        assert not fcm.requests and not fcm.clients
    else:
        assert len(fcm.requests) == 1 and fcm.requests[0][1]["android"]["ttl"] == "65s"
    await first.aclose()

    fcm.advance(61)
    fcm.response, fcm.fail_refresh, fcm.refresh_wait = 200, False, 1
    second = fcm.sender()
    assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), second,
                           now=fcm.now, clock=fcm.clock) == 1
    delivered = await saved_row(db_engine, delivery_id)
    assert delivered.state == AlertState.sent.value and delivered.sent_at is not None
    assert delivered.attempts == (1 if failure == "refresh" else 2)
    assert delivered.expires_at == deadline
    assert fcm.requests[-1][1]["android"]["ttl"] == "3s"
    data = fcm.requests[-1][1]["data"]
    assert set(data) == {"type", "account_id", "profile_id", "subject_id"}
    assert data["subject_id"] == "same-event"
    assert "collapse_key" not in fcm.requests[-1][1]["android"]
    if failure != "refresh":
        assert data == fcm.requests[0][1]["data"]
        assert fcm.requests[0][0] is not fcm.requests[-1][0]
    count = len(fcm.requests)
    assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), second,
                           now=fcm.now, clock=fcm.clock) == 0
    assert len(fcm.requests) == count, "committed acceptance fences later scans"
    await second.aclose()
    assert all(client.close_calls == 1 for client in fcm.clients)


async def test_partial_recipient_acceptance_then_escape_replays_pending_after_restart(
    api, session, db_engine, fcm,
):
    caregiver, delivery_id, deadline = await pending(api, session, fcm)
    session.add(Device(id=uuid.uuid4(), account_id=uuid.UUID(caregiver["account_id"]),
                       platform="android", push_token="second-recipient"))
    await session.commit()
    first = fcm.sender()
    fcm.responses = [200, ValueError("synthetic second-recipient failure")]
    with pytest.raises(ValueError, match="second-recipient failure"):
        await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), first,
                        now=fcm.now, clock=fcm.clock)
    assert len(fcm.requests) == 2 and not fcm.responses
    original_tokens = {r[1]["token"] for r in fcm.requests}
    assert len(original_tokens) == 2
    queued = await saved_row(db_engine, delivery_id)
    assert queued.state == AlertState.pending.value and queued.attempts == 0
    assert queued.sent_at is None and queued.expires_at == deadline
    await first.aclose()

    fcm.advance(61)
    fcm.refresh_wait = 1
    second = fcm.sender()
    assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), second,
                           now=fcm.now, clock=fcm.clock) == 1
    assert {r[1]["token"] for r in fcm.requests[2:]} == original_tokens
    assert [r[1]["android"]["ttl"] for r in fcm.requests[2:]] == ["3s", "2s"]
    assert all(r[1]["data"] == fcm.requests[0][1]["data"] for r in fcm.requests)
    assert len(fcm.clients) == 2 and all(r[0] is fcm.clients[1] for r in fcm.requests[2:])
    delivered = await saved_row(db_engine, delivery_id)
    assert delivered.state == AlertState.sent.value and delivered.attempts == 1
    assert delivered.sent_at is not None and delivered.expires_at == deadline
    await second.aclose()
