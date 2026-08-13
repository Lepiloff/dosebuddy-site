"""Composite indexes for the pull query.

Every mirrored table is read the same way: "rows of these keys, after this
cursor, in sequence order". That needs `(key, server_seq)` as one index. The
separate single-column indexes could serve either half but never both, so
Postgres had to choose between discarding rows it had already read and sorting
rows the LIMIT should have made unnecessary. Measured before this migration, on
745k dose events: 11,920 rows discarded to return 500 for a dense profile, and a
full read-then-sort of a sparse one.

The `(key)`-only indexes go: the composite has the same leading column, so it
answers everything they answered, and keeping both would tax every write twice
for one lookup path. The `(server_seq)`-only indexes stay — they are what a
future backfill or an operator counting the stream would reach for, and they are
not redundant with anything here.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


# (table, key column) — the pair every pull filters and orders by.
PAIRS = [
    ("medications", "profile_id"),
    ("dose_events", "profile_id"),
    ("schedules", "medication_id"),
    ("stock_events", "medication_id"),
]


def upgrade() -> None:
    for table, key in PAIRS:
        op.create_index(f"ix_{table}_{key}_server_seq", table, [key, "server_seq"])
        op.drop_index(f"ix_{table}_{key}", table_name=table)


def downgrade() -> None:
    for table, key in PAIRS:
        op.create_index(f"ix_{table}_{key}", table, [key])
        op.drop_index(f"ix_{table}_{key}_server_seq", table_name=table)
