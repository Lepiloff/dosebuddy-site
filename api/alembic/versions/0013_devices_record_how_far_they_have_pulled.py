"""Devices record how far their cursor has reached

So the server can answer a question it could not before: has the device that
just took over a profile actually seen that it did?

Without it the nudge telling the previous phone to stop went out at once, while
the new phone still only learned by pulling. Both halves were correct and the
combination was worse than either: measured 2026-08-18, three handovers on the
fixed server and the fixed client produced **2 min 36 s of silence**, where the
build without the client fix had produced 28 s of both phones ringing.

Silence is the worse failure (spec invariant 1 — reliability of reminders comes
first), so the previous phone is now kept ringing until this column shows the
new one is ready. The degradation is a bounded duplicate rather than a gap.

Nullable, and null for every existing row: "not known to have pulled anything",
which holds the nudge rather than releasing it. It fills in on the first pull.

The cursor stays the client's. This is a copy of the last value handed out, kept
for this one decision, and nothing reads it as authority.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("cursor_seq", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "cursor_seq")
