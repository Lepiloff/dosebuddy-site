"""Caregiver consent at the durable queue and real HTTP/worker boundaries.

PostgreSQL and ASGI are real; the sender only records host-side send attempts.
It does not prove FCM acceptance or what an offline phone displays.
"""

import asyncio
import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    Account, AlertDelivery, AlertKind, AlertState, Device, Profile, ProfileMembership, Role, utcnow,
)
from app.services import alerts
from app.services.push import Delivery
from app.worker import scan_once
from tests.conftest import auth_header
from tests.test_alerts import FailingPush, RecordingPush, _with_watcher
from tests.test_sync import ms, profile, push

pytestmark = pytest.mark.asyncio


async def _regrant(api, owner, caregiver, pid, role="with_alerts"):
    issued = await api.post("/v1/pairing/codes", headers=auth_header(owner),
                            json={"profile_id": pid, "role": role})
    assert issued.status_code == 200, issued.text
    redeemed = await api.post("/v1/pairing/redeem", headers=auth_header(caregiver),
                             json={"code": issued.json()["code"]})
    assert redeemed.status_code == 200, redeemed.text
    return redeemed


async def _pending(session, caregiver, pid, kind, *, subject=None, now=None):
    now = now or utcnow()
    intent = alerts.Alert(uuid.UUID(caregiver["account_id"]), uuid.UUID(pid), kind,
                          subject or str(uuid.uuid4()))
    assert await alerts.claim(session, intent, now)
    await session.commit()
    row = (await session.execute(select(AlertDelivery).where(
        AlertDelivery.account_id == intent.account_id,
        AlertDelivery.profile_id == intent.profile_id,
        AlertDelivery.kind == intent.kind,
        AlertDelivery.subject_id == intent.subject_id,
    ))).scalar_one()
    delivery_id = row.id
    await session.rollback()
    return intent, delivery_id, now


@pytest.mark.parametrize("kind", [AlertKind.dose_missed, AlertKind.profile_stale])
@pytest.mark.parametrize("action", ["revoke", "downgrade", "profile_delete"])
async def test_http_lifecycle_retires_pending_before_worker_retry(
    api, session, db_engine, kind, action,
):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    intent, delivery_id, now = await _pending(session, caregiver, pid, kind)
    # A saved retry/no-token debt whose deadline has not arrived still has to
    # be retired by the consent mutation itself, before any later regrant.
    await session.execute(update(AlertDelivery).where(AlertDelivery.id == delivery_id)
                          .values(next_attempt_at=now + timedelta(minutes=5)))
    await session.commit()
    if action == "revoke":
        response = await api.delete(f"/v1/profiles/{pid}/members/{caregiver['account_id']}",
                                    headers=auth_header(owner))
        assert response.status_code == 204, response.text
    elif action == "downgrade":
        await _regrant(api, owner, caregiver, pid, "viewer")
    else:
        tombstone = profile(pid, at=ms() + 10_000)
        tombstone["deleted_at"] = tombstone["updated_at"]
        result = await push(api, owner, profiles=[tombstone])
        assert result["rejected"] == [] and result["retry"] == [], result
        persisted = await session.get(Profile, uuid.UUID(pid), populate_existing=True)
        assert persisted.deleted_at_ms == tombstone["deleted_at"]
        await session.rollback()

    sender = RecordingPush()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as check:
        row = await check.get(AlertDelivery, delivery_id)
        # Retirement is part of the lifecycle commit, even before the worker
        # sees this non-due row and even if access is regranted first.
        assert row.state == AlertState.expired.value
        assert row.attempts == 0 and row.sent_at is None
    if action != "profile_delete":
        await _regrant(api, owner, caregiver, pid)
    assert await scan_once(maker, sender, now + timedelta(minutes=6)) == 0
    assert sender.sent == [], "successful consent change must prevent a future send"
    if action != "profile_delete":
        # Repeated revoke/downgrade and a new live membership cannot revive the
        # retired unique intent. New subjects remain eligible after regrant.
        assert await scan_once(maker, sender, now + timedelta(minutes=7)) == 0
        async with maker() as claim:
            assert not await alerts.claim(claim, intent, now + timedelta(minutes=7))
            await claim.commit()
        await _pending(session, caregiver, pid, kind, now=now + timedelta(minutes=7))
        assert await scan_once(maker, sender, now + timedelta(minutes=7)) == 1


