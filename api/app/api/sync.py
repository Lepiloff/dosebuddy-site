"""Sync: push and pull.

Two ideas carry the whole design, and both are easy to get wrong in a way that
does not show up until there is real data.

**The cursor is a server-assigned sequence, never a timestamp.** Phone clocks
run backwards, and two rows sharing a millisecond at a page boundary are lost
permanently and silently. Every mirrored table draws from one Postgres sequence,
so a single cursor covers the whole stream and ordering is total.

**What a caller may read depends on the role on the link, not on the account.**
A watched profile yields no schedules at all — not to save bandwidth, but so
that the caregiver's device has nothing to materialise an alarm from. The
one-reminder-owner invariant (spec §1.4) then holds by construction rather than
by suppression code being correct, and a bug in this file cannot make the wrong
phone ring.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Caller, current_caller, get_session
from app.api.schemas import Changes, PullOut, PushIn, PushOut, Rejected
from app.db.models import (
    SERVER_SEQ,
    DoseEvent,
    Medication,
    Profile,
    ProfileMembership,
    Role,
    Schedule,
    StockEvent,
)

router = APIRouter(tags=["sync"])

PAGE_SIZE = 500


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(f"seq:{seq}".encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    """An unreadable cursor starts from the beginning rather than failing.

    A client that cannot sync is worse than one that syncs too much: the extra
    rows are idempotent upserts, while a hard error leaves a device stuck with
    no way out short of reinstalling.
    """
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        if not raw.startswith("seq:"):
            return 0
        return int(raw[4:])
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


async def visible_profiles(session: AsyncSession, caller: Caller) -> dict[uuid.UUID, Role]:
    """Every profile this caller can see, and with what role."""
    owned = (
        await session.execute(
            select(Profile.id).where(Profile.owner_account_id == caller.account.id)
        )
    ).scalars()
    result: dict[uuid.UUID, Role] = {pid: Role.owner for pid in owned}

    watched = (
        await session.execute(
            select(ProfileMembership.profile_id, ProfileMembership.role).where(
                ProfileMembership.account_id == caller.account.id,
                ProfileMembership.revoked_at.is_(None),
            )
        )
    ).all()
    for pid, role in watched:
        # Ownership wins if somehow both exist: it is the stronger of the two,
        # and the caller is the one who would be surprised by less.
        result.setdefault(pid, role)
    return result


def owned_ids(profiles: dict[uuid.UUID, Role]) -> set[uuid.UUID]:
    return {pid for pid, role in profiles.items() if role is Role.owner}


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------

NEXT_SEQ = sql_text(f"nextval('{SERVER_SEQ}')")


async def _upsert(session: AsyncSession, model, values: dict[str, Any]) -> None:
    """Insert, or update only when the incoming row is not older.

    Last-write-wins on the device's `updated_at`, whole record at a time. Merging
    field by field would produce states that existed on no device — a medication
    with one phone's dose and another's form — and for two phones in one family
    editing the same row at the same moment, the merge is imaginary while the
    damage is real.
    """
    stmt = insert(model).values(**values, server_seq=NEXT_SEQ)
    stmt = stmt.on_conflict_do_update(
        index_elements=[model.id],
        set_={
            **{k: getattr(stmt.excluded, k) for k in values if k != "id"},
            "server_seq": NEXT_SEQ,
        },
        where=stmt.excluded.updated_at_ms >= model.updated_at_ms,
    )
    await session.execute(stmt)


def _sync_values(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "created_at_ms": row.created_at,
        "updated_at_ms": row.updated_at,
        "deleted_at_ms": row.deleted_at,
    }


@router.post("/sync/push", response_model=PushOut)
async def push(
    body: PushIn,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> PushOut:
    profiles = await visible_profiles(session, caller)
    mine = owned_ids(profiles)
    rejected: list[Rejected] = []
    changes: Changes = body.changes

    def refuse(entity: str, row_id: uuid.UUID, code: str) -> None:
        rejected.append(Rejected(id=row_id, entity=entity, code=code))

    # Order matters twice over: foreign keys need parents first, and a
    # medication arriving in the same batch as its schedule has to exist before
    # the schedule can be checked against it.
    for p in changes.profiles:
        if p.id in profiles and p.id not in mine:
            refuse("profiles", p.id, "forbidden_role")
            continue
        await _upsert(
            session,
            Profile,
            {
                **_sync_values(p),
                "owner_account_id": caller.account.id,
                "name": p.name,
                "color": p.color,
                "sort_order": p.sort_order,
            },
        )
        mine.add(p.id)

    for m in changes.medications:
        if m.profile_id not in mine:
            refuse("medications", m.id, "forbidden_role")
            continue
        await _upsert(
            session,
            Medication,
            {
                **_sync_values(m),
                "profile_id": m.profile_id,
                "name": m.name,
                "notes": m.notes,
                "dosage_text": m.dosage_text,
                "dose_amount": m.dose_amount,
                "form": m.form,
                "pack_size": m.pack_size,
                "refill_threshold_days": m.refill_threshold_days,
                "is_active": m.is_active,
                "photo_key": m.photo_key,
            },
        )

    med_owner = await _medication_profiles(session, changes)

    for s in changes.schedules:
        if med_owner.get(s.medication_id) not in mine:
            refuse("schedules", s.id, "forbidden_role")
            continue
        await _upsert(
            session,
            Schedule,
            {
                **_sync_values(s),
                "medication_id": s.medication_id,
                "type": s.type,
                "times": s.times,
                "days_of_week": s.days_of_week,
                "interval_days": s.interval_days,
                "start_date": s.start_date,
                "end_date": s.end_date,
            },
        )

    for d in changes.dose_events:
        if d.profile_id not in mine:
            refuse("dose_events", d.id, "forbidden_role")
            continue
        await _upsert(
            session,
            DoseEvent,
            {
                **_sync_values(d),
                "schedule_id": d.schedule_id,
                "medication_id": d.medication_id,
                "profile_id": d.profile_id,
                "planned_at_ms": d.planned_at,
                "status": d.status,
                "action_at_ms": d.action_at,
                "snooze_count": d.snooze_count,
                "snoozed_until_ms": d.snoozed_until,
                "dose_amount": d.dose_amount,
            },
        )

    for e in changes.stock_events:
        if med_owner.get(e.medication_id) not in mine:
            refuse("stock_events", e.id, "forbidden_role")
            continue
        await _upsert(
            session,
            StockEvent,
            {
                **_sync_values(e),
                "medication_id": e.medication_id,
                "delta": e.delta,
                "reason": e.reason,
                "dose_event_id": e.dose_event_id,
            },
        )

    await session.commit()

    high = (await session.execute(sql_text("SELECT last_value FROM server_seq"))).scalar_one()
    return PushOut(cursor=encode_cursor(int(high)), rejected=rejected)


async def _medication_profiles(session: AsyncSession, changes: Changes) -> dict[uuid.UUID, uuid.UUID]:
    """Which profile each referenced medication belongs to.

    Read after the medications in this batch are written, so a schedule arriving
    alongside its brand-new medication resolves rather than being refused for
    referring to something that "does not exist".
    """
    ids = {s.medication_id for s in changes.schedules} | {
        e.medication_id for e in changes.stock_events
    }
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Medication.id, Medication.profile_id).where(Medication.id.in_(ids))
        )
    ).all()
    return dict(rows)


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------

# What a watcher gets. Anything not listed here does not leave the server for a
# profile the caller merely watches — schedules and stock_events are absent from
# the table entirely, which is the point.
WATCHER_FIELDS = {
    "profiles": ("id", "name"),
    "medications": ("id", "profile_id", "name", "form"),
    "dose_events": (
        "id",
        "medication_id",
        "profile_id",
        "planned_at",
        "status",
        "action_at",
    ),
}


def _row_to_wire(entity: str, row) -> dict[str, Any]:
    common = {
        "id": str(row.id),
        "updated_at": getattr(row, "updated_at_ms", None),
        "deleted_at": getattr(row, "deleted_at_ms", None),
        "created_at": getattr(row, "created_at_ms", None),
    }
    if entity == "profiles":
        return {
            **common,
            "name": row.name,
            "color": row.color,
            "sort_order": row.sort_order,
            # Which device arms the alarms for this profile (spec §1.4). This is
            # the authoritative answer: a device that pulls and finds an id
            # other than its own stops. The push sent when authority moves is
            # only a nudge, and a nudge that is lost must not leave two phones
            # ringing for one dose.
            "owner_device_id": str(row.owner_device_id) if row.owner_device_id else None,
        }
    if entity == "medications":
        return {
            **common,
            "profile_id": str(row.profile_id),
            "name": row.name,
            "notes": row.notes,
            "dosage_text": row.dosage_text,
            "dose_amount": row.dose_amount,
            "form": row.form,
            "pack_size": row.pack_size,
            "refill_threshold_days": row.refill_threshold_days,
            "is_active": row.is_active,
            "photo_key": row.photo_key,
        }
    if entity == "schedules":
        return {
            **common,
            "medication_id": str(row.medication_id),
            "type": row.type,
            "times": row.times,
            "days_of_week": row.days_of_week,
            "interval_days": row.interval_days,
            "start_date": row.start_date,
            "end_date": row.end_date,
        }
    if entity == "dose_events":
        return {
            **common,
            "schedule_id": str(row.schedule_id) if row.schedule_id else None,
            "medication_id": str(row.medication_id),
            "profile_id": str(row.profile_id),
            "planned_at": row.planned_at_ms,
            "status": row.status,
            "action_at": row.action_at_ms,
            "snooze_count": row.snooze_count,
            "snoozed_until": row.snoozed_until_ms,
            "dose_amount": row.dose_amount,
        }
    return {
        **common,
        "medication_id": str(row.medication_id),
        "delta": row.delta,
        "reason": row.reason,
        "dose_event_id": str(row.dose_event_id) if row.dose_event_id else None,
    }


def _project(entity: str, wire: dict[str, Any]) -> dict[str, Any] | None:
    """Cut a row down to what a watcher may see, or drop it entirely."""
    allowed = WATCHER_FIELDS.get(entity)
    if allowed is None:
        return None
    return {k: v for k, v in wire.items() if k in allowed or k in ("updated_at", "deleted_at")}


@router.get("/sync/pull", response_model=PullOut)
async def pull(
    cursor: str | None = Query(default=None),
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> PullOut:
    since = decode_cursor(cursor)
    profiles = await visible_profiles(session, caller)
    if not profiles:
        return PullOut(cursor=encode_cursor(since), has_more=False, changes={})

    mine = owned_ids(profiles)
    all_ids = set(profiles)

    # Every table draws from one sequence, so rows are merged and then cut at a
    # global boundary. Cutting per table would advance the cursor past rows in
    # another table that had not been sent yet — and those rows would never be
    # seen again, because the cursor only moves forward.
    collected: list[tuple[int, str, Any]] = []

    async def gather(entity: str, model, profile_column, ids: set[uuid.UUID]) -> None:
        if not ids:
            return
        rows = (
            await session.execute(
                select(model)
                .where(model.server_seq > since, profile_column.in_(ids))
                .order_by(model.server_seq)
                .limit(PAGE_SIZE + 1)
            )
        ).scalars()
        for r in rows:
            collected.append((r.server_seq, entity, r))

    await gather("profiles", Profile, Profile.id, all_ids)
    await gather("medications", Medication, Medication.profile_id, all_ids)
    await gather("dose_events", DoseEvent, DoseEvent.profile_id, all_ids)
    # Schedules and stock events reach only the owner, and they hang off a
    # medication rather than a profile, so the visible set is resolved through it.
    if mine:
        med_ids = (
            await session.execute(
                select(Medication.id).where(Medication.profile_id.in_(mine))
            )
        ).scalars().all()
        if med_ids:
            await gather("schedules", Schedule, Schedule.medication_id, set(med_ids))
            await gather("stock_events", StockEvent, StockEvent.medication_id, set(med_ids))

    collected.sort(key=lambda t: t[0])
    has_more = len(collected) > PAGE_SIZE
    page = collected[:PAGE_SIZE]

    changes: dict[str, list[dict]] = {}
    for _seq, entity, row in page:
        # Schedules and stock events are gathered only for owned medications, so
        # they carry no role question. The other three name a profile, and the
        # role on that link decides what leaves the server.
        if entity in ("profiles", "medications", "dose_events"):
            profile_id = row.id if entity == "profiles" else row.profile_id
            role = profiles.get(profile_id, Role.owner)
        else:
            role = Role.owner

        wire = _row_to_wire(entity, row)
        if role is not Role.owner:
            projected = _project(entity, wire)
            if projected is None:
                continue
            wire = projected
        changes.setdefault(entity, []).append(wire)

    new_cursor = page[-1][0] if page else since
    return PullOut(cursor=encode_cursor(new_cursor), has_more=has_more, changes=changes)
