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

import enum
import uuid
from collections.abc import Sequence
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

    # Only for `reminder_authority_lost`, which concerns one device rather than
    # an account. Null for the caregiver alerts, and see the column comment for
    # why that must stay so.
    device_id: uuid.UUID | None = None

    # It used to carry `push_tokens` and a `payload()`, and nothing read either
    # once detection and delivery were split — the worker resolves both from the
    # stored row, because by then the object that detected the alert is gone.
    # Removed rather than left: fields nothing reads are how a payload comes to
    # be missing three of its own in the first place.


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
    return (await tokens_by_account(session, [account_id])).get(account_id, ())


async def tokens_by_account(
    session: AsyncSession, account_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, ...]]:
    """Push tokens for many accounts at once.

    One query for the whole scan rather than one per watched profile. The
    per-account form is kept for single callers, and is this with a list of one.
    """
    if not account_ids:
        return {}
    rows = (
        await session.execute(
            select(Device.account_id, Device.push_token).where(
                Device.account_id.in_(set(account_ids)),
                Device.revoked_at.is_(None),
                Device.push_token.is_not(None),
            )
        )
    ).all()
    out: dict[uuid.UUID, list[str]] = {}
    for account_id, token in rows:
        if token:
            out.setdefault(account_id, []).append(token)
    return {k: tuple(v) for k, v in out.items()}


async def find_missed_dose_alerts(session: AsyncSession, now: datetime) -> list[Alert]:
    """Doses the profile's own device reported missed, once the wait is over.

    Reported, not inferred. The server never decides a dose was missed from the
    absence of a confirmation — it cannot tell that apart from a phone that has
    not synced, and guessing would produce alerts about people who took their
    medication perfectly well.
    """
    watchers = await _watchers(session)
    if not watchers:
        return []

    # Three queries for the whole scan, not three per watched profile. The
    # per-membership threshold still applies — it is configurable, so the widest
    # window is fetched and each membership's own cutoff applied in Python. That
    # keeps the query count flat as families are added.
    floor_ms = int((now - TTL[AlertKind.dose_missed]).timestamp() * 1000)
    widest_ms = int(
        (now - timedelta(minutes=min(m.dose_alert_after_minutes for m in watchers)))
        .timestamp() * 1000
    )

    rows = (
        await session.execute(
            select(DoseEvent.profile_id, DoseEvent.id, DoseEvent.planned_at_ms).where(
                DoseEvent.profile_id.in_({m.profile_id for m in watchers}),
                DoseEvent.status == MISSED,
                DoseEvent.deleted_at_ms.is_(None),
                DoseEvent.planned_at_ms <= widest_ms,
                DoseEvent.planned_at_ms >= floor_ms,
            )
        )
    ).all()

    missed: dict[uuid.UUID, list[tuple[uuid.UUID, int]]] = {}
    for profile_id, dose_id, planned in rows:
        missed.setdefault(profile_id, []).append((dose_id, planned))


    alerts: list[Alert] = []
    for m in watchers:
        cutoff_ms = int(
            (now - timedelta(minutes=m.dose_alert_after_minutes)).timestamp() * 1000
        )
        for dose_id, planned in missed.get(m.profile_id, ()):
            if planned > cutoff_ms:
                continue
            alerts.append(
                Alert(
                    account_id=m.account_id,
                    profile_id=m.profile_id,
                    kind=AlertKind.dose_missed,
                    subject_id=str(dose_id),
                )
            )
    return alerts


async def find_stale_profile_alerts(session: AsyncSession, now: datetime) -> list[Alert]:
    """Profiles whose reminding device has gone quiet.

    The subject is the day the silence falls in, so a phone that stays off for a
    week produces one alert a day rather than one for every scan — the
    uniqueness of the delivery record does the throttling.

    **Which device is the whole point.** This asked the owner's *account* for its
    most recently seen device, so any second device — a tablet, an old phone
    still signed in — kept the answer fresh while the phone that actually arms
    the alarms sat dead in a coat pocket. The signal reported "all is well"
    precisely in the case it exists to catch. Only `owner_device_id` matters:
    that is the one device materialising reminders (spec §1.4), and its silence
    is the only silence that means nobody is being reminded.

    A profile with no `owner_device_id` raises nothing. No device has claimed
    authority, so no alarms are being armed at all — which is worse than stale,
    and a different signal than this one. Worth having; not by pretending it is
    this.
    """
    watchers = await _watchers(session)
    if not watchers:
        return []

    # One query for every reminding device, not one per watched profile.
    seen = dict(
        (
            await session.execute(
                select(Profile.id, Device.last_seen_at)
                .join(Device, Profile.owner_device_id == Device.id)
                .where(
                    Profile.id.in_({m.profile_id for m in watchers}),
                    Device.revoked_at.is_(None),
                )
            )
        ).all()
    )

    alerts: list[Alert] = []
    for m in watchers:
        threshold = now - timedelta(hours=m.stale_alert_after_hours)
        owner_seen = seen.get(m.profile_id)

        # Never seen at all is not stale: the profile has simply not started
        # syncing yet, and greeting a new caregiver with an alarm is wrong. Nor
        # is a profile whose device never claimed authority — see the docstring.
        if owner_seen is None or owner_seen >= threshold:
            continue

        alerts.append(
            Alert(
                account_id=m.account_id,
                profile_id=m.profile_id,
                kind=AlertKind.profile_stale,
                subject_id=now.date().isoformat(),
            )
        )
    return alerts


