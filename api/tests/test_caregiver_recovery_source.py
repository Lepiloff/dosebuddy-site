"""Real PostgreSQL recovery source; no FCM/device or reader acceptance claim."""

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import worker
from app.db.models import (
    Account, AlertDelivery, AlertKind, CaregiverRecoveryControl, CaregiverRecoveryEvent,
    Profile, ProfileMembership, Role, utcnow,
)
from app.db.schema_check import _caregiver_recovery_problems
from app.services import alerts, caregiver_recovery, retention
from app.services.caregiver_recovery import close_coverage, enable_coverage
from tests.conftest import auth_header, make_settings
from tests.test_alerts import RecordingPush, _with_watcher
from tests.test_alert_eligibility import _pending, _regrant

pytestmark = pytest.mark.asyncio


async def enabled(session, now=None):
    control = await enable_coverage(session, now or utcnow(), writers_verified=True)
    epoch = control.epoch
    await session.commit()
    return epoch


async def count(session, model):
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_disabled_cutover_original_deadline_conflict_restart_and_close(api, session, db_engine):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    control = await session.get(CaregiverRecoveryControl, 1)
    assert not control.producer_enabled and not control.reader_enabled and control.epoch is None
    legacy, legacy_id, scan_now = await _pending(session, caregiver, pid, AlertKind.dose_missed)
    assert await count(session, CaregiverRecoveryEvent) == 0
    with pytest.raises(ValueError):
        await enable_coverage(session, utcnow())
    cutoff = scan_now + timedelta(minutes=2)
    epoch = await enabled(session, cutoff)
    assert not await alerts.claim(session, legacy, cutoff)
    await session.commit()
    assert await count(session, CaregiverRecoveryEvent) == 0, "legacy conflict must not backfill"
    intent, delivery_id, _ = await _pending(session, caregiver, pid, AlertKind.dose_missed, now=scan_now)
    row = await session.get(CaregiverRecoveryEvent, delivery_id)
    assert row.epoch == epoch and row.raised_at == scan_now < cutoff
    assert row.expires_at == scan_now + timedelta(hours=6)
    original = (row.event_seq, row.raised_at, row.expires_at)
    await session.rollback()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as restarted:
        assert not await alerts.claim(restarted, intent, cutoff + timedelta(minutes=1))
        row = await restarted.get(CaregiverRecoveryEvent, delivery_id)
        assert (row.event_seq, row.raised_at, row.expires_at) == original
        await restarted.rollback()
        await close_coverage(restarted, cutoff + timedelta(minutes=2))
        await restarted.commit()
        _, gap_id, _ = await _pending(restarted, caregiver, pid, AlertKind.profile_stale)
        assert await restarted.get(CaregiverRecoveryEvent, gap_id) is None
        epoch2 = await enabled(restarted, cutoff + timedelta(minutes=3))
        assert epoch2 != epoch
        _, next_id, _ = await _pending(restarted, caregiver, pid, AlertKind.profile_stale)
        row2 = await restarted.get(CaregiverRecoveryEvent, next_id)
        assert row2.epoch == epoch2 and row2.event_seq > original[0]
        assert await restarted.get(CaregiverRecoveryEvent, legacy_id) is None
        control = await restarted.get(CaregiverRecoveryControl, 1, populate_existing=True)
        assert control.producer_enabled and not control.reader_enabled


