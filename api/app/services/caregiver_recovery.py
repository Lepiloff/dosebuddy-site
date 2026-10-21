"""Explicit operator-controlled coverage. No reader or implicit enable path."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CaregiverRecoveryControl, utcnow
from app.db.publication import lock_publication


async def enable_coverage(
    session: AsyncSession, now: datetime | None = None, *, writers_verified: bool = False,
) -> CaregiverRecoveryControl:
    if not writers_verified:
        raise ValueError("Verify all producers and retirement guards before enabling coverage")
    await lock_publication(session)
    now = now or utcnow()
    control = (await session.execute(select(CaregiverRecoveryControl)
                                    .execution_options(populate_existing=True))).scalar_one()
    if not control.producer_enabled:
        control.epoch = uuid.uuid4()
        control.coverage_started_at = now
        control.closed_at = None
        control.producer_enabled = True
        control.reader_enabled = False
        await session.flush()
    return control


async def close_coverage(session: AsyncSession, now: datetime | None = None) -> CaregiverRecoveryControl:
    await lock_publication(session)
    now = now or utcnow()
    control = (await session.execute(select(CaregiverRecoveryControl)
                                    .execution_options(populate_existing=True))).scalar_one()
    if control.producer_enabled:
        control.closed_at = now
    control.reader_enabled = False
    control.producer_enabled = False
    await session.flush()
    return control


async def set_reader(session: AsyncSession, *, enabled: bool, writers_verified: bool = False) -> CaregiverRecoveryControl:
    if enabled and not writers_verified:
        raise ValueError("Verify the complete reader rollout before enabling")
    await lock_publication(session)
    control = (await session.execute(select(CaregiverRecoveryControl)
                                    .execution_options(populate_existing=True))).scalar_one()
    if enabled and not control.producer_enabled:
        raise ValueError("Coverage must be enabled before the reader")
    control.reader_enabled = enabled
    await session.flush()
    return control
