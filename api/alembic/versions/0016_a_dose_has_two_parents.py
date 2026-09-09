"""A dose has two parents

0015 made `dose_events.profile_id` immutable and left `dose_events.medication_id`
alone, which guarded the half of the row nobody was attacking.

Found by the app track's review of the seam, and reproduced here before it was
believed: one account, no race. A dose sits as (profile p1, medication m1); the
push sends it as (profile p1, medication m2). The profile did not change, so the
trigger says nothing; both permission checks pass, because m2 belongs to the
same caller; and the generic upsert writes the new medication_id.

On the phone that is worse than the profile move, because nothing looks wrong.
Reminder authority is read from `dose_events.profile_id`, which is untouched, so
the alarm still rings and rings on time. The text of it is joined through
`dose_events.medication_id`, and confirming the dose takes stock off that same
medication. A reminder naming the wrong medicine, and the wrong packet counted
down — with no error anywhere and a full history that agrees with itself.

The dose row is also the one row in the schema with two parents, which is why
the rule needed saying twice. `schedule_id` is deliberately not guarded: it is
nullable, it is set null when the schedule goes, and its ids never leave the
owner's own device.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

NAME = "dose_events_medication_is_immutable"


def upgrade() -> None:
    # The function is 0015's, unchanged: it takes the column name and says which
    # one it was. Nothing to backfill — a dose that was re-pointed before this
    # ran cannot be told from one that was always where it is.
    op.execute(f"DROP TRIGGER IF EXISTS {NAME} ON dose_events")
    op.execute(
        f"CREATE TRIGGER {NAME} BEFORE UPDATE ON dose_events "
        "FOR EACH ROW WHEN (NEW.medication_id IS DISTINCT FROM OLD.medication_id) "
        "EXECUTE FUNCTION parent_is_immutable('medication_id')"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {NAME} ON dose_events")
