"""Authority can be claimed in two phases, and leased only where that is safe

Three columns, and the third is a rollout gate rather than a feature.

`previous_owner_device_id` is where reminders go back to if the new owner never
confirms it can ring. It existed only as a local variable inside the handover
request, which was enough while nothing outlived that request.

`pending_owner_device_id` is a device that has claimed and not yet reported.
Authority does not move until it does, so the phone holding the alarms goes on
holding them — including a published client that knows nothing about any of
this and would otherwise cancel them the moment it saw a foreign owner id. That
is what makes the published 1.4.4 behave correctly in the losing role with no
client change at all.

`authority_leased` says whether *this* handover was claimed under the protocol
that renews readiness. It is on the handover and not on the device because the
app track's rollout gate says it must be: the client shipping today reports
readiness **once** per (revision, cursor) and does not repeat it on an empty
sync, so its `ready_protocol_at` means "can report", not "will renew". A lease
applied to every device carrying that column would start handing authority back
an hour later on healthy phones — authority ping-pong, built deliberately.

The lease itself is off unless configured, so this migration changes no
behaviour on the day it runs.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column("previous_owner_device_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "profiles",
        sa.Column("pending_owner_device_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "profiles",
        sa.Column(
            "authority_leased", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_foreign_key(
        "fk_profiles_previous_owner_device",
        "profiles",
        "devices",
        ["previous_owner_device_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_profiles_pending_owner_device",
        "profiles",
        "devices",
        ["pending_owner_device_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_profiles_pending_owner_device", "profiles", type_="foreignkey")
    op.drop_constraint("fk_profiles_previous_owner_device", "profiles", type_="foreignkey")
    op.drop_column("profiles", "authority_leased")
    op.drop_column("profiles", "pending_owner_device_id")
    op.drop_column("profiles", "previous_owner_device_id")
