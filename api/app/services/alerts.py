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
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AlertDelivery,
    AlertKind,
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


async def _tokens_for(session: AsyncSession, account_id: uuid.UUID) -> tuple[str, ...]:
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
        doses = (
            await session.execute(
                select(DoseEvent.id).where(
                    DoseEvent.profile_id == m.profile_id,
                    DoseEvent.status == MISSED,
                    DoseEvent.deleted_at_ms.is_(None),
                    DoseEvent.planned_at_ms <= cutoff_ms,
                )
            )
        ).scalars()

        tokens = await _tokens_for(session, m.account_id)
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
                push_tokens=await _tokens_for(session, m.account_id),
            )
        )
    return alerts


async def claim(session: AsyncSession, alert: Alert) -> bool:
    """Record the delivery first, and send only if this is the one that claimed it.

    The insert is the lock. Sending first and recording afterwards would repeat
    the alert whenever the process died in between — and a caregiver woken twice
    for the same dose learns to ignore the next one, which is the failure that
    matters here.
    """
    stmt = (
        insert(AlertDelivery)
        .values(
            account_id=alert.account_id,
            profile_id=alert.profile_id,
            kind=alert.kind,
            subject_id=alert.subject_id,
            sent_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(index_elements=["account_id", "kind", "subject_id"])
        .returning(AlertDelivery.id)
    )
    claimed = (await session.execute(stmt)).scalar_one_or_none()
    return claimed is not None
