"""Consent retirement upgrades real 0022 queue debt atomically."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Account, AlertDelivery, AlertKind, Profile, ProfileMembership, Role, utcnow
from app.db.schema_check import _alert_eligibility_problems
from tests.test_credential_generation_migration import _schema_check
from tests.test_sync_publication_migration import _alembic, migration_db  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _queue(engine):
    async with engine.connect() as conn:
        return {row["id"]: dict(row) for row in (
            await conn.execute(text("SELECT * FROM alert_deliveries"))
        ).mappings()}


@pytest.mark.parametrize("fail_once", [False, True])
async def test_upgrade_retires_only_ineligible_caregiver_debt_and_retries_atomically(migration_db, fail_once):
    url, engine = migration_db
    await _alembic(url, "0022")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    now = utcnow()
    rows, eligible_profile, eligible_recipient = {}, None, None
    async with maker() as seed:
        for case in ("eligible", "revoked", "viewer", "profile_deleted", "recipient_deleted",
                     "owner_deleted", "no_membership", "already_sent", "nudge"):
            owner, recipient, pid, delivery_id = (uuid.uuid4() for _ in range(4))
            seed.add_all([
                Account(id=owner, google_sub=f"owner-{case}",
                        deleted_at=now if case == "owner_deleted" else None),
                Account(id=recipient, google_sub=f"recipient-{case}",
                        deleted_at=now if case == "recipient_deleted" else None),
            ])
            await seed.flush()
            seed.add(Profile(id=pid, owner_account_id=owner, name="Synthetic",
                             created_at_ms=1, updated_at_ms=1,
                             deleted_at_ms=2 if case == "profile_deleted" else None))
            await seed.flush()
            if case != "no_membership":
                seed.add(ProfileMembership(profile_id=pid, account_id=recipient,
                                           role=Role.viewer if case == "viewer" else Role.with_alerts,
                                           revoked_at=now if case == "revoked" else None))
            seed.add(AlertDelivery(
                id=delivery_id, account_id=recipient, profile_id=pid,
                kind=AlertKind.reminder_authority_lost if case == "nudge" else AlertKind.dose_missed,
                subject_id=f"synthetic-{case}", state="sent" if case == "already_sent" else "pending",
                attempts=1, next_attempt_at=now + timedelta(minutes=5),
                expires_at=now + timedelta(hours=1), sent_at=now if case == "already_sent" else None,
                last_error="retry",
            ))
            rows[case] = delivery_id
            if case == "eligible":
                eligible_profile, eligible_recipient = pid, recipient
        await seed.commit()
    before = await _queue(engine)
    if fail_once:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE FUNCTION reject_consent_upgrade() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'synthetic migration interruption'; END; $$
            """))
            await conn.execute(text("""
                CREATE TRIGGER reject_consent_upgrade BEFORE UPDATE ON alert_deliveries
                FOR EACH ROW EXECUTE FUNCTION reject_consent_upgrade()
            """))
        await _alembic(url, "head", succeeds=False)
        assert await _queue(engine) == before
        async with engine.begin() as conn:
            assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0022"
            assert (await conn.execute(text(
                "SELECT to_regprocedure('retire_caregiver_deliveries()')"
            ))).scalar_one() is None
            await conn.execute(text("DROP TRIGGER reject_consent_upgrade ON alert_deliveries"))
            await conn.execute(text("DROP FUNCTION reject_consent_upgrade()"))
    await _alembic(url, "head")
    after = await _queue(engine)
    for case, delivery_id in rows.items():
        expected = dict(before[delivery_id])
        if case not in ("eligible", "already_sent", "nudge"):
            expected["state"] = "expired"
        assert after[delivery_id] == expected, case
    async with engine.connect() as conn:
        assert await _alert_eligibility_problems(conn) == []
    await _schema_check(url)
    await _alembic(url, "head")
    assert await _queue(engine) == after
    # The newly installed trigger protects non-due debt before regrant. The
    # same migration schema must retain the old unique intent as expired.
    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE profile_memberships SET revoked_at=:now WHERE profile_id=:pid AND account_id=:aid"
        ), {"now": now, "pid": eligible_profile, "aid": eligible_recipient})
    async with maker() as grant:
        grant.add(ProfileMembership(profile_id=eligible_profile, account_id=eligible_recipient,
                                    role=Role.with_alerts))
        await grant.commit()
        assert (await grant.get(AlertDelivery, rows["eligible"])).state == "expired"
    # Check detects disabled guards and changed function bodies, not just names.
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE profiles DISABLE TRIGGER profile_alerts_retire_delete"))
        assert await _alert_eligibility_problems(conn) == [
            "profiles: caregiver retirement trigger profile_alerts_retire_delete is missing or changed"
        ]
        await conn.execute(text("ALTER TABLE profiles ENABLE TRIGGER profile_alerts_retire_delete"))
        await conn.execute(text("""
            CREATE OR REPLACE FUNCTION retire_caregiver_deliveries() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END; $$
        """))
        assert await _alert_eligibility_problems(conn) == [
            "function retire_caregiver_deliveries() is missing or changed"
        ]
