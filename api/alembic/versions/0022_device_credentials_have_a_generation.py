"""Keep revoked credentials dead when the same device signs in again.

Active legacy credentials remain generation zero. A device still revoked at
upgrade starts at one, so clearing revoked_at cannot revive its old JWTs.
Previously cleared revocation history is not recoverable from the old schema.
"""

from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Frozen SQL, independent of future production helper changes. All API and
    # worker processes from before this credential fence must be stopped.
    op.execute("SELECT sync_publication_lock()")
    for table in ("devices", "refresh_tokens"):
        op.add_column(table, sa.Column(
            "credential_generation", sa.BigInteger(), nullable=False,
            server_default=sa.text("0"),
        ))
        op.create_check_constraint(
            f"ck_{table}_credential_generation", table, "credential_generation >= 0"
        )
    op.execute("UPDATE devices SET credential_generation=1 WHERE revoked_at IS NOT NULL")


def downgrade() -> None:
    for table in ("refresh_tokens", "devices"):
        op.drop_constraint(f"ck_{table}_credential_generation", table, type_="check")
        op.drop_column(table, "credential_generation")