async def test_parent_child_rollback_and_child_failure_are_atomic(api, session):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    await enabled(session)
    intent = alerts.Alert(uuid.UUID(caregiver["account_id"]), uuid.UUID(pid),
                          AlertKind.dose_missed, str(uuid.uuid4()))
    assert await alerts.claim(session, intent, utcnow())
    assert await count(session, CaregiverRecoveryEvent) == 1
    allocated = (await session.execute(text("SELECT committed_frontier FROM caregiver_recovery_control"))).scalar_one()
    assert allocated > 0
    await session.rollback()
    assert await count(session, CaregiverRecoveryEvent) == 0
    assert (await session.execute(text("SELECT committed_frontier FROM caregiver_recovery_control"))).scalar_one() == 0
    assert (await session.execute(text("SELECT last_value FROM caregiver_event_seq"))).scalar_one() >= allocated
    await session.execute(text("""CREATE FUNCTION reject_recovery_child() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic child failure'; END; $$"""))
    await session.execute(text("""CREATE TRIGGER reject_recovery_child BEFORE INSERT ON caregiver_recovery_events
        FOR EACH ROW EXECUTE FUNCTION reject_recovery_child()"""))
    await session.commit()
    with pytest.raises(DBAPIError):
        await alerts.claim(session, intent, utcnow())
    await session.rollback()
    assert await count(session, CaregiverRecoveryEvent) == 0
    assert (await session.execute(select(AlertDelivery).where(AlertDelivery.subject_id == intent.subject_id))).scalar_one_or_none() is None
    await session.execute(text("DROP TRIGGER reject_recovery_child ON caregiver_recovery_events"))
    await session.execute(text("DROP FUNCTION reject_recovery_child()"))
    assert await alerts.claim(session, intent, utcnow())
    await session.commit()
    assert await count(session, CaregiverRecoveryEvent) == 1
    assert (await session.execute(text("SELECT committed_frontier FROM caregiver_recovery_control"))).scalar_one() > allocated


