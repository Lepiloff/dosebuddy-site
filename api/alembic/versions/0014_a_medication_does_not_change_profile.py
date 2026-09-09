"""A medication does not change profile

`medications.profile_id` is set when the row is created and never again. The
guard is a BEFORE UPDATE trigger rather than a check in the push endpoint, and
both halves of that sentence were paid for.

What a move breaks is on the phone, and it breaks quietly. `dose_events.profile_id`
is written when a dose is first materialised and never rewritten, so a
medication that changed parent leaves its doses naming the profile it used to
belong to. The device reads reminder authority from the dose row rather than
from the medication's current parent, decides the alarm it was about to set is
stale, and drops it — no error, no dose, no sound.

On this side it closes a hole of its own. `push` checked that the caller owns
the profile a medication *claims* and never the profile the stored row already
belongs to, so a caregiver — who is sent the id of every medication on a profile
they watch — could resend one under a profile of their own and move the row out
of the owner's data.

A trigger, because that is what makes the refusal atomic: the whole statement
aborts, so no other field of that row lands and its `server_seq` does not move.
The client treats a rejected row as one version to be quarantined whole, and a
refusal that let the name through while stopping the parent would hand it half a
version. It also holds for writers that are not this endpoint — a backfill, a
support script, the next thing someone writes.

The deploy migrates before it restarts, so for a few seconds the old code runs
against the new schema — the safe order for an additive change, and not quite
free for this one. In that window a move comes back in `retry` as `conflict`
rather than in `rejected` as `immutable_parent`, because the handler that is
still running has no branch for the new code. The client is told to try again,
and the try lands on the new code.

The two statements are duplicated in `app/db/models.py`, where they are attached
to the table's creation so the test suite gets the same guard. Duplicated on
purpose: a migration that imported the models would change meaning whenever they
did, and it has to keep meaning what it meant on the day it ran.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

# SQLSTATE class "SD" is not one Postgres uses, so a code in it can only be
# ours, and the API can recognise this refusal without reading message text.
# Mirrors IMMUTABLE_PARENT_SQLSTATE in app/db/models.py.
FUNCTION = """
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

# The WHEN clause keeps every ordinary edit to a medication out of plpgsql.
TRIGGER = """
CREATE TRIGGER medications_parent_is_immutable
BEFORE UPDATE ON medications
FOR EACH ROW WHEN (NEW.profile_id IS DISTINCT FROM OLD.profile_id)
EXECUTE FUNCTION medications_parent_is_immutable()
"""


def upgrade() -> None:
    # No backfill. Rows that were moved before this ran cannot be told from rows
    # that were always where they are, and the device is the source of truth
    # (spec §0.5): guessing at a former parent here would invent one.
    op.execute(FUNCTION)
    op.execute(TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS medications_parent_is_immutable ON medications")
    op.execute("DROP FUNCTION IF EXISTS medications_parent_is_immutable()")
