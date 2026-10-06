"""Publish sync positions in commit order, with a stable whole-page read.

The database's BEFORE STATEMENT fence takes this lock before row locks and
sequence defaults. Callers taking explicit advisory/row locks first must take
this fence explicitly. Alert-only delivery transactions never enter the fence;
no mirror/visibility/auth write may follow their FOR UPDATE across FCM.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Two-argument advisory locks have a separate namespace from the canonical-dose
# one-argument hash locks. Stable across API workers and the standalone worker.
PUBLICATION_LOCK = (1396985678, 1)
# A delete/authority transaction can indirectly wait for an AlertDelivery row
# held across FCM. Bound those database lock waits rather than blocking all
# publication indefinitely. A timeout aborts the statement/transaction; callers
# preserve their normal retry debt. This is not a wall-clock transaction SLA.
PUBLICATION_LOCK_TIMEOUT_MS = 5000
PUBLICATION_SQL = "SELECT sync_publication_lock()"
NEXT_SERVER_SEQ = "next_server_seq()"
MIRROR_TABLES = ("profiles", "medications", "schedules", "dose_events", "stock_events")

# Include auth and visibility writes: a logout that locked Device before
# waiting for publication would deadlock a pull holding publication while it
# records Device.cursor_seq. Refresh/pairing rows also precede mirror writes.
# AlertDelivery alone is excluded because a send holds its row across FCM and
# does no publication work afterward (worker.scan_once).
PUBLICATION_TABLES = (
    "accounts", "devices", "refresh_tokens", "profiles", "device_readiness",
    "profile_memberships", "pairing_codes", "medications", "schedules",
    "dose_events", "stock_events",
)

PUBLICATION_LOCK_FUNCTION = f"""
CREATE OR REPLACE FUNCTION sync_publication_lock() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM set_config('lock_timeout', '{PUBLICATION_LOCK_TIMEOUT_MS}ms', true);
    PERFORM pg_advisory_xact_lock({PUBLICATION_LOCK[0]}, {PUBLICATION_LOCK[1]});
END;
$$
"""

NEXT_SERVER_SEQ_FUNCTION = f"""
CREATE OR REPLACE FUNCTION next_server_seq() RETURNS bigint
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sync_publication_lock();
    RETURN nextval('server_seq');
END;
$$
"""

PUBLICATION_FENCE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION sync_publication_fence() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sync_publication_lock();
    RETURN NULL;
END;
$$
"""


def publication_trigger(table: str) -> str:
    return f"""
    CREATE TRIGGER {table}_sync_publication
    BEFORE INSERT OR UPDATE OR DELETE ON {table}
    FOR EACH STATEMENT EXECUTE FUNCTION sync_publication_fence()
    """


async def lock_publication(session: AsyncSession) -> None:
    # A separate statement matters: READ COMMITTED takes the following SELECT's
    # snapshot *after* any predecessor has committed and released the lock.
    await session.execute(text(PUBLICATION_SQL))
