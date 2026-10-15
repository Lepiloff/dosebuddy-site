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
from google.auth.credentials import TokenState

from app.db.models import AlertDelivery, AlertKind, AlertState, Device
from app.worker import scan_once
from tests.conftest import auth_header
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
    # The second recipient reuses the fresh token and spends no second OAuth wait.
    assert [r[1]["android"]["ttl"] for r in fcm.requests[2:]] == ["3s", "3s"]
    assert fcm.loads == fcm.refreshes == 2
    assert all(r[1]["data"] == fcm.requests[0][1]["data"] for r in fcm.requests)
    assert len(fcm.clients) == 2 and all(r[0] is fcm.clients[1] for r in fcm.requests[2:])
    delivered = await saved_row(db_engine, delivery_id)
    assert delivered.state == AlertState.sent.value and delivered.attempts == 1
    assert delivered.sent_at is not None and delivered.expires_at == deadline
    await second.aclose()


async def second_recipient(session, caregiver):
    session.add(Device(id=uuid.uuid4(), account_id=uuid.UUID(caregiver["account_id"]),
                       platform="android", push_token="second-recipient"))
    await session.commit()


async def test_401_then_200_refreshes_next_recipient_and_commits_best_outcome(
    api, session, db_engine, fcm,
):
    caregiver, delivery_id, deadline = await pending(api, session, fcm)
    await second_recipient(session, caregiver)
    sender = fcm.sender()
    fcm.responses = [401, 200]
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    assert await scan_once(maker, sender, now=fcm.now, clock=fcm.clock) == 1
    row = await saved_row(db_engine, delivery_id)
    assert row.state == AlertState.sent.value and row.attempts == 1 and row.expires_at == deadline
    assert len(fcm.requests) == 2 and fcm.loads == 1 and fcm.refreshes == 2
    assert [request[2] for request in fcm.requests] == ["Bearer oauth-1", "Bearer oauth-2"]
    assert len({request[1]["token"] for request in fcm.requests}) == 2
    assert await scan_once(maker, sender, now=fcm.now, clock=fcm.clock) == 0
    assert len(fcm.requests) == 2, "best-outcome commit does not retry the failed recipient"


async def test_all_401_remain_pending_then_refresh_on_original_deadline_retry(
    api, session, db_engine, fcm,
):
    caregiver, delivery_id, deadline = await pending(api, session, fcm)
    await second_recipient(session, caregiver)
    sender = fcm.sender()
    fcm.responses = [401, 401]
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    assert await scan_once(maker, sender, now=fcm.now, clock=fcm.clock) == 0
    queued = await saved_row(db_engine, delivery_id)
    assert queued.state == AlertState.pending.value and queued.attempts == 1
    assert queued.next_attempt_at == fcm.now + timedelta(seconds=60)
    assert queued.expires_at == deadline and fcm.credentials[0].token is None
    fcm.advance(61)
    fcm.refresh_wait = 1
    assert await scan_once(maker, sender, now=fcm.now, clock=fcm.clock) == 1
    assert len(fcm.requests) == 4 and fcm.loads == 1 and fcm.refreshes == 3
    assert [request[1]["android"]["ttl"] for request in fcm.requests[2:]] == ["3s", "3s"]
    assert [request[2] for request in fcm.requests[2:]] == ["Bearer oauth-3"] * 2
    delivered = await saved_row(db_engine, delivery_id)
    assert delivered.state == AlertState.sent.value and delivered.attempts == 2
    assert delivered.expires_at == deadline


async def test_expiry_boundary_after_first_acceptance_forces_refresh_failure_and_replay(
    api, session, db_engine, fcm,
):
    caregiver, delivery_id, deadline = await pending(api, session, fcm)
    await second_recipient(session, caregiver)
    first = fcm.sender()
    fcm.grant_expiry = 226

    def after_first():
        assert fcm.credentials[0].token_state is TokenState.FRESH
        fcm.advance(1)
        assert fcm.credentials[0].token_state is TokenState.STALE
        fcm.fail_refresh = True
        fcm.after_post = None

    fcm.after_post = after_first
    with pytest.raises(ValueError, match="synthetic refresh failure"):
        await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), first,
                        now=fcm.now, clock=fcm.clock)
    assert len(fcm.requests) == 1 and fcm.refreshes == 2
    assert fcm.credentials[0].token is None
    queued = await saved_row(db_engine, delivery_id)
    assert queued.state == AlertState.pending.value and queued.attempts == 0
    assert queued.sent_at is None and queued.expires_at == deadline
    await first.aclose()
    fcm.advance(60)
    fcm.fail_refresh, fcm.grant_expiry, fcm.refresh_wait = False, 3600, 1
    second = fcm.sender()
    assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), second,
                           now=fcm.now, clock=fcm.clock) == 1
    assert len(fcm.requests) == 3 and fcm.loads == 2 and fcm.refreshes == 3
    assert fcm.requests[0][1]["token"] in {request[1]["token"] for request in fcm.requests[1:]}
    assert [request[1]["android"]["ttl"] for request in fcm.requests[1:]] == ["3s", "3s"]
    delivered = await saved_row(db_engine, delivery_id)
    assert delivered.state == AlertState.sent.value and delivered.attempts == 1
    assert delivered.expires_at == deadline


async def test_warm_token_cache_cannot_bypass_revoke_or_replay_committed_delivery(
    api, session, db_engine, fcm,
):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    _, first_id, _ = await _pending(session, caregiver, pid, AlertKind.dose_missed,
                                    subject="accepted", now=fcm.now)
    sender = fcm.sender()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    assert await scan_once(maker, sender, now=fcm.now, clock=fcm.clock) == 1
    _, second_id, _ = await _pending(session, caregiver, pid, AlertKind.dose_missed,
                                     subject="revoked", now=fcm.now)
    response = await api.delete(f"/v1/profiles/{pid}/members/{caregiver['account_id']}",
                                headers=auth_header(owner))
    assert response.status_code == 204
    assert await scan_once(maker, sender, now=fcm.now, clock=fcm.clock) == 0
    assert len(fcm.requests) == 1 and fcm.loads == fcm.refreshes == 1
    assert (await saved_row(db_engine, first_id)).state == AlertState.sent.value
    retired = await saved_row(db_engine, second_id)
    assert retired.state == AlertState.expired.value and retired.attempts == 0
