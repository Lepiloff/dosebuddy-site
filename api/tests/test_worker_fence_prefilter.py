"""Durable alert fences at the real PostgreSQL worker boundary."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    Account, AlertDelivery, AlertKind, Device, DoseEvent, Medication, Profile,
    ProfileMembership, Role,
)
from app.db.publication import PUBLICATION_SQL, lock_publication
from app.services import alerts
from app.services.push import Delivery
from app.worker import scan_once
from tests.test_sync_publication import _pid, _waiting

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
MS = int(NOW.timestamp() * 1000)


async def _family(engine, *, recipients=1, profiles=1):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    owner = uuid.uuid4()
    accounts = [uuid.uuid4() for _ in range(recipients)]
    pids, mids = [uuid.uuid4() for _ in range(profiles)], [uuid.uuid4() for _ in range(profiles)]
    async with maker() as session:
        session.add_all([Account(id=value, google_sub=str(value)) for value in [owner, *accounts]])
        await session.flush()
        session.add_all([Device(id=uuid.uuid4(), account_id=account, platform="android",
                               push_token=str(account), last_seen_at=NOW) for account in accounts])
        session.add_all([Profile(id=pid, owner_account_id=owner, name="Synthetic",
                                created_at_ms=MS, updated_at_ms=MS) for pid in pids])
        await session.flush()
        session.add_all([Medication(id=mid, profile_id=pid, name="Synthetic", form="tablet",
                                   dosage_text="", created_at_ms=MS, updated_at_ms=MS)
                         for pid, mid in zip(pids, mids)])
        session.add_all([ProfileMembership(id=uuid.uuid4(), account_id=account, profile_id=pid,
                            role=Role.with_alerts, dose_alert_after_minutes=30, stale_alert_after_hours=36)
                         for account in accounts for pid in pids])
        await session.commit()
    return SimpleNamespace(maker=maker, accounts=accounts, profiles=pids, medications=mids)


def _alert(family, **overrides):
    return alerts.Alert(**{**dict(account_id=family.accounts[0], profile_id=family.profiles[0],
                                 kind=AlertKind.dose_missed, subject_id=str(uuid.uuid4())), **overrides})


def _fence(alert, state="sent", **overrides):
    return dict(id=uuid.uuid4(), account_id=alert.account_id, profile_id=alert.profile_id,
                kind=alert.kind, subject_id=alert.subject_id, device_id=None, state=state,
                attempts=1, next_attempt_at=NOW + timedelta(minutes=5),
                expires_at=NOW + timedelta(hours=5), last_error=None,
                sent_at=NOW - timedelta(minutes=10) if state == "sent" else None, **overrides)


async def _store(family, rows=(), doses=()):
    async with family.maker() as session:
        session.add_all([DoseEvent(id=uuid.UUID(alert.subject_id), profile_id=alert.profile_id,
                                  medication_id=family.medications[0], status="missed", dose_amount=1,
                                  planned_at_ms=MS - 3600_000, created_at_ms=MS, updated_at_ms=MS)
                         for alert in doses])
        session.add_all([AlertDelivery(**row) for row in rows])
        await session.commit()


async def _rows(family):
    # A new session proves durable acceptance rather than an ORM identity map.
    async with family.maker() as session:
        return {row.subject_id: {column.name: getattr(row, column.name)
                                for column in AlertDelivery.__table__.columns}
                for row in (await session.execute(select(AlertDelivery))).scalars()}


def _found(monkeypatch, found):
    async def missed(*_args):
        return list(found)

    async def stale(*_args):
        return []

    monkeypatch.setattr(alerts, "find_missed_dose_alerts", missed)
    monkeypatch.setattr(alerts, "find_stale_profile_alerts", stale)


class Sender:
    def __init__(self, expected, now=NOW, outcome=Delivery.ok):
        self.expected, self.now, self.outcome, self.subjects = expected, now, outcome, []

    async def send(self, token, data, collapse, ttl_seconds, *, expires_at, clock):
        alert, deadline = self.expected[data["subject_id"]]
        assert data == dict(type=alert.kind.value, account_id=str(alert.account_id),
                            profile_id=str(alert.profile_id), subject_id=alert.subject_id)
        assert token == str(alert.account_id) and collapse is None
        assert expires_at == deadline and clock() == self.now
        assert ttl_seconds == int((deadline - self.now).total_seconds()) > 0
        self.subjects.append(alert.subject_id)
        return self.outcome


@contextmanager
def _preparation_sql(engine, monkeypatch):
    """Controlled finders have no SQL; stop before expire/delivery starts."""
    statements, active = [], True
    original = alerts.expire

    def before(_conn, _cursor, statement, _params, _context, _many):
        if active:
            statements.append(" ".join(statement.split()))

    async def expire(*args, **kwargs):
        nonlocal active
        active = False
        return await original(*args, **kwargs)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        with monkeypatch.context() as local:
            local.setattr(alerts, "expire", expire)
            yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)


async def test_worker_preserves_terminal_fences_and_retry_debt_after_restart(db_engine):
    family = await _family(db_engine)
    held = [_alert(family) for _ in range(4)]
    rows = [_fence(alert, state) for alert, state in zip(held, ["sent", "expired", "given_up", "pending"])]
    fresh = _alert(family)
    await _store(family, rows, [*held, fresh])
    sender = Sender({fresh.subject_id: (fresh, NOW + timedelta(hours=6))})
    assert await scan_once(family.maker, sender, NOW, lease=None) == 1
    assert sender.subjects == [fresh.subject_id]
    first = await _rows(family)
    for row in rows:
        assert first[row["subject_id"]] == row
    assert first[fresh.subject_id]["state"] == "sent"

    retry_at = NOW + timedelta(minutes=6)
    # A new sessionmaker/sender models restart of the worker's local state.
    restarted = async_sessionmaker(db_engine, expire_on_commit=False)
    pending = held[-1]
    failed = Sender({pending.subject_id: (pending, rows[-1]["expires_at"])}, retry_at, Delivery.retry)
    assert await scan_once(restarted, failed, retry_at, lease=None) == 0
    assert failed.subjects == [pending.subject_id]
    debt = (await _rows(family))[pending.subject_id]
    assert debt["state"] == "pending" and debt["attempts"] == 2
    assert debt["next_attempt_at"] > retry_at and debt["expires_at"] == rows[-1]["expires_at"]
    retry = Sender({pending.subject_id: (pending, debt["expires_at"])}, debt["next_attempt_at"])
    assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), retry,
                           retry.now, lease=None) == 1
    final = await _rows(family)
    assert final[pending.subject_id]["state"] == "sent" and final[pending.subject_id]["attempts"] == 3
    assert final[pending.subject_id]["expires_at"] == rows[-1]["expires_at"]
    for row in rows[:-1]:
        assert final[row["subject_id"]] == row
    assert final[fresh.subject_id] == first[fresh.subject_id]


@pytest.mark.parametrize("dimension", ["account", "profile", "kind", "subject"])
async def test_worker_fence_uses_every_unique_key_dimension(db_engine, monkeypatch, dimension):
    family = await _family(db_engine, recipients=2, profiles=2)
    old = _alert(family)
    changes = dict(account=dict(account_id=family.accounts[1]), profile=dict(profile_id=family.profiles[1]),
                   kind=dict(kind=AlertKind.profile_stale), subject=dict(subject_id=str(uuid.uuid4())))
    new = alerts.Alert(**{**old.__dict__, **changes[dimension]})
    row = _fence(old)
    await _store(family, [row])
    _found(monkeypatch, [old, new])
    sender = Sender({new.subject_id: (new, NOW + alerts.TTL[new.kind])})
    assert await scan_once(family.maker, sender, NOW, lease=None) == 1
    assert sender.subjects == [new.subject_id]
    async with family.maker() as session:
        persisted = (await session.execute(select(AlertDelivery))).scalars().all()
        assert len(persisted) == 2
        previous = next(item for item in persisted if item.id == row["id"])
        assert all(getattr(previous, key) == value for key, value in row.items())
        accepted = next(item for item in persisted if item.id != row["id"])
        assert (accepted.account_id, accepted.profile_id, accepted.kind, accepted.subject_id) == (
            new.account_id, new.profile_id, new.kind, new.subject_id)
        assert accepted.state == "sent" and accepted.attempts == 1


async def test_chunk_boundary_preserves_missing_order_duplicates_and_all_states(db_engine, monkeypatch):
    family = await _family(db_engine)
    known = [_alert(family) for _ in range(501)]
    rows = [_fence(alert, ["sent", "expired", "given_up", "pending"][i % 4])
            for i, alert in enumerate(known)]
    await _store(family, rows)
    a, b = _alert(family), _alert(family)
    _found(monkeypatch, [*known[:499], a, known[499], b, a, known[500]])
    original, missing_claims = alerts.claim, []

    async def claim(session, alert, now):
        if alert.subject_id in {a.subject_id, b.subject_id}:
            missing_claims.append(alert.subject_id)
        return await original(session, alert, now)

    monkeypatch.setattr(alerts, "claim", claim)
    sender = Sender({item.subject_id: (item, NOW + timedelta(hours=6)) for item in (a, b)})
    assert await scan_once(family.maker, sender, NOW, lease=None) == 2
    assert missing_claims == [a.subject_id, b.subject_id, a.subject_id]
    assert sorted(sender.subjects) == sorted([a.subject_id, b.subject_id])
    persisted = await _rows(family)
    assert len(persisted) == 503
    for row in rows:
        assert persisted[row["subject_id"]] == row
    assert all(persisted[item.subject_id]["state"] == "sent" for item in (a, b))


async def test_all_fenced_worker_still_waits_for_publication_and_keeps_local_timeout(db_engine, monkeypatch):
    family = await _family(db_engine)
    known = _alert(family)
    row = _fence(known)
    await _store(family, [row], [known])
    detected, release = asyncio.Event(), asyncio.Event()
    original_stale, original_expire = alerts.find_stale_profile_alerts, alerts.expire
    worker_pid, timeout_seen = [], []

    async def stale(session, now):
        result = await original_stale(session, now)
        worker_pid.append(await _pid(session))
        detected.set()
        await release.wait()
        return result

    async def expire(session, now):
        timeout_seen.append((await session.execute(text("SELECT current_setting('lock_timeout')"))).scalar_one())
        return await original_expire(session, now)

    monkeypatch.setattr(alerts, "find_stale_profile_alerts", stale)
    monkeypatch.setattr(alerts, "expire", expire)
    sender = Sender({})
    async with family.maker() as publisher:
        await lock_publication(publisher)
        task = asyncio.create_task(scan_once(family.maker, sender, NOW, lease=None))
        try:
            await asyncio.wait_for(detected.wait(), 3)
            release.set()
            assert await _waiting(task, db_engine, worker_pid[0]), "all-fenced scan passed the publication fence"
            assert timeout_seen == []
            await publisher.commit()
            assert await asyncio.wait_for(task, 3) == 0
        finally:
            release.set()
            await publisher.rollback()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert timeout_seen == ["5s"]
    assert (await _rows(family))[known.subject_id] == row


async def test_revoke_committed_after_detection_prevents_a_new_claim(db_engine, monkeypatch):
    family = await _family(db_engine)
    fresh = _alert(family)
    await _store(family, doses=[fresh])
    detected, release = asyncio.Event(), asyncio.Event()
    original = alerts.find_stale_profile_alerts
    worker_pid = []

    async def stale(session, now):
        result = await original(session, now)
        worker_pid.append(await _pid(session))
        detected.set()
        await release.wait()
        return result

    monkeypatch.setattr(alerts, "find_stale_profile_alerts", stale)
    async with family.maker() as publisher:
        await lock_publication(publisher)
        task = asyncio.create_task(scan_once(family.maker, Sender({}), NOW, lease=None))
        try:
            await asyncio.wait_for(detected.wait(), 3)
            await publisher.execute(update(ProfileMembership).values(revoked_at=NOW))
            release.set()
            assert await _waiting(task, db_engine, worker_pid[0])
            await publisher.commit()
            assert await asyncio.wait_for(task, 3) == 0
        finally:
            release.set()
            await publisher.rollback()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await _rows(family) == {}


async def test_empty_found_does_no_preparation_sql(db_engine, monkeypatch):
    family = await _family(db_engine)
    _found(monkeypatch, [])
    with _preparation_sql(db_engine, monkeypatch) as statements:
        assert await scan_once(family.maker, Sender({}), NOW, lease=None) == 0
    assert statements == []


async def test_one_fully_new_key_is_delivered_with_measured_preparation_cost(db_engine, monkeypatch):
    family = await _family(db_engine)
    fresh = _alert(family)
    _found(monkeypatch, [fresh])
    sender = Sender({fresh.subject_id: (fresh, NOW + timedelta(hours=6))})
    with _preparation_sql(db_engine, monkeypatch) as statements:
        assert await scan_once(family.maker, sender, NOW, lease=None) == 1
    assert sender.subjects == [fresh.subject_id]
    persisted = await _rows(family)
    assert len(persisted) == 1 and persisted[fresh.subject_id]["state"] == "sent"
    print(f"fully-new key preparation SQL: {len(statements)}")


async def test_501_existing_fences_need_one_publication_and_two_reads(db_engine, monkeypatch):
    family = await _family(db_engine)
    known = [_alert(family) for _ in range(501)]
    await _store(family, [_fence(alert) for alert in known])
    _found(monkeypatch, known)
    with _preparation_sql(db_engine, monkeypatch) as statements:
        assert await scan_once(family.maker, Sender({}), NOW, lease=None) == 0
    assert len(await _rows(family)) == 501
    assert len(statements) == 3, "known fences must not repeat consent/ON CONFLICT per event"
    assert statements.count(PUBLICATION_SQL) == 1
    assert all(statement.startswith("SELECT ") for statement in statements)
