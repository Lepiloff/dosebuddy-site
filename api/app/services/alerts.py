"""Deciding what a watcher should be told.

Kept apart from sending it. The rules here are the part worth testing hard —
who gets told, when, and how often — and separating them from FCM means those
tests need no Firebase and no network.

Both signals exist because collapsing them would lie in one direction or the
other. `dose_missed` says the device reported a dose missed. `profile_stale`
says the device has said nothing, which is not the same claim: treating silence
as a miss cries wolf, and staying quiet lets a genuine miss pass exactly when
the phone is off — the case most worth catching.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.push import Delivery

from app.db.models import (
    AlertDelivery,
    AlertKind,
    AlertState,
    Device,
    DoseEvent,
    Profile,
    ProfileMembership,
    Role,
)

MISSED = "missed"


@dataclass(frozen=True)
class Alert:
    """What a device is to be told, and nothing more.

    No medication name, and no free text. The payload crosses FCM, which is
    Google, and sending article 9 content there for the sake of a notification
    body would hand a third party exactly what the whole design keeps from them.
    The receiving app already holds the data and renders the wording locally.
    """

    account_id: uuid.UUID
    profile_id: uuid.UUID
    kind: AlertKind
    subject_id: str
    push_tokens: tuple[str, ...]

    def payload(self) -> dict[str, str]:
        return {
            "type": self.kind.value,
            "profile_id": str(self.profile_id),
            "subject_id": self.subject_id,
        }


async def _watchers(session: AsyncSession) -> list[ProfileMembership]:
    """Only `with_alerts`. A viewer asked to see, not to be interrupted."""
    return list(
        (
            await session.execute(
                select(ProfileMembership).where(
                    ProfileMembership.role == Role.with_alerts,
                    ProfileMembership.revoked_at.is_(None),
                )
            )
        ).scalars()
    )


async def tokens_for(session: AsyncSession, account_id: uuid.UUID) -> tuple[str, ...]:
    rows = (
        await session.execute(
            select(Device.push_token).where(
                Device.account_id == account_id,
                Device.revoked_at.is_(None),
                Device.push_token.is_not(None),
            )
        )
    ).scalars()
    return tuple(t for t in rows if t)


async def find_missed_dose_alerts(session: AsyncSession, now: datetime) -> list[Alert]:
    """Doses the profile's own device reported missed, once the wait is over.

    Reported, not inferred. The server never decides a dose was missed from the
    absence of a confirmation — it cannot tell that apart from a phone that has
    not synced, and guessing would produce alerts about people who took their
    medication perfectly well.
    """
    alerts: list[Alert] = []

    for m in await _watchers(session):
        cutoff_ms = int(
            (now - timedelta(minutes=m.dose_alert_after_minutes)).timestamp() * 1000
        )
        # And no older than the alert stays worth sending. Without a floor, the
        # first scan after a caregiver is added raises one alert per missed dose
        # in the profile's whole history — a phone full of notifications about
        # last spring, and the one about this morning buried among them. The
        # same window that decides an alert is too old to deliver decides it is
        # too old to raise.
        floor_ms = int((now - TTL[AlertKind.dose_missed]).timestamp() * 1000)
        doses = (
            await session.execute(
                select(DoseEvent.id).where(
                    DoseEvent.profile_id == m.profile_id,
                    DoseEvent.status == MISSED,
                    DoseEvent.deleted_at_ms.is_(None),
                    DoseEvent.planned_at_ms <= cutoff_ms,
                    DoseEvent.planned_at_ms >= floor_ms,
                )
            )
        ).scalars()

        tokens = await tokens_for(session, m.account_id)
        for dose_id in doses:
            alerts.append(
                Alert(
                    account_id=m.account_id,
                    profile_id=m.profile_id,
                    kind=AlertKind.dose_missed,
                    subject_id=str(dose_id),
                    push_tokens=tokens,
                )
            )
    return alerts


async def find_stale_profile_alerts(session: AsyncSession, now: datetime) -> list[Alert]:
    """Profiles whose device has gone quiet.

    The subject is the day the silence falls in, not the profile, so a phone
    that stays off for a week produces one alert a day rather than one for every
    scan — the uniqueness of the delivery record does the throttling.
    """
    alerts: list[Alert] = []

    for m in await _watchers(session):
        threshold = now - timedelta(hours=m.stale_alert_after_hours)

        owner_seen = (
            await session.execute(
                select(Device.last_seen_at)
                .join(Profile, Profile.owner_account_id == Device.account_id)
                .where(Profile.id == m.profile_id, Device.revoked_at.is_(None))
                .order_by(Device.last_seen_at.desc().nullslast())
                .limit(1)
            )
        ).scalar_one_or_none()

        # Never seen at all is not stale: the profile has simply not started
        # syncing yet, and greeting a new caregiver with an alarm is wrong.
        if owner_seen is None or owner_seen >= threshold:
            continue

        alerts.append(
            Alert(
                account_id=m.account_id,
                profile_id=m.profile_id,
                kind=AlertKind.profile_stale,
                subject_id=now.date().isoformat(),
                push_tokens=await tokens_for(session, m.account_id),
            )
        )
    return alerts


# How long each signal stays worth delivering. A missed dose is actionable
# while there is still something to do about it — ring, remind, go round. A
# staleness alert is about a day, so it keeps for that day and no longer.
TTL = {
    AlertKind.dose_missed: timedelta(hours=6),
    AlertKind.profile_stale: timedelta(hours=24),
}

# Waits between attempts. Short at first, because most failures are seconds
# long; spread out afterwards, because the ones that are not are usually
# minutes or hours. Runs out well inside the shortest TTL, so an alert gives up
# for a stated reason rather than by quietly outliving its usefulness.
BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=20),
    timedelta(minutes=60),
)
MAX_ATTEMPTS = len(BACKOFF) + 1


async def claim(session: AsyncSession, alert: Alert, now: datetime) -> bool:
    """Register the alert exactly once, due immediately.

    The insert is the lock, so detection can run every minute and raise the same
    alert every time without it being told twice. What it no longer does is
    count as delivery: the row starts `pending`, and only a send that FCM
    accepted moves it to `sent`.
    """
    stmt = (
        insert(AlertDelivery)
        .values(
            account_id=alert.account_id,
            profile_id=alert.profile_id,
            kind=alert.kind,
            subject_id=alert.subject_id,
            state=AlertState.pending.value,
            attempts=0,
            next_attempt_at=now,
            expires_at=now + TTL[alert.kind],
        )
        .on_conflict_do_nothing(index_elements=["account_id", "kind", "subject_id"])
        .returning(AlertDelivery.id)
    )
    claimed = (await session.execute(stmt)).scalar_one_or_none()
    return claimed is not None


async def due(session: AsyncSession, now: datetime, limit: int = 200) -> list[uuid.UUID]:
    """Which alerts are waiting to be delivered, oldest first.

    Identifiers, not rows, and no lock. Each one is locked as it is taken, by
    `take`, because the loop commits after every delivery — a lock taken over
    the whole batch would be released by the first of those commits and the rest
    of the batch would silently be unprotected for the remainder of the pass.
    """
    rows = (
        await session.execute(
            select(AlertDelivery.id)
            .where(
                AlertDelivery.state == AlertState.pending.value,
                AlertDelivery.next_attempt_at <= now,
            )
            .order_by(AlertDelivery.next_attempt_at)
            .limit(limit)
        )
    ).scalars()
    return list(rows)


async def take(
    session: AsyncSession, delivery_id: uuid.UUID, now: datetime
) -> AlertDelivery | None:
    """Lock one delivery for sending, or return None if it is no longer ours.

    `SKIP LOCKED` so a second worker — one started by mistake, or two overlapping
    for a moment during a deploy — moves on to another alert instead of waiting
    on this one or, worse, sending it a second time.

    The state is rechecked under the lock: between `due` listing it and this
    locking it, another worker may already have delivered it.
    """
    return (
        await session.execute(
            select(AlertDelivery)
            .where(
                AlertDelivery.id == delivery_id,
                AlertDelivery.state == AlertState.pending.value,
                AlertDelivery.next_attempt_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()


def collapse_key(delivery: AlertDelivery) -> str:
    """What makes two deliveries of one alert land as one notification."""
    return f"{delivery.kind.value}:{delivery.subject_id}"


def payload_for(delivery: AlertDelivery) -> dict[str, str]:
    """The same three fields Alert carries, rebuilt from the stored row.

    Built here rather than kept on the Alert, because by the time an alert is
    delivered the object that detected it is long gone — and it must still carry
    no medication name.
    """
    return {
        "type": delivery.kind.value,
        "profile_id": str(delivery.profile_id),
        "subject_id": delivery.subject_id,
    }


async def deliver(push, delivery: AlertDelivery, tokens: tuple[str, ...]) -> Delivery:
    """Send to every device the caregiver has, and report the best outcome.

    Best, not worst: reaching one of someone's two phones is telling them. Only
    when nothing succeeded does the distinction between "try later" and "these
    tokens are dead" decide what happens next, and a single retryable failure is
    enough to keep the alert alive.
    """
    outcomes = [
        await push.send(token, payload_for(delivery), collapse_key(delivery))
        for token in tokens
    ]
    if Delivery.ok in outcomes:
        return Delivery.ok
    if Delivery.retry in outcomes:
        return Delivery.retry
    return Delivery.gone


def record(delivery: AlertDelivery, outcome: Delivery, now: datetime) -> None:
    """Move a delivery on by what the send actually did.

    Expiry is checked before backoff: an alert whose remaining attempts would
    all land after it stopped being useful should say so, rather than retrying
    into a window where nobody wants the answer any more.
    """
    delivery.attempts += 1

    if outcome is Delivery.ok:
        delivery.state = AlertState.sent.value
        delivery.sent_at = now
        delivery.last_error = None
        return

    delivery.last_error = outcome.value

    if outcome is Delivery.gone:
        # Every token for this account refused it. Nothing is reachable, and
        # trying the same tokens again cannot change that.
        delivery.state = AlertState.given_up.value
        return

    if delivery.attempts >= MAX_ATTEMPTS:
        delivery.state = AlertState.given_up.value
        return

    nxt = now + BACKOFF[delivery.attempts - 1]
    if nxt >= delivery.expires_at:
        delivery.state = AlertState.expired.value
        return

    delivery.next_attempt_at = nxt


async def expire(session: AsyncSession, now: datetime) -> int:
    """Retire alerts that outlived their usefulness while waiting.

    Separate from `record` because an alert can expire without any attempt
    failing — the worker being down for a day does exactly that, and those rows
    must not sit `pending` for ever once it comes back.
    """
    result = await session.execute(
        sa_update(AlertDelivery)
        .where(
            AlertDelivery.state == AlertState.pending.value,
            AlertDelivery.expires_at <= now,
        )
        .values(state=AlertState.expired.value)
    )
    return result.rowcount or 0