async def test_operator_cutover_time_is_sampled_after_publication_wait(db_engine, monkeypatch):
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    clock = [utcnow()]
    monkeypatch.setattr(caregiver_recovery, "utcnow", lambda: clock[0])
    async with maker() as predecessor, maker() as operator:
        await predecessor.execute(text("SELECT sync_publication_lock()"))
        operator_pid = (await operator.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        task = asyncio.create_task(enable_coverage(operator, writers_verified=True))
        try:
            for _ in range(200):
                async with db_engine.connect() as probe:
                    blocked = (await probe.execute(text(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"
                    ), {"pid": operator_pid})).scalar_one()
                if blocked:
                    break
                await asyncio.sleep(.01)
            assert blocked and not task.done()
            clock[0] += timedelta(minutes=2)
            await predecessor.commit()
            control = await asyncio.wait_for(task, 3)
            assert control.coverage_started_at == clock[0]
            await operator.commit()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_publication_orders_concurrent_commits_and_delivery_update_never_waits_for_it(api, session, db_engine):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    await enabled(session)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    def intent():
        return alerts.Alert(uuid.UUID(caregiver["account_id"]), uuid.UUID(pid),
                            AlertKind.dose_missed, str(uuid.uuid4()))
    async with maker() as first, maker() as second:
        first_intent, second_intent = intent(), intent()
        assert await alerts.claim(first, first_intent, utcnow())
        pid2 = (await second.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        async def claim_second():
            assert await alerts.claim(second, second_intent, utcnow())
            await second.commit()
        task = asyncio.create_task(claim_second())
        try:
            for _ in range(200):
                async with db_engine.connect() as probe:
                    blocked = (await probe.execute(text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"), {"pid": pid2})).scalar_one()
                if blocked:
                    break
                await asyncio.sleep(.01)
            assert blocked and not task.done()
            await first.commit()
            await asyncio.wait_for(task, 3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    rows = (await session.execute(select(CaregiverRecoveryEvent).order_by(CaregiverRecoveryEvent.event_seq))).scalars().all()
    assert len(rows) == 2 and rows[0].event_seq < rows[1].event_seq
    first_delivery_id = rows[0].delivery_id
    await session.rollback()
    # Hold publication on another connection: a delivery-state update must not
    # request it after taking the row, even while recovery is enabled.
    async with maker() as publication, maker() as delivery:
        await publication.execute(text("SELECT sync_publication_lock()"))
        await asyncio.wait_for(delivery.execute(update(AlertDelivery).where(
            AlertDelivery.id == first_delivery_id).values(state="sent")), 1)
        await delivery.commit()


@pytest.mark.parametrize("state", ["pending", "sent", "given_up"])
@pytest.mark.parametrize("action", ["revoke", "downgrade", "membership_delete", "profile_soft", "recipient_soft", "owner_soft", "profile_hard", "recipient_hard"])
async def test_all_transport_states_retire_irreversibly_or_cascade(api, session, state, action):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    await enabled(session)
    intent, delivery_id, _ = await _pending(session, caregiver, pid, AlertKind.dose_missed)
    await session.execute(update(AlertDelivery).where(AlertDelivery.id == delivery_id).values(state=state))
    membership = (await session.execute(select(ProfileMembership).where(
        ProfileMembership.profile_id == uuid.UUID(pid), ProfileMembership.revoked_at.is_(None)))).scalar_one()
    membership_id = membership.id
    await session.commit()
    if action == "revoke":
        response = await api.delete(f"/v1/profiles/{pid}/members/{caregiver['account_id']}", headers=auth_header(owner))
        assert response.status_code == 204
    elif action == "downgrade":
        await _regrant(api, owner, caregiver, pid, "viewer")
    elif action == "membership_delete":
        await session.execute(delete(ProfileMembership).where(ProfileMembership.id == membership_id))
    elif action == "profile_soft":
        await session.execute(update(Profile).where(Profile.id == uuid.UUID(pid)).values(deleted_at_ms=2))
    elif action == "profile_hard":
        await session.execute(delete(Profile).where(Profile.id == uuid.UUID(pid)))
    else:
        aid = uuid.UUID(owner["account_id"] if action == "owner_soft" else caregiver["account_id"])
        operation = delete(Account).where(Account.id == aid) if action == "recipient_hard" else update(Account).where(Account.id == aid).values(deleted_at=utcnow())
        await session.execute(operation)
    await session.commit()
    row = await session.get(CaregiverRecoveryEvent, delivery_id, populate_existing=True)
    if action.endswith("hard"):
        assert row is None and await session.get(AlertDelivery, delivery_id) is None
        return
    assert row.retired_at is not None
    retired_at = row.retired_at
    parent = await session.get(AlertDelivery, delivery_id, populate_existing=True)
    assert parent.state == ("expired" if state == "pending" else state)
    if action in ("revoke", "downgrade", "membership_delete"):
        await session.rollback()
        await _regrant(api, owner, caregiver, pid)
        if action == "downgrade":
            live = (await session.execute(select(ProfileMembership).where(
                ProfileMembership.profile_id == uuid.UUID(pid), ProfileMembership.revoked_at.is_(None)))).scalar_one()
            assert live.id == membership_id, "same membership ID is reused on role upgrade"
        assert not await alerts.claim(session, intent, utcnow())
        await session.commit()
        row = await session.get(CaregiverRecoveryEvent, delivery_id, populate_existing=True)
        assert row.retired_at == retired_at
    with pytest.raises(IntegrityError):
        await session.execute(update(CaregiverRecoveryEvent).where(CaregiverRecoveryEvent.delivery_id == delivery_id).values(retired_at=None))
    await session.rollback()


async def test_old_revoked_history_purge_does_not_retire_new_membership_event(api, session):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    await enabled(session)
    _, delivery_id, _ = await _pending(session, caregiver, pid, AlertKind.profile_stale)
    session.add(ProfileMembership(account_id=uuid.UUID(caregiver["account_id"]), profile_id=uuid.UUID(pid),
                                  role=Role.with_alerts, revoked_at=utcnow() - timedelta(days=731)))
    await session.commit()
    assert await retention.sweep(session, utcnow()) == 1
    await session.commit()
    assert (await session.get(CaregiverRecoveryEvent, delivery_id)).retired_at is None


async def test_retention_exact_threshold_keeps_parent_fence(api, session):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    await enabled(session)
    intent, delivery_id, now = await _pending(session, caregiver, pid, AlertKind.dose_missed)
    threshold = now + timedelta(hours=30)
    frontier = (await session.execute(text("SELECT committed_frontier FROM caregiver_recovery_control"))).scalar_one()
    assert await retention.purge_recovery_batch(session, threshold - timedelta(microseconds=1)) == 0
    assert await retention.purge_recovery_batch(session, threshold) == 1
    await session.commit()
    assert await session.get(AlertDelivery, delivery_id) is not None
    assert not await alerts.claim(session, intent, threshold)
    await session.commit()
    assert await count(session, CaregiverRecoveryEvent) == 0
    assert (await session.execute(text("SELECT committed_frontier FROM caregiver_recovery_control"))).scalar_one() == frontier


async def test_daily_bounded_cleanup_retains_due_until_remainder_drained(api, session, db_engine):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    await enabled(session)
    now = utcnow()
    await session.execute(text("""INSERT INTO alert_deliveries
        (id,account_id,profile_id,kind,subject_id,state,attempts,next_attempt_at,expires_at)
        SELECT md5('r1-event-' || i)::uuid, :aid, :pid, 'dose_missed', 'r1-event-' || i,
        'given_up', 5, :raised, :expires FROM generate_series(1,10001) AS i"""),
        {"aid": uuid.UUID(caregiver["account_id"]), "pid": uuid.UUID(pid),
         "raised": now-timedelta(hours=31), "expires": now-timedelta(hours=25)})
    await session.commit()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    last = now-timedelta(days=1)
    assert await worker.sweep_if_due(maker, last, now) == last
    assert await count(session, CaregiverRecoveryEvent) == 1
    assert await count(session, AlertDelivery) == 10001
    await session.rollback()
    later = now+timedelta(seconds=60)
    assert await worker.sweep_if_due(maker, last, later) == later
    assert await count(session, CaregiverRecoveryEvent) == 0
    assert await count(session, AlertDelivery) == 10001


async def test_cleanup_failure_does_not_stop_next_real_delivery_scan(api, session, db_engine, monkeypatch):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    await enabled(session)
    _, delivery_id, _ = await _pending(session, caregiver, pid, AlertKind.dose_missed)
    sender, events = RecordingPush(), []
    async def close():
        pass
    sender.aclose = close
    real_scan, real_purge = worker.scan_once, retention.purge_recovery_batch
    async def scan(*args, **kwargs):
        events.append("scan")
        return await real_scan(*args, **kwargs)
    async def purge(*args, **kwargs):
        events.append("purge")
        if events.count("purge") == 1:
            raise RuntimeError("synthetic cleanup failure")
        return await real_purge(*args, **kwargs)
    async def sleep(_):
        if events.count("scan") == 2:
            raise asyncio.CancelledError()
    monkeypatch.setattr(worker, "get_settings", make_settings)
    monkeypatch.setattr(worker, "setup_logging", lambda _: None)
    monkeypatch.setattr(worker, "create_engine", lambda _: db_engine)
    monkeypatch.setattr(worker, "build_push", lambda _: sender)
    monkeypatch.setattr(worker, "scan_once", scan)
    monkeypatch.setattr(retention, "purge_recovery_batch", purge)
    monkeypatch.setattr(worker, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        await worker.main()
    assert events == ["scan", "purge", "scan", "purge"]
    assert (await session.get(AlertDelivery, delivery_id, populate_existing=True)).state == "sent"


@pytest.mark.parametrize("tamper", [
    "ALTER SEQUENCE caregiver_event_seq CACHE 8",
    "DROP INDEX ix_caregiver_recovery_expiry",
    "ALTER TABLE alert_deliveries DISABLE TRIGGER alert_deliveries_recovery_publication",
    "ALTER TABLE profiles DISABLE TRIGGER profile_recovery_retire_delete",
    "ALTER TABLE caregiver_recovery_control ALTER COLUMN producer_enabled SET DEFAULT true",
    "CREATE OR REPLACE FUNCTION guard_caregiver_recovery() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$",
])
async def test_schema_guard_checks_actual_source_retirement_and_defaults(session, tamper):
    conn = await session.connection()
    assert await _caregiver_recovery_problems(conn) == []
    await session.execute(text(tamper))
    assert await _caregiver_recovery_problems(conn), tamper
