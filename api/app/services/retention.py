"""Deleting what we have run out of reason to keep.

A revoked membership is kept deliberately: "who could see this profile, and
when" is a question a data subject may put under Article 15, and a row that was
deleted on revocation cannot answer it. The schema says so out loud — the
uniqueness on `profile_memberships` is partial precisely so revoked rows can
stay.

But the reason expires. Nobody asks about an access that ended two years ago,
and "kept for ever" sits badly beside Article 5(1)(e), which allows personal
data to be held only as long as its purpose needs it. So the row outlives the
access by a fixed window and then goes.

The window is the owner's decision (2026-08-19) and the privacy policy names it.
That is why this module exists at all: without it the page would promise a
deletion that never happens, which is worse than promising nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CaregiverRecoveryEvent, ProfileMembership

log = structlog.get_logger(__name__)

# Two years, counted in days because a timedelta has no months. Across a leap
# year that is a day short of the calendar date, which a retention bound can
# afford — it errs towards deleting, never towards keeping longer than promised.
REVOKED_MEMBERSHIP_TTL = timedelta(days=730)

# How often the sweep runs. Daily, not hourly: nothing here is urgent, and a
# retention window measured in years is not made more correct by being enforced
# to the minute.
SWEEP_INTERVAL = timedelta(days=1)
RECOVERY_RETENTION = timedelta(hours=24)
RECOVERY_BATCH_SIZE = 1000
RECOVERY_MAX_BATCHES = 10


async def purge_recovery_batch(session: AsyncSession, now: datetime) -> int:
    """Delete only expired projection rows; retain the parent unique fences."""
    candidates = (select(CaregiverRecoveryEvent.delivery_id)
                  .where(CaregiverRecoveryEvent.expires_at <= now - RECOVERY_RETENTION)
                  .order_by(CaregiverRecoveryEvent.expires_at, CaregiverRecoveryEvent.delivery_id)
                  .limit(RECOVERY_BATCH_SIZE))
    result = await session.execute(delete(CaregiverRecoveryEvent).where(
        CaregiverRecoveryEvent.delivery_id.in_(candidates)))
    return result.rowcount or 0


async def purge_revoked_memberships(session: AsyncSession, now: datetime) -> int:
    """Delete memberships revoked longer ago than the window.

    Only revoked ones. A live membership has `revoked_at IS NULL` and the
    comparison would drop it silently, so the null check is not redundant with
    the date — it is the whole safety of the statement.
    """
    result = await session.execute(
        delete(ProfileMembership).where(
            ProfileMembership.revoked_at.is_not(None),
            ProfileMembership.revoked_at <= now - REVOKED_MEMBERSHIP_TTL,
        )
    )
    return result.rowcount or 0


async def sweep(session: AsyncSession, now: datetime) -> int:
    """Every retention pass we owe, in one place.

    One function so that the worker has a single thing to call and the policy
    has a single thing to point at. Returns the number of rows removed.
    """
    removed = await purge_revoked_memberships(session, now)
    if removed:
        log.info("retention.purged", memberships=removed)
    return removed
