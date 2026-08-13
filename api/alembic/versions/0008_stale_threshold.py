"""A silence of twelve hours is a normal night, not a signal.

Android's Doze reaches twelve hours of no network on an idle phone most nights,
so the threshold fired on healthy devices and the caregiver would have been told
their parent's phone had gone quiet most mornings. An alert that is usually
wrong is one its reader learns to dismiss unread, which costs the alert that is
right.

Thirty-six hours sits out a night, a weekend away and a day with no signal, and
still reaches the caregiver within two of their own mornings.

Existing rows move only if they are still on the old default: a caregiver who
has deliberately chosen twelve hours is answered by `dose_missed`, and this
migration has no business overriding a choice. Nothing has chosen yet — there
are no memberships in production — but that will not be true for long.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

OLD, NEW = 12, 36


def upgrade() -> None:
    op.alter_column(
        "profile_memberships",
        "stale_alert_after_hours",
        server_default=str(NEW),
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.execute(
        f"UPDATE profile_memberships SET stale_alert_after_hours = {NEW}"
        f" WHERE stale_alert_after_hours = {OLD}"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE profile_memberships SET stale_alert_after_hours = {OLD}"
        f" WHERE stale_alert_after_hours = {NEW}"
    )
    op.alter_column(
        "profile_memberships",
        "stale_alert_after_hours",
        server_default=str(OLD),
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
