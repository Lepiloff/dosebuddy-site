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
from app.services.push import FcmPush, LoggingPush, Push

SCAN_INTERVAL_SECONDS = 60

log = structlog.get_logger(__name__)


def build_push(settings) -> Push:
    if settings.fcm_project_id and settings.fcm_credentials_path:
        return FcmPush(settings.fcm_project_id, settings.fcm_credentials_path)
    return LoggingPush()


async def scan_once(sessionmaker, push: Push, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    sent = 0

    async with sessionmaker() as session:
        found = await alerts.find_missed_dose_alerts(session, now)
        found += await alerts.find_stale_profile_alerts(session, now)

        for alert in found:
            # Claim before sending: if the process dies between the two, the
            # alert is lost rather than repeated. A caregiver who is told twice
            # about one dose starts ignoring the third.
            if not await alerts.claim(session, alert):
                continue
            await session.commit()

            for token in alert.push_tokens:
                if await push.send(token, alert.payload()):
                    sent += 1

        await session.commit()

    return sent


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
