"""Recovery rehearsals on newly created PostgreSQL databases, never the test DB.

A genuine 0020 schema permits the old commit gap. 0021 must re-offer those rows
without changing LWW, tombstones, canonical-dose adoption or any identifiers.
"""

import asyncio
import os
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import sync
from app.api.deps import Caller
from app.db.models import (
    Account, Device, DeviceReadiness, DoseEvent, Medication, Profile, Schedule,
    StockEvent, utcnow,
)
from app.db.publication import MIRROR_TABLES, PUBLICATION_TABLES
from app.db.schema_check import _publication_problems
from tests.conftest import TEST_DATABASE_URL, TEST_KEY

pytestmark = pytest.mark.asyncio


async def _alembic(url, revision, succeeds=True):
    env = {**os.environ, "ENVIRONMENT": "local", "DATABASE_URL": url,
           "REDIS_URL": "redis://localhost:6379/0", "ENCRYPTION_KEY": TEST_KEY,
           "JWT_SECRET": "synthetic-migration-secret-123456"}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "alembic", "upgrade", revision,
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert (process.returncode == 0) == succeeds, output.decode()
    return output.decode()


@pytest_asyncio.fixture
async def migration_db(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", TEST_KEY)
    # Identifiers are generated here, not derived from external input.
    name = "sonadose_publication_" + uuid.uuid4().hex
    url = make_url(TEST_DATABASE_URL)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    target = url.set(database=name).render_as_string(hide_password=False)
    engine = create_async_engine(target)
    try:
        yield target, engine
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE "{name}"'))
        await admin.dispose()


async def _rows(engine):
    async with engine.connect() as connection:
        result = {}
        for table in MIRROR_TABLES:
            result[table] = {
                row["id"]: dict(row)
                for row in (await connection.execute(text(f"SELECT * FROM {table}"))).mappings()
            }
        return result


@pytest.mark.parametrize("fail_once", [False, True])
async def test_upgrade_republishes_skipped_rows_and_retry_is_atomic(migration_db, fail_once):
    url, engine = migration_db
    await _alembic(url, "0020")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    aid, dev, pid, mid, sid, winner_id, loser_id, stock_id = (uuid.uuid4() for _ in range(8))
    allocation = text("nextval('server_seq')")
    async with maker() as seed:
        seed.add(Account(id=aid, google_sub="synthetic-migration", created_at=utcnow()))
        await seed.flush()
        # This is the actual old schema; the current Device ORM has columns
        # introduced later and must not be used to manufacture a 0020 fixture.
        await seed.execute(text(
            "INSERT INTO devices (id, account_id, platform, cursor_seq, "
            "adopts_doses_at, ready_protocol_at, created_at) "
            "VALUES (:id, :account, 'android', :cursor, :now, :now, :now)"
        ), {"id": dev, "account": aid, "cursor": 2**63 - 1, "now": utcnow()})
        await seed.flush()
        seed.add(Profile(id=pid, owner_account_id=aid, owner_device_id=dev, name="Synthetic",
                         created_at_ms=1, updated_at_ms=1, origin_device_id=dev, op_seq=1,
                         server_seq=allocation))
        await seed.flush()
        seed.add(Medication(id=mid, profile_id=pid, name="Synthetic", form="tablet",
                            created_at_ms=1, updated_at_ms=1, origin_device_id=dev, op_seq=2,
                            server_seq=allocation))
        await seed.flush()
        seed.add(Schedule(id=sid, medication_id=mid, type="fixed_times", times='["09:00"]',
                          start_date="2026-08-01", created_at_ms=1, updated_at_ms=1,
                          origin_device_id=dev, op_seq=3, server_seq=allocation))
        await seed.flush()
        seed.add(DoseEvent(id=winner_id, profile_id=pid, medication_id=mid, schedule_id=sid,
                           planned_at_ms=5, status="pending", dose_amount=1, dose_key=f"{sid}:5",
                           created_at_ms=1, updated_at_ms=1, origin_device_id=dev, op_seq=4,
                           server_seq=allocation))
        await seed.flush()
        seed.add(DoseEvent(id=loser_id, profile_id=pid, medication_id=mid, schedule_id=sid,
                           planned_at_ms=5, status="taken", dose_amount=1, dose_key=f"{sid}:5",
                           superseded_by=winner_id, created_at_ms=1, updated_at_ms=2,
                           origin_device_id=dev, op_seq=5, server_seq=allocation))
        seed.add(StockEvent(id=stock_id, medication_id=mid, delta=-1, reason="dose_taken",
                            dose_event_id=loser_id, created_at_ms=1, updated_at_ms=1,
                            origin_device_id=dev, op_seq=6, server_seq=allocation))
        seed.add(DeviceReadiness(device_id=dev, profile_id=pid, revision=2**63 - 1,
                                 applied_cursor=2**63 - 1, updated_at=utcnow()))
        await seed.commit()
    # The real old failure: lower schedule tombstone commits after a higher row
    # was already visible and a valid server-issued cursor advanced past it.
    async with maker() as first, maker() as second:
        await first.execute(text(
            "UPDATE schedules SET deleted_at_ms=7, updated_at_ms=7, op_seq=7, "
            "server_seq=nextval('server_seq') WHERE id=:id"
        ), {"id": sid})
        await second.execute(text(
            "UPDATE medications SET server_seq=nextval('server_seq') WHERE id=:id"
        ), {"id": mid})
        await second.commit()
        old_cursor = (await second.execute(text("SELECT last_value FROM server_seq"))).scalar_one()
        await first.commit()
    before = await _rows(engine)
    assert before["schedules"][sid]["server_seq"] < old_cursor
    async with engine.begin() as connection:
        # Explicitly normalised by 0021, independently checked by schema_check.
        await connection.execute(text("ALTER SEQUENCE server_seq CACHE 8 INCREMENT BY 2 CYCLE"))
        if fail_once:
            await connection.execute(text("""
                CREATE FUNCTION reject_republish() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'synthetic migration interruption'; END; $$
            """))
            await connection.execute(text("""
                CREATE TRIGGER reject_republish BEFORE UPDATE ON stock_events
                FOR EACH ROW EXECUTE FUNCTION reject_republish()
            """))
    if fail_once:
        await _alembic(url, "head", succeeds=False)
        assert await _rows(engine) == before
        async with engine.begin() as connection:
            assert (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0020"
            assert (await connection.execute(text(
                "SELECT count(*) FROM pg_trigger WHERE tgname LIKE '%_sync_publication'"
            ))).scalar_one() == 0
            await connection.execute(text("DROP TRIGGER reject_republish ON stock_events"))
            await connection.execute(text("DROP FUNCTION reject_republish()"))
    await _alembic(url, "head")
    after = await _rows(engine)
    for table in MIRROR_TABLES:
        assert before[table].keys() == after[table].keys()
        for row_id, row in after[table].items():
            assert old_cursor < row["server_seq"] < 2**63 - 1
            assert {key: value for key, value in row.items() if key != "server_seq"} == {
                key: value for key, value in before[table][row_id].items() if key != "server_seq"
            }
    async with engine.connect() as connection:
        assert await _publication_problems(connection) == []
        # Every application table participates, except the delivery queue whose
        # row is held across FCM. New tables require an explicit lock decision.
        tables = set((await connection.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ))).scalars()) - {"alembic_version", "alert_deliveries"}
        assert tables == set(PUBLICATION_TABLES)
        assert (await connection.execute(text("SELECT cursor_seq FROM devices WHERE id=:id"),
                                         {"id": dev})).scalar_one() == 2**63 - 1
    # A restarted old client with a legitimate skipped cursor receives every
    # retained row, including the tombstone and canonical/adoption markers.
    async with maker() as reader:
        caller = Caller(await reader.get(Account, aid), dev)
        page, reached = await sync._page(reader, caller, sync.encode_cursor(old_cursor), None)
        assert set(page.changes) == set(MIRROR_TABLES)
        assert page.changes["schedules"][0]["deleted_at"] == 7
        assert {row["id"] for row in page.changes["dose_events"]} == {str(winner_id), str(loser_id)}
        assert reached > old_cursor
    await _alembic(url, "head")
    assert await _rows(engine) == after  # Repeated upgrade does not republish again.
