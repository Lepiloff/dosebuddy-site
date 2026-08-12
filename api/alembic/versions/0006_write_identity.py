"""every write carries who made it and where it sits in that device's sequence

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-12

`updated_at` alone cannot order writes. Two edits to one row inside a single
millisecond are ordinary, and comparing timestamps only means either dropping
the second as a duplicate or applying a genuine resend as if it were new — one
loses data, the other churns. It also cannot break a tie between two devices
except by whichever request happened to arrive last.

So each row records the device that wrote it and that device's own monotonic
counter, which owes nothing to a clock. (updated_at, origin_device_id, op_seq)
then orders writes and makes a resend an exact tie.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("profiles", "medications", "schedules", "dose_events", "stock_events")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column("origin_device_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.add_column(
            table,
            sa.Column("op_seq", sa.BigInteger(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "op_seq")
        op.drop_column(table, "origin_device_id")
