"""profiles: timestamps in milliseconds, like the rest of the mirror

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-11

`profiles` was built for pairing, before sync existed, so its timestamps were
timestamptz while every other mirrored table stores the device's milliseconds.
Last-write-wins would then have compared a device clock against a database type
— and not failed loudly for it, just picked the wrong winner now and then.

Safe as a straight swap only because no profile rows exist yet. With data this
would need a conversion pass instead.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM profiles")  # empty in practice; makes the swap total

    op.drop_column("profiles", "created_at")
    op.drop_column("profiles", "updated_at")
    op.drop_column("profiles", "deleted_at")

    op.add_column("profiles", sa.Column("created_at_ms", sa.BigInteger(), nullable=False))
    op.add_column("profiles", sa.Column("updated_at_ms", sa.BigInteger(), nullable=False))
    op.add_column("profiles", sa.Column("deleted_at_ms", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM profiles")

    op.drop_column("profiles", "created_at_ms")
    op.drop_column("profiles", "updated_at_ms")
    op.drop_column("profiles", "deleted_at_ms")

    op.add_column(
        "profiles",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "profiles",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "profiles",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
