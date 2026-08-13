"""Two indexes where one was meant, on three columns.

`mapped_column(..., unique=True, index=True)` means one unique index. Alembic
rendered it as a UNIQUE constraint *and* a separate non-unique index, so
production has been carrying a redundant index on `accounts.google_sub`,
`pairing_codes.code_hash` and `refresh_tokens.token_hash` since 0001 — paid for
on every insert, useful to nothing, because a unique index already serves every
lookup a plain one would.

Nothing was broken by it: uniqueness was enforced throughout, by the constraint.
What it broke was the ability to trust a schema comparison, which is how it was
found — the models and the migrations disagreed, and that disagreement is the
kind that hides a real one later. CI now fails on any such drift, so this is the
last of them.

**Order matters.** Uniqueness must be enforced at every instant, including
midway through this migration: dropping the constraint first would leave a
window in which two identical `google_sub` values could be inserted, and the
index creation afterwards would then fail with the duplicates already committed.
So the new unique index is created while the old constraint still stands, and
only then is the constraint dropped.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# (table, column) — each carrying a redundant index alongside a unique constraint.
COLUMNS = [
    ("accounts", "google_sub"),
    ("pairing_codes", "code_hash"),
    ("refresh_tokens", "token_hash"),
]


def upgrade() -> None:
    for table, column in COLUMNS:
        index = f"ix_{table}_{column}"
        constraint = f"{table}_{column}_key"

        # The constraint still enforces uniqueness while this one is away.
        op.drop_index(index, table_name=table)
        op.create_index(index, table, [column], unique=True)
        # Two enforcers exist at this point; now the older one goes.
        op.drop_constraint(constraint, table, type_="unique")


def downgrade() -> None:
    for table, column in COLUMNS:
        index = f"ix_{table}_{column}"
        constraint = f"{table}_{column}_key"

        op.create_unique_constraint(constraint, table, [column])
        op.drop_index(index, table_name=table)
        op.create_index(index, table, [column])