@pytest.mark.parametrize("action", ["revoke", "downgrade"])
@pytest.mark.parametrize("debt", ["retry", "no_token"])
async def test_real_worker_debt_cannot_revive_after_regrant(api, session, db_engine, action, debt):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    intent, delivery_id, now = await _pending(session, caregiver, pid, AlertKind.dose_missed)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    if debt == "no_token":
        await session.execute(update(Device).where(Device.account_id == intent.account_id)
                              .values(push_token=None))
        await session.commit()
    assert await scan_once(maker, FailingPush(1), now) == 0
    async with maker() as check:
        row = await check.get(AlertDelivery, delivery_id)
        assert row.state == "pending" and row.next_attempt_at > now
        assert row.attempts == (1 if debt == "retry" else 0)
    if action == "revoke":
        for _ in range(2):
            response = await api.delete(f"/v1/profiles/{pid}/members/{caregiver['account_id']}",
                                        headers=auth_header(owner))
            assert response.status_code == 204
    else:
        await _regrant(api, owner, caregiver, pid, "viewer")
    await _regrant(api, owner, caregiver, pid)
    await session.execute(update(Device).where(Device.account_id == intent.account_id)
                          .values(push_token="synthetic-restored-token"))
    await session.commit()
    sender = RecordingPush()
    assert await scan_once(maker, sender, now + timedelta(minutes=6)) == 0
    assert sender.sent == []
    async with maker() as check:
        assert (await check.get(AlertDelivery, delivery_id)).state == "expired"


async def test_losing_lww_delete_does_not_cancel_a_live_intent(api, session, db_engine):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    _, delivery_id, now = await _pending(session, caregiver, pid, AlertKind.dose_missed)
    current = await session.get(Profile, uuid.UUID(pid))
    tombstone = profile(pid, at=current.updated_at_ms - 1)
    tombstone["deleted_at"] = tombstone["updated_at"]
    await session.rollback()
    result = await push(api, owner, profiles=[tombstone])
    assert result["rejected"] == [] and result["retry"] == []
    assert (await session.get(Profile, uuid.UUID(pid), populate_existing=True)).deleted_at_ms is None
    await session.rollback()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    assert await scan_once(maker, RecordingPush(), now) == 1
    async with maker() as check:
        assert (await check.get(AlertDelivery, delivery_id)).state == "sent"


async def test_retention_of_revoked_history_does_not_cancel_new_consent(api, session, db_engine):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    response = await api.delete(f"/v1/profiles/{pid}/members/{caregiver['account_id']}",
                                headers=auth_header(owner))
    assert response.status_code == 204
    await _regrant(api, owner, caregiver, pid)
    _, delivery_id, now = await _pending(session, caregiver, pid, AlertKind.profile_stale)
    await session.execute(delete(ProfileMembership).where(
        ProfileMembership.profile_id == uuid.UUID(pid), ProfileMembership.revoked_at.is_not(None),
    ))
    await session.commit()
    assert (await session.get(AlertDelivery, delivery_id, populate_existing=True)).state == "pending"
    await session.rollback()
    assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), RecordingPush(), now) == 1


async def test_membership_retirement_preserves_authority_nudges(api, session):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    now = utcnow()
    device_id = (await session.execute(select(Device.id).where(
        Device.account_id == uuid.UUID(owner["account_id"]),
    ))).scalar_one()
    await alerts.claim(session, alerts.authority_lost(
        uuid.UUID(owner["account_id"]), uuid.UUID(pid), device_id, 99,
    ), now)
    await session.commit()
    response = await api.delete(f"/v1/profiles/{pid}/members/{caregiver['account_id']}",
                                headers=auth_header(owner))
    assert response.status_code == 204
    row = (await session.execute(select(AlertDelivery))).scalar_one()
    assert row.kind is AlertKind.reminder_authority_lost and row.state == "pending"


