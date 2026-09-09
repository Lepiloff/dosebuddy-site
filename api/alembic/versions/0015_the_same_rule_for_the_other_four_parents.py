"""The same rule for the other four parents

0014 stopped a medication changing profile. The check it replaced — authorise
the row by the parent it *claims* — stood in four more places, and on
2026-09-09 all four reproduced against a real database.

**profiles.owner_account_id.** The guard in `push` only covered profiles the
caller could currently see, and a caregiver whose access had been revoked can no
longer see the profile while still knowing its id. Pushing the profile row
rewrote `owner_account_id` to theirs. They then pulled the *owner's* projection
— medication names, notes, and the schedules a watcher is never sent — while the
real owner's `roles` map went empty. Revocation was not merely ineffective
against this; it was the step that made it possible, because membership is what
the old check refused on.

**dose_events.profile_id.** Dose ids are sent to every watcher by design
(contract §4.3), so a caregiver could resend one under a profile of their own,
and the row left the owner's feed for theirs.

**schedules.medication_id** and **stock_events.medication_id.** Ids for these
never reach a watcher, so it takes the account's own token — but a re-pointed
schedule strands the doses already materialised from it, which keep naming the
medication and profile they were built for, and the stock balance is the sum of
the journal, so moving one event silently changes two balances.

The medication trigger from 0014 is rebuilt on the shared function here. Five
copies of one plpgsql body is exactly the duplication that drifts, and the CI
check that would catch the drift is newer than the habit that causes it.

None of the five columns has a legitimate move: reminder authority is
`owner_device_id`, pairing grants membership rather than ownership, and the app
cannot move a medication, a schedule or a stock event between parents at all.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# Mirrors app/db/models.py, which attaches the same statements to the schema the
# tests build. SQLSTATE class "SD" is not one Postgres uses, so the code can only
# be ours and `push` can recognise the refusal without reading message text.
FUNCTION = """
CREATE OR REPLACE FUNCTION parent_is_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'SD001',
        MESSAGE = TG_TABLE_NAME || '.' || TG_ARGV[0] || ' is immutable',
        DETAIL = 'row ' || OLD.id;
END;
$$
"""

TRIGGERS = [
    ("profiles", "profiles_owner_is_immutable", "owner_account_id"),
    ("medications", "medications_parent_is_immutable", "profile_id"),
    ("dose_events", "dose_events_parent_is_immutable", "profile_id"),
    ("schedules", "schedules_parent_is_immutable", "medication_id"),
    ("stock_events", "stock_events_parent_is_immutable", "medication_id"),
]

# What 0014 created, restored on the way down so that a downgrade lands on the
# schema 0014 described rather than on one with no guard at all.
FUNCTION_0014 = """
CREATE OR REPLACE FUNCTION medications_parent_is_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'medications.profile_id is immutable'
        USING ERRCODE = 'SD001',
              DETAIL = 'medication ' || OLD.id || ' from ' || OLD.profile_id
                       || ' to ' || NEW.profile_id;
END;
$$
"""

TRIGGER_0014 = """
CREATE TRIGGER medications_parent_is_immutable
BEFORE UPDATE ON medications
FOR EACH ROW WHEN (NEW.profile_id IS DISTINCT FROM OLD.profile_id)
EXECUTE FUNCTION medications_parent_is_immutable()
"""


def upgrade() -> None:
    # No backfill, and nothing to repair: a row that was moved before this ran
    # cannot be told from a row that was always where it is, and the device is
    # the source of truth (spec §0.5). Guessing at a former parent here would
    # invent one.
    op.execute(FUNCTION)
    op.execute("DROP TRIGGER IF EXISTS medications_parent_is_immutable ON medications")
    op.execute("DROP FUNCTION IF EXISTS medications_parent_is_immutable()")
    for table, name, column in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
        op.execute(
            f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} "
            f"FOR EACH ROW WHEN (NEW.{column} IS DISTINCT FROM OLD.{column}) "
            f"EXECUTE FUNCTION parent_is_immutable('{column}')"
        )


def downgrade() -> None:
    for table, name, _column in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS parent_is_immutable()")
    op.execute(FUNCTION_0014)
    op.execute(TRIGGER_0014)
