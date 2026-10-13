"""Independent caregiver events through the worker and real FCM JSON encoder.

PostgreSQL, dispatch and FcmPush are real. OAuth and the HTTP transport are
synthetic; these tests cannot observe Firebase storage or a phone's receipt.
"""

import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import AlertDelivery, AlertKind, AlertState, Device, utcnow
from app.services import push as push_service
from app.services.push import Delivery, build_push
from app.worker import scan_once
from tests.conftest import make_settings
from tests.test_alert_eligibility import _pending, _regrant
from tests.test_alerts import _handover, _with_watcher
from tests.test_sync import _owner_with_data

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fcm(monkeypatch):
    messages = []
    control = SimpleNamespace(status=200)

    class Credentials:
        token = "synthetic-oauth-token"

        def refresh(self, request):
            pass

    monkeypatch.setattr(
        push_service.service_account.Credentials, "from_service_account_file",
        lambda *args, **kwargs: Credentials(),
    )

    def accept(request):
        assert str(request.url) == "https://fcm.googleapis.com/v1/projects/synthetic-project/messages:send"
        assert request.headers["authorization"] == "Bearer synthetic-oauth-token"
        # Decode the HTTP body, not the sender's in-memory dictionary: omitted
        # fields must remain absent after the actual httpx JSON encoding.
        envelope = json.loads(request.content)
        assert set(envelope) == {"message"}
        messages.append(envelope["message"])
        return httpx.Response(control.status, json={})

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(accept)
    monkeypatch.setattr(
        push_service.httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    sender = build_push(make_settings(
        fcm_project_id="synthetic-project", fcm_credentials_path="/unused/synthetic-key.json",
    ))
    return sender, messages, control


async def second_watched_profile(api, session, caregiver):
    owner, pid, *_ = await _owner_with_data(api)
    await _regrant(api, owner, caregiver, pid)
    return pid


async def queue_events(session, caregiver, profiles, count, now):
    expected = set()
    for i in range(count):
        kind = AlertKind.dose_missed if i % 2 == 0 else AlertKind.profile_stale
        pid = profiles[(i // 2) % len(profiles)]
        # Reuse subjects across profiles and kinds. The client ledger must
        # retain those distinctions, independently of provider metadata.
        subject = f"synthetic-subject-{i // 4}"
        await _pending(session, caregiver, pid, kind, subject=subject, now=now)
        expected.add((kind.value, pid, subject))
    assert len(expected) == count
    return expected


def assert_caregiver_messages(messages, expected, account_id, token):
    assert len(messages) == len(expected)
    assert {message["token"] for message in messages} == {token}
    assert {
        (message["data"]["type"], message["data"]["profile_id"], message["data"]["subject_id"])
        for message in messages
    } == expected
    keys = [message["android"]["collapse_key"] for message in messages
            if "collapse_key" in message["android"]]
    assert not keys, f"independent caregiver events consume {len(set(keys))} pending collapse keys on one token"
    for message in messages:
        assert set(message) == {"token", "data", "android"}
        assert set(message["data"]) == {"type", "account_id", "profile_id", "subject_id"}
        assert message["data"]["account_id"] == account_id
        assert all(isinstance(value, str) for value in message["data"].values())
        assert set(message["android"]) == {"priority", "ttl"}
        assert message["android"]["priority"] == "high"


@pytest.mark.parametrize("count", [5, 10])
async def test_worker_sends_independent_caregiver_events_without_collapse(
    api, session, db_engine, fcm, count,
):
    _, caregiver, pid_a, *_ = await _with_watcher(api, session)
    pid_b = await second_watched_profile(api, session, caregiver)
    now = utcnow()
    expected = await queue_events(session, caregiver, (pid_a, pid_b), count, now)
    sender, messages, _ = fcm
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    assert await scan_once(maker, sender, now=now) == count
    assert_caregiver_messages(messages, expected, caregiver["account_id"], "token-" + caregiver["account_id"][:8])
    assert {message["android"]["ttl"] for message in messages} == {"21600s", "86400s"}
    async with maker() as check:
        rows = (await check.execute(select(AlertDelivery))).scalars().all()
        assert len(rows) == count
        assert all(row.state == AlertState.sent.value and row.attempts == 1 for row in rows)
    assert await scan_once(maker, sender, now=now + timedelta(minutes=1)) == 0
    assert len(messages) == count, "accepted events stay fenced in the durable queue"


@pytest.mark.parametrize("kind", [AlertKind.dose_missed, AlertKind.profile_stale])
async def test_worker_retry_keeps_caregiver_event_identity_without_collapse(
    api, session, db_engine, fcm, kind,
):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    _, delivery_id, now = await _pending(session, caregiver, pid, kind, subject="same-event")
    sender, messages, control = fcm
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    control.status = 503
    assert await scan_once(maker, sender, now=now) == 0
    async with maker() as check:
        queued = await check.get(AlertDelivery, delivery_id)
        assert queued.state == AlertState.pending.value and queued.attempts == 1
        expires_at = queued.expires_at
    control.status = 200
    assert await scan_once(maker, sender, now=now + timedelta(minutes=2)) == 1
    assert len(messages) == 2 and messages[0]["data"] == messages[1]["data"]
    for message in messages:
        assert_caregiver_messages([message], {(kind.value, pid, "same-event")},
                                  caregiver["account_id"], "token-" + caregiver["account_id"][:8])
    assert int(messages[0]["android"]["ttl"][:-1]) - int(messages[1]["android"]["ttl"][:-1]) == 120
    async with maker() as check:
        delivered = await check.get(AlertDelivery, delivery_id)
        assert delivered.state == AlertState.sent.value and delivered.attempts == 2
        assert delivered.expires_at == expires_at


async def test_worker_mixed_token_keeps_only_authority_collapse_across_retry(
    api, session, db_engine, fcm,
):
    caregiver, authority_pid, losing, winning = await _handover(api, session, db_engine)
    losing_id, winning_id = losing.id, winning.id
    await session.execute(update(Device).where(Device.id == losing_id).values(push_token="shared-token"))
    await session.execute(update(Device).where(Device.id == winning_id).values(push_token=None))
    await session.commit()
    # The owner of this profile watches two other owners on the same handset.
    profiles = tuple([await second_watched_profile(api, session, caregiver) for _ in range(2)])
    now = utcnow()
    expected = await queue_events(session, caregiver, profiles, 5, now)
    sender, messages, control = fcm
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    control.status = 503
    assert await scan_once(maker, sender, now=now) == 0
    control.status = 200
    assert await scan_once(maker, sender, now=now + timedelta(minutes=2)) == 5
    assert len(messages) == 12 and {message["token"] for message in messages} == {"shared-token"}
    authority = [message for message in messages if message["data"]["type"] == "reminder_authority_lost"]
    assert len(authority) == 2
    assert {message["android"]["collapse_key"] for message in authority} == {
        f"reminder_authority_lost:{authority_pid}",
    }
    assert all(message["data"]["owner_device_id"] == str(winning_id) for message in authority)
    assert all(message["data"]["account_id"] == caregiver["account_id"] for message in authority)
    caregiver_messages = [message for message in messages if message not in authority]
    assert_caregiver_messages(caregiver_messages[:5], expected, caregiver["account_id"], "shared-token")
    assert_caregiver_messages(caregiver_messages[5:], expected, caregiver["account_id"], "shared-token")
    async with maker() as check:
        rows = (await check.execute(select(AlertDelivery))).scalars().all()
        assert len(rows) == 6
        assert all(row.state == AlertState.sent.value and row.attempts == 2 for row in rows)


@pytest.mark.parametrize("collapse", [None, "reminder_authority_lost:synthetic-profile"])
async def test_fcm_http_omits_optional_collapse_instead_of_encoding_null(fcm, collapse):
    sender, messages, _ = fcm
    assert await sender.send("synthetic-token", {"type": "synthetic-event"}, collapse, 60) is Delivery.ok
    android = messages[0]["android"]
    if collapse is None:
        assert "collapse_key" not in android, "null is an encoded field, not omission"
    else:
        assert android["collapse_key"] == collapse
    assert android["priority"] == "high" and int(android["ttl"][:-1]) > 0