@pytest.mark.parametrize("target", ["owner", "caregiver"])
@pytest.mark.parametrize("retry_door", [False, True])
async def test_account_erasure_waits_for_and_removes_pending(
    api, session, db_engine, target, retry_door,
):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    _, delivery_id, now = await _pending(session, caregiver, pid, AlertKind.dose_missed)
    account = owner if target == "owner" else caregiver
    if retry_door:
        subject = (await session.get(Account, uuid.UUID(account["account_id"]))).google_sub
        await session.rollback()
        response = await api.post("/v1/account/delete-retry", json={
            "account_id": account["account_id"], "id_token": f"google-token-{subject}",
        })
    else:
        response = await api.delete("/v1/account", headers=auth_header(account))
    assert response.status_code == 204, response.text
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    sender = RecordingPush()
    assert await scan_once(maker, sender, now) == 0
    assert sender.sent == []
    async with maker() as check:
        assert await check.get(AlertDelivery, delivery_id) is None


@pytest.mark.parametrize("action", ["membership_delete", "membership_downgrade",
                                       "profile_soft_delete", "recipient_soft_delete",
                                       "owner_soft_delete"])
async def test_alternative_database_writers_retire_pending(api, session, action):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    _, delivery_id, _ = await _pending(session, caregiver, pid, AlertKind.profile_stale)
    if action.startswith("membership"):
        statement = (delete(ProfileMembership) if action == "membership_delete"
                     else update(ProfileMembership).values(role=Role.viewer))
        statement = statement.where(ProfileMembership.profile_id == uuid.UUID(pid),
                                    ProfileMembership.account_id == uuid.UUID(caregiver["account_id"]))
    elif action == "profile_soft_delete":
        statement = update(Profile).where(Profile.id == uuid.UUID(pid)).values(deleted_at_ms=ms())
    else:
        account = caregiver if action == "recipient_soft_delete" else owner
        statement = update(Account).where(Account.id == uuid.UUID(account["account_id"]))\
                                   .values(deleted_at=utcnow())
    await session.execute(statement)
    await session.commit()
    assert (await session.get(AlertDelivery, delivery_id, populate_existing=True)).state == "expired"


async def test_worker_rechecks_fresh_eligibility_for_legacy_pending(api, session, db_engine):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    _, delivery_id, now = await _pending(session, caregiver, pid, AlertKind.dose_missed)
    response = await api.delete(f"/v1/profiles/{pid}/members/{caregiver['account_id']}",
                                headers=auth_header(owner))
    assert response.status_code == 204
    # Simulate an old queue writer after revocation or a pre-upgrade intent.
    # Dispatch must not trust detection, even when a pending row survives.
    await session.execute(update(AlertDelivery).where(AlertDelivery.id == delivery_id)
                          .values(state="pending"))
    await session.commit()
    sender = RecordingPush()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    assert await scan_once(maker, sender, now) == 0
    assert sender.sent == []
    async with maker() as check:
        assert (await check.get(AlertDelivery, delivery_id)).state == "expired"
        # Stale detection is not allowed to queue a new subject after revoke.
        assert not await alerts.claim(check, alerts.Alert(
            uuid.UUID(caregiver["account_id"]), uuid.UUID(pid), AlertKind.dose_missed,
            str(uuid.uuid4()),
        ), now)


