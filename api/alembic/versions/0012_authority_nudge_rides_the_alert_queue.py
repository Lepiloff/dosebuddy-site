"""The authority nudge becomes a queued delivery with a named device

The nudge told the previous device that it no longer arms a profile's alarms.
It was sent inline from the API, and it never arrived: the API has no FCM
credentials — `deploy/docker-compose.yml` gives them to the worker alone — so
`build_push` returned the logging stand-in and every nudge was written to a log
file instead of a phone. Observed live on 2026-08-15 as
`{"event": "push.not_configured", "type": "reminder_authority_lost"}`.

Moving it onto this table is what the owner chose over handing the API the key.
It keeps the credential in one process, reuses the retry, backoff, collapse and
TTL machinery that is already written and tested, and produces a row afterwards
saying whether the nudge went — the question asked during acceptance that
nothing on the server could answer.

`device_id` is new because this signal, unlike the two alerts, concerns exactly
one device. Nullable, and null for everything that existed before: an alert for
a caregiver goes to every phone they have, and narrowing that would lose alerts
to a phone left at home.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres 12 and later allow this inside a transaction as long as the new
    # label is not used in the same one. Nothing here inserts a row of this
    # kind, so the constraint is met — and the API that will write them only
    # starts using it after this migration has committed.
    op.execute("ALTER TYPE alert_kind ADD VALUE IF NOT EXISTS 'reminder_authority_lost'")

    op.add_column(
        "alert_deliveries",
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("alert_deliveries", "device_id")
    # The enum label stays. Removing a value from a Postgres enum means
    # rebuilding the type and every column using it, and leaving an unused
    # label costs nothing.
