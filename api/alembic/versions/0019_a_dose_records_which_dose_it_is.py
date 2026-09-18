"""A dose records which dose it is, while that is still knowable

A handover hands both phones the same job for a while. That overlap is
deliberate — without it the new owner would report readiness it does not have —
and its price is that A and B each materialise the same scheduled dose and each
invent an id for it. The server takes both: to it they are two rows. The
client's local UNIQUE quarantines the later one, and `taken` parts company with
the stock it should have decremented.

Whether the server should ever merge them is open (contract §3) and is not a
server decision: canonicalising rewrites `stock_events.dose_event_id` and
reaches into the client's own identifiers — notifications, deep links, the
outbox. That belongs to the owner and the app track.

What is not open is whether the answer stays available. The key is
`schedule_id` with the planned instant — the app track's own local uniqueness,
and not "medication plus time", because two schedules of one medication may
fall in the same minute. `dose_events.schedule_id` is nullable and
`ON DELETE SET NULL`: the day a schedule is deleted, every dose it ever
produced loses the only thing that said which dose it was. Computed at merge
time, the key would already be gone for exactly the histories most likely to
need it.

So it is computed on first sight and frozen. Nothing reads it yet.

The backfill covers what can still be covered — rows whose schedule survives —
and deliberately leaves the rest null rather than guessing from medication and
time. An ad-hoc dose has no key and must not be given one: it was not produced
by a schedule, so sharing a minute with one makes it a different event, not the
same event twice.

`dose_events_key_is_set_once` is the same refusal as the parent triggers, one
letter apart in the WHEN clause: null may become a value, a value may not
become anything else. The push path preserves it too, in the conflict clause,
but a rule that lives only in one code path is a rule the next code path will
not know about — and the way this column is lost is by being *recomputed*, in
good faith, after the schedule is gone.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dose_events", sa.Column("dose_key", sa.String(128), nullable=True))
    op.execute(
        "UPDATE dose_events "
        "SET dose_key = schedule_id || ':' || planned_at_ms "
        "WHERE schedule_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE TRIGGER dose_events_key_is_set_once
        BEFORE UPDATE ON dose_events
        FOR EACH ROW WHEN (
            OLD.dose_key IS NOT NULL AND NEW.dose_key IS DISTINCT FROM OLD.dose_key
        )
        EXECUTE FUNCTION parent_is_immutable('dose_key')
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS dose_events_key_is_set_once ON dose_events")
    op.drop_column("dose_events", "dose_key")
