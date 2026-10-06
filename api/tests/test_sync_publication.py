"""Commit publication regressions on real PostgreSQL with observable barriers."""

import asyncio
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import pairing, sync
from app.api.deps import Caller
from app.core.security import read_access_token
from app.db.models import Account, DoseEvent, Medication, Schedule
from tests.conftest import auth_header
from tests.test_sync import _owner_with_data

pytestmark = pytest.mark.asyncio


async def _waiting(task, engine, pid):
    """Postgres proves the wait; task scheduling alone does not."""
    async with engine.connect() as monitor:
        for _ in range(200):
            if task.done():
                task.result()
                return False
            waiting = (await monitor.execute(text(
                "SELECT wait_event_type = 'Lock' AND wait_event = 'advisory' "
                "FROM pg_stat_activity WHERE pid = :pid"
            ), {"pid": pid})).scalar_one()
            if waiting:
                return True
            await asyncio.sleep(0.01)
    return False


async def _pid(session):
    return (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()


def _device(api, owner):
    return uuid.UUID(read_access_token(api.app.state.settings.jwt_secret,
                                      owner["access_token"])["did"])


async def _page(maker, owner, dev, cursor=None):
    async with maker() as reader:
        caller = Caller(await reader.get(Account, uuid.UUID(owner["account_id"])), dev)
        page, reached = await sync._page(reader, caller, cursor, None)
        await reader.commit()
        return page, reached


@pytest.mark.parametrize("mutation", ["schedule_update", "schedule_delete", "dose_action"])
async def test_a_late_transaction_cannot_be_skipped_by_a_later_writer(api, db_engine, mutation):
    owner, pid, mid, sid, did = await _owner_with_data(api)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    dev = _device(api, owner)
    from app.db.models import Profile
    async with maker() as seed:
        other_pid = uuid.uuid4()
        seed.add(Profile(id=other_pid, owner_account_id=uuid.UUID(owner["account_id"]),
                         name="Independent", created_at_ms=1, updated_at_ms=1))
        await seed.commit()
    before, _ = await _page(maker, owner, dev)
    async with maker() as first, maker() as second:
        common = {"created_at_ms": 1, "updated_at_ms": 9_000_000_000_000,
                  "deleted_at_ms": None, "origin_device_id": dev, "op_seq": 900}
        if mutation == "dose_action":
            values = {**common, "id": uuid.UUID(did), "profile_id": uuid.UUID(pid),
                      "medication_id": uuid.UUID(mid), "schedule_id": uuid.UUID(sid),
                      "planned_at_ms": 1, "status": "taken", "action_at_ms": 2,
                      "dose_amount": 1.0, "snooze_count": 0}
            model, entity, wanted_id = DoseEvent, "dose_events", did
        else:
            values = {**common, "id": uuid.UUID(sid), "medication_id": uuid.UUID(mid),
                      "type": "fixed_times", "times": '["10:00"]',
                      "start_date": "2026-08-01"}
            if mutation == "schedule_delete":
                values["deleted_at_ms"] = common["updated_at_ms"]
            model, entity, wanted_id = Schedule, "schedules", sid
        await sync._upsert(first, model, values)
        lower = (await first.execute(select(model.server_seq)
                                     .where(model.id == uuid.UUID(wanted_id)))).scalar_one()
        pid_second = await _pid(second)

        async def write_second():
            # A different production writer, not another call to push.
            await pairing._resend_profile(second, other_pid)
            await second.commit()

        later = asyncio.create_task(write_second())
        try:
            assert await _waiting(later, db_engine, pid_second), (
                "a later writer committed past the unpublished lower sequence"
            )
            await first.commit()
            await asyncio.wait_for(later, 5)
        finally:
            await first.rollback()
            await asyncio.gather(later, return_exceptions=True)
            await second.rollback()
    after, reached = await _page(maker, owner, dev, before.cursor)
    assert reached >= lower
    assert wanted_id in {row["id"] for row in after.changes[entity]}
    if mutation == "schedule_delete":
        assert after.changes[entity][0]["deleted_at"] == values["deleted_at_ms"]


async def test_pull_waits_for_an_unpublished_write_without_a_second_writer(api, db_engine):
    owner, pid, mid, sid, did = await _owner_with_data(api)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as writer, maker() as reader:
        reader_pid = await _pid(reader)
        caller = Caller(await reader.get(Account, uuid.UUID(owner["account_id"])),
                        _device(api, owner))
        await writer.execute(update(Schedule).where(Schedule.id == uuid.UUID(sid))
                             .values(times='["11:00"]', server_seq=text("nextval('server_seq')")))
        # Isolate the page fence: HTTP now waits at the authentication fence
        # first. Calling the real page preserves the original regression proof.
        pulling = asyncio.create_task(sync._page(reader, caller, None, None))
        try:
            assert await _waiting(pulling, db_engine, reader_pid), (
                "pull returned while a sequence predecessor was uncommitted"
            )
            await writer.commit()
            page, _reached = await asyncio.wait_for(pulling, 5)
            assert page.changes["schedules"][0]["times"] == '["11:00"]'
        finally:
            await writer.rollback()
            if not pulling.done():
                pulling.cancel()
            await asyncio.gather(pulling, return_exceptions=True)


async def test_a_page_does_not_mix_medication_ids_with_newer_feed_keys(api, db_engine, monkeypatch):
    owner, pid, mid, sid, did = await _owner_with_data(api)
    entered, release = asyncio.Event(), asyncio.Event()
    original_feed = sync.feed_query
    original_execute = sync.AsyncSession.execute

    def observed_feed(*args):
        entered.set()
        return original_feed(*args)

    async def paused_execute(session, statement, *args, **kwargs):
        if entered.is_set() and not release.is_set() and getattr(statement, "is_select", False):
            await release.wait()
        return await original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(sync, "feed_query", observed_feed)
    monkeypatch.setattr(sync.AsyncSession, "execute", paused_execute)
    reading = asyncio.create_task(api.get("/v1/sync/pull", headers=auth_header(owner)))
    await entered.wait()
    monkeypatch.setattr(sync.AsyncSession, "execute", original_execute)
    maker = async_sessionmaker(db_engine)
    new_mid, new_sid = uuid.uuid4(), uuid.uuid4()
    async with maker() as writer:
        writer_pid = await _pid(writer)

        async def add_child():
            writer.add(Medication(id=new_mid, profile_id=uuid.UUID(pid), name="New",
                                  form="tablet", created_at_ms=2, updated_at_ms=2))
            await writer.flush()
            writer.add(Schedule(id=new_sid, medication_id=new_mid, type="fixed_times",
                                times='["12:00"]', start_date="2026-08-01",
                                created_at_ms=2, updated_at_ms=2))
            await writer.commit()

        writing = asyncio.create_task(add_child())
        try:
            assert await _waiting(writing, db_engine, writer_pid), (
                "a child committed after the page had frozen its medication ids"
            )
            release.set()
            first = await asyncio.wait_for(reading, 5)
            await asyncio.wait_for(writing, 5)
        finally:
            release.set()
            await asyncio.gather(reading, return_exceptions=True)
            await writer.rollback()
            await asyncio.gather(writing, return_exceptions=True)
    assert first.status_code == 200, first.text
    second = await api.get("/v1/sync/pull", headers=auth_header(owner),
                           params={"cursor": first.json()["cursor"]})
    assert second.status_code == 200, second.text
    assert str(new_sid) in {row["id"] for row in second.json()["changes"]["schedules"]}


@pytest.mark.parametrize("entity", ["profiles", "medications", "schedules", "dose_events", "stock_events"])
async def test_every_mirror_default_waits_before_allocating_a_sequence(api, db_engine, entity):
    from app.db.models import Profile, StockEvent
    owner, pid, mid, sid, did = await _owner_with_data(api)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    common = {"id": uuid.uuid4(), "created_at_ms": 2, "updated_at_ms": 2}
    row = {
        "profiles": lambda: Profile(**common, owner_account_id=uuid.UUID(owner["account_id"]), name="Other"),
        "medications": lambda: Medication(**common, profile_id=uuid.UUID(pid), name="Other", form="tablet"),
        "schedules": lambda: Schedule(**common, medication_id=uuid.UUID(mid), type="fixed_times",
                                      times='["13:00"]', start_date="2026-08-01"),
        "dose_events": lambda: DoseEvent(**common, profile_id=uuid.UUID(pid), medication_id=uuid.UUID(mid),
                                         planned_at_ms=3, status="pending", dose_amount=1.0),
        "stock_events": lambda: StockEvent(**common, medication_id=uuid.UUID(mid), delta=1.0, reason="manual"),
    }[entity]()
    async with maker() as first, maker() as second:
        await first.execute(update(Schedule).where(Schedule.id == uuid.UUID(sid))
                            .values(server_seq=text("nextval('server_seq')")))
        allocated = (await first.execute(text("SELECT last_value FROM server_seq"))).scalar_one()
        second_pid = await _pid(second)

        async def default_write():
            second.add(row)
            await second.flush()
            await second.commit()

        writing = asyncio.create_task(default_write())
        try:
            assert await _waiting(writing, db_engine, second_pid)
            async with db_engine.connect() as monitor:
                assert (await monitor.execute(text("SELECT last_value FROM server_seq"))).scalar_one() == allocated
            await first.commit()
            await asyncio.wait_for(writing, 5)
        finally:
            await first.rollback()
            await asyncio.gather(writing, return_exceptions=True)
            await second.rollback()
        assert row.server_seq > allocated


async def test_rollback_releases_the_fence_and_paging_crosses_sequence_holes(api, db_engine, monkeypatch):
    from app.db.models import StockEvent
    owner, pid, mid, sid, did = await _owner_with_data(api)
    dev, maker = _device(api, owner), async_sessionmaker(db_engine, expire_on_commit=False)
    before, _ = await _page(maker, owner, dev)
    monkeypatch.setattr(sync, "PAGE_SIZE", 2)
    new_ids = {uuid.uuid4() for _ in range(5)}
    async with maker() as first, maker() as second:
        await first.execute(update(Schedule).where(Schedule.id == uuid.UUID(sid))
                            .values(times='["14:00"]', server_seq=text("nextval('server_seq')")))
        second_pid = await _pid(second)

        async def write_stock():
            second.add_all([StockEvent(id=row_id, medication_id=uuid.UUID(mid), delta=1,
                                      reason="manual", created_at_ms=3, updated_at_ms=3)
                            for row_id in new_ids])
            await second.commit()

        writing = asyncio.create_task(write_stock())
        try:
            assert await _waiting(writing, db_engine, second_pid)
            await first.rollback()
            await asyncio.wait_for(writing, 5)
        finally:
            await first.rollback()
            await asyncio.gather(writing, return_exceptions=True)
            await second.rollback()
    cursor, seen = before.cursor, set()
    for _ in range(4):
        page, reached = await _page(maker, owner, dev, cursor)
        assert "schedules" not in page.changes
        seen |= {uuid.UUID(row["id"]) for row in page.changes.get("stock_events", [])}
        assert sync.decode_cursor(page.cursor) >= sync.decode_cursor(cursor)
        cursor = page.cursor
        if not page.has_more:
            break
    assert seen == new_ids
    empty, reached = await _page(maker, owner, dev, cursor)
    assert empty.changes == {} and empty.cursor == cursor


async def test_pull_and_logout_take_publication_before_the_device_row(api, db_engine, monkeypatch):
    owner, pid, mid, sid, did = await _owner_with_data(api)
    entered, release = asyncio.Event(), asyncio.Event()
    original_page = sync._page

    async def paused_page(session, *args):
        page = await original_page(session, *args)
        entered.set()
        await release.wait()
        return page

    monkeypatch.setattr(sync, "_page", paused_page)
    pulling = asyncio.create_task(api.get("/v1/sync/pull", headers=auth_header(owner)))
    await entered.wait()
    # Real logout on its own connection: it begins at RefreshToken, then flushes
    # Device and clears authority. No Device row lock may precede publication.
    from app.api import auth
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as logging_out:
        logout_pid = await _pid(logging_out)
        caller = Caller(await logging_out.get(Account, uuid.UUID(owner["account_id"])), _device(api, owner))
        leaving = asyncio.create_task(auth.logout(auth.LogoutIn(reminders_stopped=True), caller, logging_out))
        try:
            assert await _waiting(leaving, db_engine, logout_pid)
            release.set()
            result = await asyncio.wait_for(pulling, 5)
            await asyncio.wait_for(leaving, 5)
            assert result.status_code == 200, result.text
        finally:
            release.set()
            await asyncio.gather(pulling, leaving, return_exceptions=True)
    # A fresh request sees revocation; the prior request committed its cursor.
    denied = await api.get("/v1/sync/pull", headers=auth_header(owner))
    assert denied.status_code == 401


async def test_slow_fcm_does_not_directly_hold_publication_and_cascade_wait_is_bounded(api, db_engine):
    from datetime import timedelta
    from sqlalchemy.exc import DBAPIError
    from app.api.auth import _erase_account
    from app.db.models import AlertDelivery, AlertKind, Device, Profile, utcnow
    from app.services.push import Delivery
    from app.worker import scan_once

    owner, pid, mid, sid, did = await _owner_with_data(api)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    now, entered, release = utcnow(), asyncio.Event(), asyncio.Event()
    async with maker() as seed:
        await seed.execute(update(Device).where(Device.id == _device(api, owner)).values(push_token="synthetic-fcm"))
        seed.add(AlertDelivery(account_id=uuid.UUID(owner["account_id"]), profile_id=uuid.UUID(pid),
                               kind=AlertKind.dose_missed, subject_id=did, state="pending", attempts=0,
                               next_attempt_at=now, expires_at=now + timedelta(hours=1)))
        await seed.commit()

    class SlowPush:
        async def send(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return Delivery.ok

    sending = asyncio.create_task(scan_once(maker, SlowPush(), now=now))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        # Proves only absence of a direct lock across the network call.
        page = await asyncio.wait_for(api.get("/v1/sync/pull", headers=auth_header(owner)), 2)
        assert page.status_code == 200, page.text
        # A profile/account FK cascade really waits for the in-flight delivery.
        # Its publication transaction times out and rolls back as a whole.
        async with maker() as deleting:
            with pytest.raises(DBAPIError) as failed:
                await asyncio.wait_for(_erase_account(deleting, uuid.UUID(owner["account_id"])), 7)
            assert sync._sqlstate(failed.value) == "55P03"
            await deleting.rollback()
        async with maker() as check:
            assert await check.get(Account, uuid.UUID(owner["account_id"])) is not None
            assert await check.get(Profile, uuid.UUID(pid)) is not None
        # Publication debt was released even though FCM is still outstanding.
        again = await asyncio.wait_for(api.get("/v1/sync/pull", headers=auth_header(owner)), 2)
        assert again.status_code == 200, again.text
    finally:
        release.set()
        await asyncio.wait_for(sending, 5)
    async with maker() as retry:
        await _erase_account(retry, uuid.UUID(owner["account_id"]))
    async with maker() as check:
        assert await check.get(Account, uuid.UUID(owner["account_id"])) is None


async def test_a_timed_out_push_acknowledges_nothing_and_the_same_batch_can_retry(api, db_engine):
    from httpx import ASGITransport, AsyncClient
    from app.db.publication import lock_publication
    from tests.test_sync import schedule
    owner, pid, mid, sid, did = await _owner_with_data(api)
    payload = {"changes": {"schedules": [{**schedule(sid, mid, at=9_000_000_000_000),
                                          "times": '["15:00"]'}]}}
    async with async_sessionmaker(db_engine)() as blocker:
        await lock_publication(blocker)
        # The normal HTTP boundary returns its existing internal_error envelope,
        # so clients keep the unacknowledged batch. Nothing is partially stored.
        async with AsyncClient(transport=ASGITransport(app=api.app, raise_app_exceptions=False),
                               base_url="http://test") as http:
            failed = await asyncio.wait_for(http.post("/v1/sync/push", headers=auth_header(owner),
                                                     json=payload), 7)
        assert failed.status_code == 500
        assert failed.json()["error"]["code"] == "internal_error"
        await blocker.rollback()
    async with async_sessionmaker(db_engine)() as check:
        assert (await check.get(Schedule, uuid.UUID(sid))).times != '["15:00"]'
    retry = await api.post("/v1/sync/push", headers=auth_header(owner), json=payload)
    assert retry.status_code == 200, retry.text
    assert retry.json()["retry"] == []
    async with async_sessionmaker(db_engine)() as check:
        assert (await check.get(Schedule, uuid.UUID(sid))).times == '["15:00"]'


async def test_push_returns_its_committed_frontier_before_the_next_writer_allocates(api, db_engine, monkeypatch):
    from tests.test_sync import schedule
    owner, pid, mid, sid, did = await _owner_with_data(api)
    committed, allocated, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    maker = async_sessionmaker(db_engine)
    original_commit = sync.AsyncSession.commit
    next_seq = None
    push_session = None
    original_lock = sync.lock_publication

    async def observe_push(session):
        nonlocal push_session
        push_session = session
        await original_lock(session)

    async def after_commit(session):
        await original_commit(session)
        if session is push_session and not committed.is_set():
            committed.set()
            await allocated.wait()

    async def successor():
        nonlocal next_seq
        await committed.wait()
        async with maker() as writer:
            await writer.execute(update(Schedule).where(Schedule.id == uuid.UUID(sid))
                                 .values(server_seq=text("nextval('server_seq')")))
            next_seq = (await writer.execute(select(Schedule.server_seq)
                                            .where(Schedule.id == uuid.UUID(sid)))).scalar_one()
            allocated.set()
            await release.wait()
            await writer.commit()

    # Authentication commits before entering push. Observe its actual first
    # fence, then pause only that push commit after publication is released.
    monkeypatch.setattr(sync, "lock_publication", observe_push)
    monkeypatch.setattr(sync.AsyncSession, "commit", after_commit)
    next_writer = asyncio.create_task(successor())
    try:
        result = await asyncio.wait_for(api.post("/v1/sync/push", headers=auth_header(owner),
                                                json={"changes": {"schedules": [
                                                    schedule(sid, mid, at=9_000_000_000_000)
                                                ]}}), 5)
        assert result.status_code == 200, result.text
        assert sync.decode_cursor(result.json()["cursor"]) < next_seq
    finally:
        release.set()
        await asyncio.wait_for(next_writer, 5)


async def test_preview_releases_publication_before_returning_with_its_session_still_open(api, db_engine):
    from types import SimpleNamespace
    from app.db.models import StockEvent
    owner, pid, mid, sid, did = await _owner_with_data(api)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as reading, maker() as writer:
        caller = Caller(await reading.get(Account, uuid.UUID(owner["account_id"])), _device(api, owner))
        request = SimpleNamespace(app=api.app)
        page = await sync.preview(request=request, caller=caller, session=reading, cursor=None)
        assert page.changes
        assert not reading.in_transaction()
        writer.add(StockEvent(id=uuid.uuid4(), medication_id=uuid.UUID(mid), delta=1,
                              reason="manual", created_at_ms=4, updated_at_ms=4))
        await asyncio.wait_for(writer.commit(), 2)
