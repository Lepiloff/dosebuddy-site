"""A device says when it is ready, instead of the server assuming it

`devices.cursor_seq` records that a page was produced for a device. The
authority gate has always read it as "the new owner is ready", and the gap
between those two sentences is the failure this project ranks worst: the
previous phone told to stop while the new one has nothing to ring with.

The app track measured what is in that gap (2026-09-17). Their cursor is saved
in the same transaction as the page, so it does prove the page arrived — but
rows inside it can be quarantined or left waiting for a parent, and the next
page is requested *before* alarms are rebuilt. So even an acknowledged cursor
means "these bytes reached the database", not "this phone will ring".

Two columns for it, and the split matters.

`devices.ready_protocol_at` says the device speaks the protocol at all. It is a
property of the build rather than of the session, so it cannot be learned at
sign-in: a phone that updates keeps its token and goes on pulling in the
background without authenticating again. It is carried on every pull and
written in the same statement as `cursor_seq` — the moment the server learns a
device can report readiness is the same moment it records what that device was
handed.

Null there is load-bearing. A device that never says it keeps the old rule, and
must: a gate that waited for a signal the device cannot send would leave the
previous phone ringing for ever, and a permanent duplicate is not an
improvement on a brief silence.

`device_readiness` is the assertion itself, per (device, profile). It carries
the revision the device saw and the cursor it applied through, because the
revision alone can be sent by a phone holding none of the data — the client
learns it from the claim response before loading anything.

No timeout on waiting for it, deliberately, and the app track asked for that
explicitly: a timeout would re-admit the silence this exists to prevent. A long
wait is shown to a person instead, with a way to retry or to stop the reminders
on purpose.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("ready_protocol_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "device_readiness",
        sa.Column("device_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("applied_cursor", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("device_id", "profile_id"),
    )


def downgrade() -> None:
    op.drop_table("device_readiness")
    op.drop_column("devices", "ready_protocol_at")
