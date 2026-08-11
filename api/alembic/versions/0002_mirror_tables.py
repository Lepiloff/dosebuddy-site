"""the device model mirrored: medications, schedules, dose_events, stock_events

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEQ = sa.text("nextval('server_seq')")


def _sync_columns() -> list[sa.Column]:
    """The v1.0 sync-ready foundation (spec §4.4), same on every mirrored table.

    Milliseconds as BIGINT rather than timestamptz: that is what the device
    stores and what crosses the wire, and converting in and out of a database
    type is where an hour goes missing at a DST boundary.
    """
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("server_seq", sa.BigInteger(), nullable=False, server_default=SEQ),
    ]


def upgrade() -> None:
    op.create_table(
        "medications",
        *_sync_columns(),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Encrypted blobs, versioned by their first byte.
        sa.Column("name", sa.LargeBinary(), nullable=False),
        sa.Column("notes", sa.LargeBinary(), nullable=True),
        sa.Column("dosage_text", sa.LargeBinary(), nullable=True),
        sa.Column("dose_amount", sa.Float(), nullable=False, server_default="1"),
        # TEXT, not a Postgres enum: the value that arrives tomorrow comes from a
        # newer client and is relayed through here to an older one. A type that
        # rejects what it does not know would break that relay.
        sa.Column("form", sa.String(32), nullable=False),
        sa.Column("pack_size", sa.Float(), nullable=True),
        sa.Column("refill_threshold_days", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("photo_key", sa.String(255), nullable=True),
        # No current_stock: it is a cache of the stock journal sum, and a derived
        # value on the wire is a second source of truth.
    )
    op.create_index("ix_medications_profile_id", "medications", ["profile_id"])
    op.create_index("ix_medications_server_seq", "medications", ["server_seq"])

    op.create_table(
        "schedules",
        *_sync_columns(),
        sa.Column(
            "medication_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("medications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(32), nullable=False),
        # Stored and returned verbatim; the server never expands a schedule into
        # doses. A second calendar implementation would disagree with the first
        # at a DST boundary, and disagree quietly.
        sa.Column("times", sa.Text(), nullable=False),
        sa.Column("days_of_week", sa.Text(), nullable=True),
        sa.Column("interval_days", sa.Integer(), nullable=True),
        sa.Column("start_date", sa.String(10), nullable=False),
        sa.Column("end_date", sa.String(10), nullable=True),
    )
    op.create_index("ix_schedules_medication_id", "schedules", ["medication_id"])
    op.create_index("ix_schedules_server_seq", "schedules", ["server_seq"])

    op.create_table(
        "dose_events",
        *_sync_columns(),
        sa.Column(
            "schedule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("schedules.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "medication_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("medications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("planned_at_ms", sa.BigInteger(), nullable=False),
        # Not monotonic: missed returns to pending when the intake window is
        # widened, taken returns to pending from the calendar.
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("action_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("snooze_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snoozed_until_ms", sa.BigInteger(), nullable=True),
        sa.Column("dose_amount", sa.Float(), nullable=False),
    )
    op.create_index("ix_dose_events_profile_id", "dose_events", ["profile_id"])
    op.create_index("ix_dose_events_medication_id", "dose_events", ["medication_id"])
    op.create_index("ix_dose_events_server_seq", "dose_events", ["server_seq"])
    # Missed-dose detection reads by profile and time; the caregiver alerts are
    # built on it, so it should not be a sequential scan once history exists.
    op.create_index("ix_dose_events_planned_at_ms", "dose_events", ["planned_at_ms"])
    op.create_index("ix_dose_events_status", "dose_events", ["status"])

    op.create_table(
        "stock_events",
        *_sync_columns(),
        sa.Column(
            "medication_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("medications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("delta", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column(
            "dose_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dose_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_stock_events_medication_id", "stock_events", ["medication_id"])
    op.create_index("ix_stock_events_server_seq", "stock_events", ["server_seq"])


def downgrade() -> None:
    op.drop_table("stock_events")
    op.drop_table("dose_events")
    op.drop_table("schedules")
    op.drop_table("medications")
