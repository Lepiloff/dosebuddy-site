"""The dose that can give way is told to, and says so on its own row

Refusing a duplicate at push time closes the order where the previous phone got
there first. It does not close the other one, and the app track named it
(18.09.2026): the claimant materialises a dose while the previous phone is
offline and pushes it first, so the key is free and the row is stored. The old
phone comes back and pushes the same dose, and it is never refused — a client
that cannot be told about a foreign id would be left holding a quarantined row
instead of a dose. Two live rows, from the rule that was supposed to prevent
them.

So the row that *can* give way is told to, on the row itself. `superseded_by`
names the survivor rather than leaving the client to find it: "my dose vanished,
I will look for a live twin at the same planned time" is a deduction that can go
wrong in silence. Set once — a row that has given way stays given away.

The server does no more than name it. Moving the action, the alarm and the stock
entry is the client's work, and deliberately so: only the client knows whether
stock is tracked at all, how much was left in the packet, and whether the
history has been folded into a baseline — the three facts that made every
server-side arithmetic on stock wrong (contract §3).

`devices.adopts_doses_at` is how the server knows which rows can be told
anything. It is written from the push that declares the capability and not from
the pull, because the client's sync cycle pushes before it pulls
(`sync_service.dart:280`) while `ready_protocol_at` is written on pull. Reading
the capability from the pull would misclassify the first push of every freshly
updated phone — and the first push after an update is exactly where the rows
that need the new answer live.

Two columns and a trigger, all of them inert until a client sends
`understands: ["duplicate_dose"]`. Nothing in production does.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("adopts_doses_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "dose_events",
        sa.Column("superseded_by", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "dose_events_superseded_by_fkey",
        "dose_events",
        "dose_events",
        ["superseded_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        CREATE TRIGGER dose_events_supersede_is_set_once
        BEFORE UPDATE ON dose_events
        FOR EACH ROW WHEN (
            OLD.superseded_by IS NOT NULL
            AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by
        )
        EXECUTE FUNCTION parent_is_immutable('superseded_by')
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS dose_events_supersede_is_set_once ON dose_events")
    op.drop_constraint("dose_events_superseded_by_fkey", "dose_events", type_="foreignkey")
    op.drop_column("dose_events", "superseded_by")
    op.drop_column("devices", "adopts_doses_at")
