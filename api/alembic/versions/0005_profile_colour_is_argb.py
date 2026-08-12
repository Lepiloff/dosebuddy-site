"""profiles.color must hold an unsigned ARGB value

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-12

An Android colour is unsigned ARGB: 0xFF2A9D8F is 4283215696, which is past the
top of a signed int32. The device stores it without complaint, so the first
profile carrying a colour would have failed its push with a type error and taken
the whole batch with it.

Found by generating the contract's examples from real values rather than from
zeros.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("profiles", "color", type_=sa.BigInteger(), existing_nullable=False)


def downgrade() -> None:
    # Narrowing would truncate any real colour, so it is only safe while the
    # column holds nothing that needs the width.
    op.alter_column("profiles", "color", type_=sa.Integer(), existing_nullable=False)
