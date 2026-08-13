"""One alert per profile per day, not one per caregiver per day.

`uq_alert_once` was (account_id, kind, subject_id). For `profile_stale` the
subject is a date, so a caregiver looking after two parents received a single
alert a day between them: whichever profile the scan reached first took the key,
and the other was silently deduplicated away. The second parent's phone could be
dead for a week and never be mentioned once.

The more people someone looks after, the more the key hid — the opposite of what
anyone would want, and invisible to anyone testing with one profile.

`dose_missed` was unaffected in practice, its subject being a dose id, which is
unique across profiles anyway. Adding the profile costs it nothing.

Rebuilding the index cannot lose a row: widening a unique key only ever admits
more, never fewer.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_alert_once", table_name="alert_deliveries")
    op.create_index(
        "uq_alert_once",
        "alert_deliveries",
        ["account_id", "profile_id", "kind", "subject_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_alert_once", table_name="alert_deliveries")
    # Narrowing the key can collide on rows the wider one allowed, and those
    # rows are exactly the alerts this migration exists to stop losing. Drop the
    # duplicates deliberately rather than let the index creation fail.
    op.execute(
        """
        DELETE FROM alert_deliveries a USING alert_deliveries b
        WHERE a.ctid > b.ctid
          AND a.account_id = b.account_id
          AND a.kind = b.kind
          AND a.subject_id = b.subject_id
        """
    )
    op.create_index(
        "uq_alert_once",
        "alert_deliveries",
        ["account_id", "kind", "subject_id"],
        unique=True,
    )
