"""alert thresholds on a membership, and a record of what was sent

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ALERT_KIND = postgresql.ENUM(
    "dose_missed", "profile_stale", name="alert_kind", create_type=False
)


def upgrade() -> None:
    ALERT_KIND.create(op.get_bind(), checkfirst=True)

    # Thresholds live on the link, not globally: one caregiver wants to know at
    # once, another only if a dose is really being forgotten, and both are right.
    op.add_column(
        "profile_memberships",
        sa.Column(
            "dose_alert_after_minutes", sa.Integer(), nullable=False, server_default="30"
        ),
    )
    op.add_column(
        "profile_memberships",
        sa.Column(
            "stale_alert_after_hours", sa.Integer(), nullable=False, server_default="12"
        ),
    )

    op.create_table(
        "alert_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", ALERT_KIND, nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_alert_deliveries_account_id", "alert_deliveries", ["account_id"])
    op.create_index("ix_alert_deliveries_profile_id", "alert_deliveries", ["profile_id"])
    # The lock that stops an alert being sent twice: the insert either claims the
    # row or finds it taken, and only the claimer sends.
    op.create_index(
        "uq_alert_once", "alert_deliveries", ["account_id", "kind", "subject_id"], unique=True
    )


def downgrade() -> None:
    op.drop_table("alert_deliveries")
    op.drop_column("profile_memberships", "stale_alert_after_hours")
    op.drop_column("profile_memberships", "dose_alert_after_minutes")
    ALERT_KIND.drop(op.get_bind(), checkfirst=True)
