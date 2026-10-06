"""Commit-ordered sync publication and recovery of already skipped rows.

Revision ID: 0021
Revises: 0020

Deploy with API/worker writers stopped, then restart only after schema_check.
The advisory fence must precede DDL/table locks, just as it precedes row locks
in normal traffic. Downgrade cannot undo positions clients may have consumed.
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

MIRROR_TABLES = ("profiles", "medications", "schedules", "dose_events", "stock_events")
PUBLICATION_TABLES = (
    "accounts", "devices", "refresh_tokens", "profiles", "device_readiness",
    "profile_memberships", "pairing_codes", "medications", "schedules",
    "dose_events", "stock_events",
)

PUBLICATION_LOCK_FUNCTION = """
CREATE OR REPLACE FUNCTION sync_publication_lock() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM set_config('lock_timeout', '5000ms', true);
    PERFORM pg_advisory_xact_lock(1396985678, 1);
END;
$$
"""
NEXT_SERVER_SEQ_FUNCTION = """
CREATE OR REPLACE FUNCTION next_server_seq() RETURNS bigint
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sync_publication_lock();
    RETURN nextval('server_seq');
END;
$$
"""
PUBLICATION_FENCE_FUNCTION = """
CREATE OR REPLACE FUNCTION sync_publication_fence() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sync_publication_lock();
    RETURN NULL;
END;
$$
"""


def upgrade() -> None:
    op.execute("SELECT pg_advisory_xact_lock(1396985678, 1)")
    # Sequence caches are connection-local and can publish a smaller cached
    # number after a larger one even with serial transactions. Gaps from aborted
    # transactions are fine; reversed or recycled numbers are not.
    op.execute("ALTER SEQUENCE server_seq INCREMENT BY 1 NO CYCLE CACHE 1")
    op.execute(PUBLICATION_LOCK_FUNCTION)
    op.execute(NEXT_SERVER_SEQ_FUNCTION)
    op.execute(PUBLICATION_FENCE_FUNCTION)
    for table in PUBLICATION_TABLES:
        op.execute(f"""
            CREATE TRIGGER {table}_sync_publication
            BEFORE INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION sync_publication_fence()
        """)
    for table in MIRROR_TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN server_seq SET DEFAULT next_server_seq()")

    # Older clients may already have advanced past a late commit. Preserve every
    # domain/LWW field and id (including tombstones and superseded_by), and put
    # all existing rows beyond every previously issued server frontier.
    # Replaying an upgraded revision is unnecessary: Alembic records this in
    # the same transaction. Rollback leaves harmless sequence gaps only.
    highs = ["SELECT last_value AS seq FROM server_seq"]
    highs += [f"SELECT max(server_seq) AS seq FROM {table}" for table in MIRROR_TABLES]
    # Device.cursor_seq/readiness.applied_cursor are echoed client input and
    # may be arbitrarily large. They cannot prove an issued server frontier.
    op.execute("SELECT setval('server_seq', (SELECT max(seq) FROM (" +
               " UNION ALL ".join(highs) + ") AS frontiers), true)")
    for table in MIRROR_TABLES:
        op.execute(f"UPDATE {table} SET server_seq = next_server_seq()")


def downgrade() -> None:
    op.execute("SELECT pg_advisory_xact_lock(1396985678, 1)")
    for table in PUBLICATION_TABLES:
        op.execute(f"DROP TRIGGER {table}_sync_publication ON {table}")
    for table in MIRROR_TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN server_seq SET DEFAULT nextval('server_seq')")
    op.execute("DROP FUNCTION sync_publication_fence()")
    op.execute("DROP FUNCTION next_server_seq()")
    op.execute("DROP FUNCTION sync_publication_lock()")