@pytest.mark.parametrize("outcome", [Delivery.ok, Delivery.retry])
@pytest.mark.parametrize("action", ["revoke", "downgrade", "profile_delete", "owner_delete", "recipient_delete"])
async def test_revoke_ack_waits_for_started_send_and_blocks_future_retry(
    api, session, db_engine, outcome, action,
):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    _, delivery_id, now = await _pending(session, caregiver, pid, AlertKind.dose_missed)
    started, finish = asyncio.Event(), asyncio.Event()

    class PausedPush(RecordingPush):
        async def send(self, token, data, collapse, ttl_seconds):
            await super().send(token, data, collapse, ttl_seconds)
            started.set()
            await finish.wait()
            return outcome

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    sender = PausedPush()
    worker = asyncio.create_task(scan_once(maker, sender, now))
    revoke = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        # Worker holds its delivery row across external I/O, but no global
        # publication lock and no membership lock.
        async with maker() as probe:
            await asyncio.wait_for(probe.execute(text("SELECT sync_publication_lock()")), 1)
            await probe.rollback()
        if action == "revoke":
            mutation = api.delete(f"/v1/profiles/{pid}/members/{caregiver['account_id']}",
                                  headers=auth_header(owner))
        elif action == "downgrade":
            mutation = _regrant(api, owner, caregiver, pid, "viewer")
        elif action == "profile_delete":
            tombstone = profile(pid, at=ms() + 10_000)
            tombstone["deleted_at"] = tombstone["updated_at"]
            mutation = api.post("/v1/sync/push", headers=auth_header(owner),
                                json={"changes": {"profiles": [tombstone]}})
        else:
            mutation = api.delete("/v1/account", headers=auth_header(
                owner if action == "owner_delete" else caregiver,
            ))
        revoke = asyncio.create_task(mutation)
        for _ in range(100):
            async with maker() as probe:
                waiting = (await probe.execute(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                    "AND wait_event_type='Lock' AND pid <> pg_backend_pid())"
                ))).scalar_one()
            if waiting or revoke.done():
                break
            await asyncio.sleep(0.01)
        assert waiting and not revoke.done(), "revoke must not ack ahead of the in-flight send"
        finish.set()
        assert await asyncio.wait_for(worker, 2) == (1 if outcome is Delivery.ok else 0)
        response = await asyncio.wait_for(revoke, 2)
        assert response.status_code == (200 if action in ("downgrade", "profile_delete") else 204), response.text
        if action == "profile_delete":
            assert response.json()["rejected"] == [] and response.json()["retry"] == []
    finally:
        finish.set()
        await asyncio.gather(worker, *([revoke] if revoke else []), return_exceptions=True)
    async with maker() as check:
        row = await check.get(AlertDelivery, delivery_id)
        if action in ("owner_delete", "recipient_delete"):
            assert row is None
        else:
            assert row.state == ("sent" if outcome is Delivery.ok else "expired")
    assert await scan_once(maker, sender, now + timedelta(minutes=2)) == 0
    assert len(sender.sent) == 1


async def test_timed_out_revoke_does_not_ack_or_partially_end_consent(api, session, db_engine):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    _, delivery_id, now = await _pending(session, caregiver, pid, AlertKind.dose_missed)
    started, finish = asyncio.Event(), asyncio.Event()

    class PausedRetry:
        async def send(self, *args):
            started.set()
            await finish.wait()
            return Delivery.retry

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    worker = asyncio.create_task(scan_once(maker, PausedRetry(), now))
    url = f"/v1/profiles/{pid}/members/{caregiver['account_id']}"
    try:
        await asyncio.wait_for(started.wait(), 2)
        async with AsyncClient(transport=ASGITransport(app=api.app, raise_app_exceptions=False),
                               base_url="http://test") as http:
            failed = await asyncio.wait_for(http.delete(url, headers=auth_header(owner)), 7)
        assert failed.status_code == 500
        assert failed.json()["error"]["code"] == "internal_error"
        async with maker() as check:
            member = (await check.execute(select(ProfileMembership).where(
                ProfileMembership.profile_id == uuid.UUID(pid),
                ProfileMembership.account_id == uuid.UUID(caregiver["account_id"]),
            ))).scalar_one()
            assert member.revoked_at is None and member.role is Role.with_alerts
            assert (await check.get(AlertDelivery, delivery_id)).state == "pending"
        # Failed lifecycle rollback released publication while send is still
        # paused. A retry of the lifecycle operation remains meaningful.
        response = await asyncio.wait_for(api.get("/v1/sync/pull", headers=auth_header(owner)), 2)
        assert response.status_code == 200
    finally:
        finish.set()
        await asyncio.wait_for(worker, 2)
    response = await api.delete(url, headers=auth_header(owner))
    assert response.status_code == 204
    sender = RecordingPush()
    assert await scan_once(maker, sender, now + timedelta(minutes=2)) == 0
    assert sender.sent == []
