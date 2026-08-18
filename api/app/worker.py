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

        pending = await alerts.due(session, now)
        # Tokens for the whole batch in one query, rather than one per delivery.
        by_account = await alerts.tokens_by_account(
            session, await alerts.accounts_of(session, pending)
        )

        for delivery_id in pending:
            delivery = await alerts.take(session, delivery_id, now)
            if delivery is None:
                # Another worker has it, or has already finished with it.
                continue

            if delivery.kind is alerts.AlertKind.reminder_authority_lost:
                # A different shape of question, so a different path rather than
                # the alert path with exceptions bolted on: one named device
                # instead of an account, and a payload built from the world as
                # it is now instead of the row as it was queued.
                resolved = await alerts.resolve_nudge(session, delivery, now)
                if resolved is alerts.NoNudge.moot:
                    # Authority came back, or there is nothing left to arm.
                    # Retired, not retried — and separately from a failure,
                    # because "the nudge never arrived" and "the nudge stopped
                    # being true" are different answers and only one is a fault.
                    delivery.state = alerts.AlertState.expired.value
                    await session.commit()
                    continue
                if resolved is alerts.NoNudge.awaiting_winner:
                    # The device taking over has not pulled the handover yet,
                    # so the one losing it keeps ringing. Logged because "the
                    # old phone did not stop" is a support question, and the
                    # answer — it was told to keep going, on purpose — is not
                    # guessable from an empty table.
                    log.info(
                        "nudge.awaiting_winner",
                        profile_id=str(delivery.profile_id),
                        device_id=str(delivery.device_id),
                        queued_at=delivery.expires_at.isoformat(),
                    )
                    alerts.defer(delivery, now, alerts.AWAITING_WINNER_WAIT)
                    await session.commit()
                    continue
                if resolved is alerts.NoNudge.no_token:
                    log.info(
                        "nudge.no_token",
                        profile_id=str(delivery.profile_id),
                        device_id=str(delivery.device_id),
                    )
                    alerts.defer(delivery, now, alerts.NO_TOKEN_WAIT)
                    await session.commit()
                    continue

                outcome = await alerts.deliver_nudge(push, delivery, resolved)
                alerts.record(delivery, outcome, now)
                await session.commit()
                continue

            tokens = by_account.get(delivery.account_id, ())
            if not tokens:
                # Nobody to tell right now. Not a failure of this attempt — the
                # caregiver may simply not have opened the app yet — so it waits
                # rather than burning one of the alert's attempts. It has to be
                # pushed forward, though: leaving next_attempt_at alone had the
                # row picked up, locked and released on every pass for its whole
                # TTL, which is several hundred times to learn the same thing.
                #
                # Said out loud, because staying quiet about it hid a real
                # failure for two days: a caregiver whose device never
                # registered a token looks, from every server-side view, exactly
                # like a caregiver with nothing to be told. The alert sat here
                # until it expired and nobody learned why the phone was silent.
                log.warning(
                    "alert.no_recipient",
                    account_id=str(delivery.account_id),
                    profile_id=str(delivery.profile_id),
                    kind=delivery.kind.value,
                    waiting_since=delivery.expires_at.isoformat(),
                )
                alerts.defer(delivery, now, alerts.NO_TOKEN_WAIT)
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