# How long each signal stays worth delivering. A missed dose is actionable
# while there is still something to do about it — ring, remind, go round. A
# staleness alert is about a day, so it keeps for that day and no longer.
TTL = {
    AlertKind.dose_missed: timedelta(hours=6),
    AlertKind.profile_stale: timedelta(hours=24),
    # An hour. The nudge only makes a device stop sooner than its next pull
    # would; after that the pull says the same thing from the data, and the
    # revision now travels with it, so a late nudge really is redundant rather
    # than assumed to be.
    AlertKind.reminder_authority_lost: timedelta(hours=1),
}

# Waits between attempts. Short at first, because most failures are seconds
# long; spread out afterwards, because the ones that are not are usually
# minutes or hours.
#
# It used to say this runs out well inside the shortest TTL. That stopped being
# true when the authority nudge arrived with a TTL of one hour: its attempts
# land at 0, +1, +6 and +26 minutes, and the fifth would fall past the hour, so
# `record` retires it as expired instead. That is the correct outcome and the
# path is tested — but the sentence promising otherwise had to go, because a
# comment that describes an old arrangement is how the last three defects
# survived review.
BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=20),
    timedelta(minutes=60),
)
MAX_ATTEMPTS = len(BACKOFF) + 1

# How long to wait when the caregiver has no device to be told on. Not an
# attempt — nothing was tried, and burning the alert's attempts on the absence
# of a phone would use them up before one appears. Without this the row was
# re-selected, locked and released every single scan for the whole of its TTL:
# several hundred passes to discover, each time, that there is still nobody to
# tell.
NO_TOKEN_WAIT = timedelta(minutes=5)


def defer(delivery: AlertDelivery, now: datetime, wait: timedelta) -> None:
    """Put a delivery aside without counting an attempt against it."""
    delivery.next_attempt_at = now + wait


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
            device_id=alert.device_id,
            state=AlertState.pending.value,
            attempts=0,
            next_attempt_at=now,
            expires_at=now + TTL[alert.kind],
        )
        .on_conflict_do_nothing(
            index_elements=["account_id", "profile_id", "kind", "subject_id"]
        )
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


def authority_lost(
    account_id: uuid.UUID,
    profile_id: uuid.UUID,
    device_id: uuid.UUID,
    revision: int,
) -> Alert:
    """The nudge to the device that has just stopped arming a profile's alarms.

    The revision is the subject, which makes each handover its own row: two
    handovers of one profile are two things to say, not one said twice. It is
    also why the unique index cannot collapse them — the same reasoning that put
    `profile_id` in that index after a caregiver watching two parents received
    one alert a day between them.
    """
    return Alert(
        account_id=account_id,
        profile_id=profile_id,
        kind=AlertKind.reminder_authority_lost,
        subject_id=str(revision),
        device_id=device_id,
    )


class NoNudge(str, enum.Enum):
    """Why a queued nudge is not being sent on this pass."""

    # Nothing left to say to this device. Retired rather than retried.
    moot = "moot"
    # The device is reachable in principle but has no token yet. Waits.
    no_token = "no_token"
    # The device taking over has not yet pulled the handover, so the device
    # losing it must keep ringing. Waits.
    awaiting_winner = "awaiting_winner"


# How long to wait before asking again whether the new owner has caught up. The
# worker scans every minute anyway, so this only keeps the row from being locked
# and released on every pass while a phone sleeps through its background period.
# Not an attempt: nothing was tried, and the wait is expected to be minutes.
AWAITING_WINNER_WAIT = timedelta(seconds=60)


@dataclass(frozen=True)
class Nudge:
    token: str
    payload: dict[str, str]
    ttl_seconds: int


