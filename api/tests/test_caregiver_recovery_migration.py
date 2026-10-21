"""0023 fixtures upgraded transactionally to the actual additive 0024 schema."""

import asyncio
import json
import os
import sys
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Account, AlertDelivery, AlertKind, CaregiverRecoveryEvent, Profile, ProfileMembership, Role, utcnow
from app.db.schema_check import _caregiver_recovery_problems
from app.services import alerts
from app.services.caregiver_recovery import enable_coverage
from tests.test_credential_generation_migration import _schema_check
from tests.test_sync_publication_migration import _alembic, migration_db  # noqa: F401
from tests.conftest import TEST_KEY

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("interrupted", [False, True])
async def test_real_0023_fixture_preserved_disabled_no_backfill_and_repeat_upgrade(migration_db, interrupted):
    url, engine = migration_db
    await _alembic(url, "0023")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    owner, recipient, pid = (uuid.uuid4() for _ in range(3))
    now = utcnow()
    async with maker() as session:
        session.add_all([Account(id=owner, google_sub="r1-owner"), Account(id=recipient, google_sub="r1-recipient")])
        await session.flush()
        session.add(Profile(id=pid, owner_account_id=owner, name="Synthetic", created_at_ms=1, updated_at_ms=1))
        await session.flush()
        session.add(ProfileMembership(account_id=recipient, profile_id=pid, role=Role.with_alerts))
        await session.flush()
        legacy = alerts.Alert(recipient, pid, AlertKind.dose_missed, str(uuid.uuid4()))
        assert await alerts.claim(session, legacy, now)
        await session.commit()
    async with engine.connect() as conn:
        before = [dict(row) for row in (await conn.execute(text("SELECT * FROM alert_deliveries ORDER BY id"))).mappings()]
    if interrupted:
        async with engine.begin() as conn:
            await conn.execute(text("""CREATE FUNCTION reject_recovery_upgrade() RETURNS event_trigger
                LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic migration interruption'; END; $$"""))
            await conn.execute(text("""CREATE EVENT TRIGGER reject_recovery_upgrade ON ddl_command_start
                WHEN TAG IN ('CREATE INDEX') EXECUTE FUNCTION reject_recovery_upgrade()"""))
        await _alembic(url, "head", succeeds=False)
        async with engine.begin() as conn:
            assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0023"
            for name in ("caregiver_recovery_control", "caregiver_recovery_events", "caregiver_event_seq"):
                assert (await conn.execute(text("SELECT to_regclass(:name)"), {"name": name})).scalar_one() is None
            assert [dict(row) for row in (await conn.execute(text("SELECT * FROM alert_deliveries ORDER BY id"))).mappings()] == before
            await conn.execute(text("DROP EVENT TRIGGER reject_recovery_upgrade"))
            await conn.execute(text("DROP FUNCTION reject_recovery_upgrade()"))
    await _alembic(url, "head")
    await _schema_check(url)

    await _alembic(url, "head")
    async with engine.connect() as conn:
        assert [dict(row) for row in (await conn.execute(text("SELECT * FROM alert_deliveries ORDER BY id"))).mappings()] == before
        assert await _caregiver_recovery_problems(conn) == []
        control = (await conn.execute(text("SELECT * FROM caregiver_recovery_control"))).mappings().one()
        assert not control["producer_enabled"] and not control["reader_enabled"] and control["epoch"] is None
        assert (await conn.execute(text("SELECT count(*) FROM caregiver_recovery_events"))).scalar_one() == 0
    async with maker() as session:
        await enable_coverage(session, now, writers_verified=True)
        assert not await alerts.claim(session, legacy, now)
        assert (await session.execute(select(CaregiverRecoveryEvent))).scalar_one_or_none() is None
        assert await alerts.claim(session, alerts.Alert(recipient, pid, AlertKind.profile_stale, now.date().isoformat()), now)
        await session.commit()
        row = (await session.execute(select(CaregiverRecoveryEvent))).scalar_one()
        assert row.event_seq == 1 and row.raised_at == now
        # A sent parent is still retired by the migrated lifecycle guards.
        await session.execute(text("UPDATE alert_deliveries SET state='sent' WHERE id=:id"), {"id": row.delivery_id})
        await session.execute(text("UPDATE profile_memberships SET role='viewer' WHERE profile_id=:pid"), {"pid": pid})
        await session.commit()
        await session.refresh(row)
        assert row.retired_at is not None
        await session.execute(text("UPDATE profile_memberships SET role='with_alerts' WHERE profile_id=:pid"), {"pid": pid})
        await session.commit()
        await session.refresh(row)
        assert row.retired_at is not None
    await _schema_check(url)


async def test_operator_enable_is_explicit_schema_checked_and_close_reenable_changes_epoch(migration_db):
    url, _ = migration_db
    await _alembic(url, "head")
    env = {**os.environ, "ENVIRONMENT": "local", "DATABASE_URL": url,
           "REDIS_URL": "redis://localhost:6379/0", "ENCRYPTION_KEY": TEST_KEY,
           "JWT_SECRET": "synthetic-r1-operator-secret-123456"}
    async def run(*args, succeeds=True):
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "tools.caregiver_recovery",
            *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await process.communicate()
        assert (process.returncode == 0) == succeeds, err.decode()
        return json.loads(out) if succeeds else None
    initial = await run("status")
    assert not initial["producer_enabled"] and initial["committed_frontier"] == "0"
    await run("enable", succeeds=False)
    assert await run("status") == initial
    first = await run("enable", "--all-writers-upgraded")
    assert first["producer_enabled"] and not first["reader_enabled"]
    assert await run("enable", "--all-writers-upgraded") == first
    await run("reader-enable", "--all-writers-upgraded", succeeds=False)  # no key
    assert not (await run("status"))["reader_enabled"]
    env["CAREGIVER_CURSOR_SIGNING_KEY"] = "cnJycnJycnJycnJycnJycnJycnJycnJycnJycnJycnI"
    await run("reader-enable", succeeds=False)
    reading = await run("reader-enable", "--all-writers-upgraded")
    assert reading["reader_enabled"] and reading["epoch"] == first["epoch"]
    paused = await run("reader-disable")
    assert not paused["reader_enabled"] and paused["epoch"] == first["epoch"]
    closed = await run("close")
    assert not closed["producer_enabled"] and closed["closed_at"] is not None
    await run("reader-enable", "--all-writers-upgraded", succeeds=False)
    second = await run("enable", "--all-writers-upgraded")
    assert second["epoch"] != first["epoch"] and not second["reader_enabled"]

