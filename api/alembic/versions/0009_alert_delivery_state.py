"""An alert that was not delivered must say so.

`sent_at` was written when the alert was claimed, before anything was sent, so a
refused push left behind a row claiming delivery. The alert was lost, never
retried, and — because the record was wrong — could not even be found
afterwards. Losing a caregiver's notice that a dose was missed is the one
failure this service exists to prevent.

Delivery now carries state, attempts and a next attempt, and `sent_at` is set
only by a send FCM accepted. Retrying is safe because each message goes with a
collapse key, so two deliveries of one alert land as one notification.

Existing rows are recorded as `sent`. Under the old code they had been attempted
exactly once and could never be retried, so calling them sent is the only claim
consistent with what actually happened to them.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert_deliveries",
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
    )
    op.add_column(
        "alert_deliveries",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "alert_deliveries",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "alert_deliveries",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "alert_deliveries", sa.Column("last_error", sa.String(200), nullable=True)
    )

    # Backfill before the NOT NULLs land, or the alter fails on any existing row.
    op.execute(
        "UPDATE alert_deliveries"
        " SET state = 'sent', attempts = 1,"
        "     next_attempt_at = sent_at, expires_at = sent_at"
    )
    op.alter_column("alert_deliveries", "next_attempt_at", nullable=False)
    op.alter_column("alert_deliveries", "expires_at", nullable=False)

    # Only a real send writes it now, so it has to be able to be empty.
    op.alter_column(
        "alert_deliveries",
        "sent_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )

    op.create_index(
        "ix_alert_deliveries_due", "alert_deliveries", ["state", "next_attempt_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_alert_deliveries_due", table_name="alert_deliveries")

    # Rows that never made it would violate the old NOT NULL, and the old schema
    # has nowhere to say what became of them. Dropping them is the honest move:
    # the previous code could not have recorded them at all.
    op.execute("DELETE FROM alert_deliveries WHERE sent_at IS NULL")
    op.alter_column(
        "alert_deliveries",
        "sent_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )

    for column in ("last_error", "expires_at", "next_attempt_at", "attempts", "state"):
        op.drop_column("alert_deliveries", column)