async def resolve_nudge(
    session: AsyncSession, delivery: AlertDelivery, now: datetime
) -> Nudge | NoNudge:
    """Build the nudge from the world as it is now, not as it was when queued.

    **This is what makes retries safe, and it was the owner's objection to
    queueing at all.** Store the payload and a retry twenty minutes later says
    "you lost it" using twenty-minute-old facts — so a handover A to B that
    failed to send, followed by B back to A, would tell A it is not the owner
    while A is precisely the owner, and A would fall silent. Rebuilt at send
    time the same retry names the current holder, which is A, addressed to A;
    the message cannot make A stop, and the check below retires it before it is
    even sent.

    So the queue no longer trades a missed nudge for a wrong one. It only ever
    sends what is true at the moment it sends it.

    Four ways there is nothing to say, all of them retirement rather than
    failure: the profile is gone, nobody holds authority, the device we were
    going to tell holds it again, or that device has been signed out and is
    arming nothing anyway.
    """
    profile = await session.get(Profile, delivery.profile_id)
    if profile is None or profile.deleted_at_ms is not None:
        return NoNudge.moot
    if profile.owner_device_id is None or profile.owner_device_id == delivery.device_id:
        return NoNudge.moot

    device = await session.get(Device, delivery.device_id) if delivery.device_id else None
    if device is None or device.revoked_at is not None:
        return NoNudge.moot

    # **The gate, and the reason this signal is worth delaying at all.**
    #
    # Silencing the previous phone before the new one has seen that it took over
    # leaves nobody ringing. Measured 18.08: the nudge reached the losing phone
    # in 1.4 s and it stopped six seconds later, while the winning phone was
    # still waiting on a background pull — 2 min 36 s during which the dose had
    # no alarm on any device. The unfixed build, slower on both halves, had
    # produced 28 s of two phones ringing instead. Both are violations of §1.4,
    # and the ranking between them is not ours: reliability of reminders is
    # invariant 1, so a duplicate is the failure to prefer.
    #
    # So the nudge waits for evidence rather than an assumption. `cursor_seq` is
    # how far the new owner's device has actually been handed rows, and it must
    # have reached the profile as it now stands — not merely the revision at the
    # moment of handover, because a profile written again since would leave that
    # older number satisfied by a pull that never carried the new owner.
    #
    # Null means no pull is known, which holds the nudge. That is the cautious
    # direction: holding it costs a duplicate, releasing it early costs silence.
    owner_device = await session.get(Device, profile.owner_device_id)
    if owner_device is None:
        return NoNudge.moot
    if owner_device.cursor_seq is None or owner_device.cursor_seq < profile.server_seq:
        return NoNudge.awaiting_winner

    if not device.push_token:
        return NoNudge.no_token

    # Bounded by the row's own deadline rather than a fresh hour per attempt.
    # The two are the same question — how long this is worth delivering — and
    # letting a retry outlive the row would have the server stop caring about a
    # message FCM is still holding.
    remaining = int((delivery.expires_at - now).total_seconds())
    if remaining <= 0:
        return NoNudge.moot

    return Nudge(
        token=device.push_token,
        payload={
            "type": delivery.kind.value,
            "profile_id": str(delivery.profile_id),
            "owner_device_id": str(profile.owner_device_id),
            # The profile's position in the change feed, which is also what the
            # device sees on its next pull. One number in both channels, so the
            # device can drop a nudge it has already outrun.
            "revision": str(profile.server_seq),
            "expires_at": delivery.expires_at.isoformat().replace("+00:00", "Z"),
        },
        ttl_seconds=remaining,
    )


async def deliver_nudge(push, delivery: AlertDelivery, nudge: Nudge) -> Delivery:
    """One device, one token, one outcome.

    Deliberately not `deliver`, which reports the best result across every
    token an account has. That rule is right for an alert — reaching one of
    someone's two phones is telling them — and wrong here, where the whole point
    is that exactly one device is being addressed.
    """
    return await push.send(
        nudge.token, nudge.payload, collapse_key(delivery), nudge.ttl_seconds
    )


def collapse_key(delivery: AlertDelivery) -> str:
    """What makes two deliveries of one alert land as one notification.

    The profile is in the key for the same reason it is in the unique index: for
    `profile_stale` the subject is only a date, so a key without it would have
    FCM replace one parent's alert with another's on the caregiver's phone. The
    replacement is silent, and the alert it swallowed is the one about the
    parent nobody has heard from.
    """
    if delivery.kind is AlertKind.reminder_authority_lost:
        # No subject here, and that is the difference: two authority nudges for
        # one profile are not two things a device needs to hear. The later one
        # is the truth and should replace the earlier one while the phone is
        # offline, which is exactly what a shared collapse key buys.
        return f"{delivery.kind.value}:{delivery.profile_id}"
    return f"{delivery.kind.value}:{delivery.profile_id}:{delivery.subject_id}"


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
    # The same TTL the server uses for this signal. If FCM's were shorter it
    # would drop the message while the server still counted it delivered — a
    # loss with no trace on either side.
    ttl_seconds = int(TTL[delivery.kind].total_seconds())
    outcomes = [
        await push.send(
            token, payload_for(delivery), collapse_key(delivery), ttl_seconds
        )
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


async def accounts_of(
    session: AsyncSession, delivery_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    """Which accounts a batch of deliveries is addressed to.

    So the loop can fetch every token it will need in one query instead of one
    per delivery, before it starts locking rows.
    """
    if not delivery_ids:
        return []
    return list(
        (
            await session.execute(
                select(AlertDelivery.account_id)
                .where(AlertDelivery.id.in_(set(delivery_ids)))
                .distinct()
            )
        ).scalars()
    )
