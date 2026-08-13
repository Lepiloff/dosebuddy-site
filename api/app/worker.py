"""The alert loop.

Its own process, not a background task inside the API. With a task, every uvicorn
worker would run its own copy and the same caregiver would be told the same thing
once per worker — invisible today because there is one worker, and a surprise the
day there are two. One process, one loop.

It also fails apart from the API: a scan that throws does not take request
handling with it, and vice versa.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import create_engine, create_sessionmaker
from app.services import alerts
from app.services.push import Push, build_push

SCAN_INTERVAL_SECONDS = 60

log = structlog.get_logger(__name__)


async def scan_once(sessionmaker, push: Push, now: datetime | None = None) -> int:
    """Detect, then deliver. Two phases, and the split is the point.

    They used to be one: claim a row and send in the same breath, with the claim
    written as `sent_at`. A send that FCM refused therefore left a row claiming
    delivery, so the alert was lost, unretried and — because the record lied —
    not even findable afterwards. For a product whose whole purpose is telling a
    family that a dose was missed, that is the wrong way to fail.

    Now the claim only says the alert exists and is owed. Delivery is a separate
    pass over what is owed, with its own attempts and backoff, and a caregiver
    is not woken twice because the retry carries a collapse key.

    Returns the number of alerts delivered in this pass.
    """
    now = now or datetime.now(timezone.utc)
    delivered = 0

    async with sessionmaker() as session:
        found = await alerts.find_missed_dose_alerts(session, now)
        found += await alerts.find_stale_profile_alerts(session, now)
        for alert in found:
            await alerts.claim(session, alert, now)

        # Before delivering, not after: an alert that stopped being useful while
        # the worker was down should be retired, not sent late.
        await alerts.expire(session, now)
        await session.commit()

        for delivery_id in await alerts.due(session, now):
            delivery = await alerts.take(session, delivery_id, now)
            if delivery is None:
                # Another worker has it, or has already finished with it.
                continue

            tokens = await alerts.tokens_for(session, delivery.account_id)
            if not tokens:
                # Nobody to tell right now. Not a failure of this attempt — the
                # caregiver may simply not have opened the app yet — so it waits
                # rather than burning one of the alert's attempts. Committed
                # anyway, to drop the lock rather than hold it for the pass.
                await session.commit()
                continue

            outcome = await alerts.deliver(push, delivery, tokens)
            alerts.record(delivery, outcome, now)
            if delivery.state == alerts.AlertState.sent.value:
                delivered += 1

            # Committed per delivery. A crash halfway through a batch must not
            # replay the sends that already happened.
            await session.commit()

    return delivered


async def main() -> None:
    settings = get_settings()
    setup_logging(settings)

    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    push = build_push(settings)

    log.info("worker.start", push=type(push).__name__, interval=SCAN_INTERVAL_SECONDS)
    try:
        while True:
            try:
                sent = await scan_once(sessionmaker, push)
                if sent:
                    log.info("worker.sent", count=sent)
            except Exception:  # noqa: BLE001
                # One bad scan must not end the loop. A worker that exits on the
                # first error looks alive in `docker ps` for exactly as long as
                # the restart takes, and stops alerting for good if the error
                # repeats.
                log.exception("worker.scan_failed")
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
