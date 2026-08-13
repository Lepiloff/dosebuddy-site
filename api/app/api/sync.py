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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import cast as sa_cast
from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy import true as sa_true
from sqlalchemy import tuple_ as sa_tuple
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.deps import Caller, current_caller, get_session
from app.api.schemas import Changes, Outcome, PullOut, PushIn, PushOut
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

# nginx caps the body at 2 MB (deploy/nginx/conf.d/20-api.conf); this caps the
# record count, which is the limit a client can actually plan against. Both are
# stated in the contract so a batch is sized rather than discovered.
MAX_PUSH_RECORDS = 1000


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
    """Insert, or update only when the incoming write is genuinely newer.

    Last-write-wins on the whole record. Merging field by field would produce
    states that existed on no device — a medication with one phone's dose and
    another's form — and for two phones editing the same row at the same moment
    the merge is imaginary while the damage is real.

    Newer is decided by **(updated_at, origin_device_id, op_seq)**, not by
    `updated_at` alone, and that matters twice over.

    Two edits to one row inside a single millisecond are ordinary. Comparing
    timestamps only, the second is either dropped as a duplicate (strictly
    greater) or applied while a genuine resend is also applied (greater or
    equal) — one loses data, the other churns. With the operation's own
    identity in the comparison, a resend is an exact tie and does nothing,
    while a second edit carries a higher op_seq and lands.

    It also makes a tie between two devices deterministic rather than a race
    settled by whichever request arrived last.
    """
    stmt = insert(model).values(**values, server_seq=NEXT_SEQ)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[model.id],
        set_={
            **{k: getattr(excluded, k) for k in values if k != "id"},
            "server_seq": NEXT_SEQ,
        },
        where=sa_tuple(
            excluded.updated_at_ms, excluded.origin_device_id, excluded.op_seq
        ) > sa_tuple(model.updated_at_ms, model.origin_device_id, model.op_seq),
    )
    await session.execute(stmt)


def _sync_values(row, device_id: uuid.UUID) -> dict[str, Any]:
    return {
        "id": row.id,
        "created_at_ms": row.created_at,
        "updated_at_ms": row.updated_at,
        "deleted_at_ms": row.deleted_at,
        # From the token, never the body: a device must not write under another
        # device's identity, and the tie-breaker would be meaningless if it could.
        "origin_device_id": device_id,
        "op_seq": row.op_seq,
    }


@router.post("/sync/push", response_model=PushOut)
async def push(
    body: PushIn,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> PushOut:
    changes: Changes = body.changes

    total = sum(
        len(getattr(changes, name))
        for name in ("profiles", "medications", "schedules", "dose_events", "stock_events")
    )
    if total > MAX_PUSH_RECORDS:
        # Refused whole rather than truncated. A partially applied batch leaves
        # the client believing it sent everything, and the missing rows are only
        # noticed as data that quietly never arrived.
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "batch_too_large")

    profiles = await visible_profiles(session, caller)
    mine = owned_ids(profiles)
    rejected: list[Outcome] = []
    retry: list[Outcome] = []
    dev = caller.device_id

    def refuse(entity: str, row_id: uuid.UUID, code: str) -> None:
        """Final. Sending this again will not help."""
        rejected.append(Outcome(id=row_id, entity=entity, code=code))

    def later(entity: str, row_id: uuid.UUID, code: str) -> None:
        """Not the client's fault. The same record will land once its parent has."""
        retry.append(Outcome(id=row_id, entity=entity, code=code))

    async def apply(entity: str, model, row_id: uuid.UUID, values: dict[str, Any]) -> None:
        # A savepoint per record, so one failure does not poison the batch. A
        # foreign key that is not there yet is the common case — the parent is
        # simply on a later page or a later push — and it is recoverable, so it
        # is reported as retry rather than as the client having done something
        # wrong.
        try:
            async with session.begin_nested():
                await _upsert(session, model, values)
        except IntegrityError:
            later(entity, row_id, "missing_parent")
        except SQLAlchemyError:
            later(entity, row_id, "conflict")

    # Parents before children: foreign keys need it, and a medication arriving
    # in the same batch as its schedule has to exist before the schedule can be
    # checked against it.
    for p in changes.profiles:
        if p.id in profiles and p.id not in mine:
            refuse("profiles", p.id, "forbidden_role")
            continue
        await apply("profiles", Profile, p.id, {
            **_sync_values(p, dev),
            "owner_account_id": caller.account.id,
            "name": p.name,
            "color": p.color,
            "sort_order": p.sort_order,
        })
        mine.add(p.id)

    for m in changes.medications:
        if m.profile_id not in mine:
            refuse("medications", m.id, "forbidden_role")
            continue
        await apply("medications", Medication, m.id, {
            **_sync_values(m, dev),
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
        })

    med_owner = await _medication_profiles(session, changes)

    for sc in changes.schedules:
        owner_profile = med_owner.get(sc.medication_id)
        if owner_profile is None:
            later("schedules", sc.id, "missing_parent")
            continue
        if owner_profile not in mine:
            refuse("schedules", sc.id, "forbidden_role")
            continue
        await apply("schedules", Schedule, sc.id, {
            **_sync_values(sc, dev),
            "medication_id": sc.medication_id,
            "type": sc.type,
            "times": sc.times,
            "days_of_week": sc.days_of_week,
            "interval_days": sc.interval_days,
            "start_date": sc.start_date,
            "end_date": sc.end_date,
        })

    for d in changes.dose_events:
        if d.profile_id not in mine:
            refuse("dose_events", d.id, "forbidden_role")
            continue
        await apply("dose_events", DoseEvent, d.id, {
            **_sync_values(d, dev),
            "schedule_id": d.schedule_id,
            "medication_id": d.medication_id,
            "profile_id": d.profile_id,
            "planned_at_ms": d.planned_at,
            "status": d.status,
            "action_at_ms": d.action_at,
            "snooze_count": d.snooze_count,
            "snoozed_until_ms": d.snoozed_until,
            "dose_amount": d.dose_amount,
        })

    for e in changes.stock_events:
        owner_profile = med_owner.get(e.medication_id)
        if owner_profile is None:
            later("stock_events", e.id, "missing_parent")
            continue
        if owner_profile not in mine:
            refuse("stock_events", e.id, "forbidden_role")
            continue
        await apply("stock_events", StockEvent, e.id, {
            **_sync_values(e, dev),
            "medication_id": e.medication_id,
            "delta": e.delta,
            "reason": e.reason,
            "dose_event_id": e.dose_event_id,
        })

    await session.commit()

    high = (await session.execute(sql_text("SELECT last_value FROM server_seq"))).scalar_one()
    return PushOut(cursor=encode_cursor(int(high)), rejected=rejected, retry=retry)


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
    "profiles": ("id", "name", "role"),
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


def _row_to_wire(entity: str, row, role: Role | None = None) -> dict[str, Any]:
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
            # The caller's own role on this profile. Not a column — role lives
            # on the (account ↔ profile) link — but the client cannot decide
            # anything without it, and the thing it decides is P0.
            #
            # Without it, a watcher sees a profile with no owner_device_id and
            # cannot tell that from a profile of its own that nobody has claimed
            # yet, so it would arm alarms for someone else's doses. The rule it
            # enables has no ambiguous branch:
            #
            #   role != owner                      never arm
            #   role == owner, owner_device_id nil claim authority, then arm
            #   role == owner, id == this device   arm
            #   role == owner, id == another       do not arm
            "role": role.value if role else None,
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


def page_query(model, key_column, ids, since: int):
    """One ordered index range per key, merged — never a filtered scan.

    The obvious spelling, `WHERE key IN (...) AND server_seq > :since ORDER BY
    server_seq LIMIT n`, cannot walk an index on `(key, server_seq)` and keep the
    ordering, so Postgres falls back to one of two bad plans and picks between
    them on estimates. Measured on 745k dose events across 600 profiles:

      - dense key: walks the `server_seq` index and discards. 11,920 rows thrown
        away to return 500. The waste is proportional to how small a share of the
        table the caller owns, so it grows with *other people's* data — the one
        thing a tenant cannot do anything about.
      - sparse key: reads every row the key has and then sorts. The LIMIT stops
        nothing, so a caregiver's first pull materialises their whole history
        before the first page can be returned.

    Expanding the keys into a LATERAL takes the choice away. Every branch is an
    ordered range scan on `(key, server_seq)` that stops at the limit, so the
    work is bounded by keys × PAGE_SIZE and no longer depends on the size of the
    table. Same 500 rows, 2.3 ms against 10.2 ms, and flat as the table grows.

    Module level rather than a closure so that test_pull_plan.py can assert the
    plan of the statement production actually runs, not of a copy of it.
    """
    wanted = func.unnest(sa_cast(list(ids), ARRAY(PgUUID(as_uuid=True)))).alias("wanted")
    page = (
        select(model)
        .where(key_column == wanted.column, model.server_seq > since)
        .order_by(model.server_seq)
        .limit(PAGE_SIZE + 1)
        .lateral("page")
    )
    row = aliased(model, page)
    return (
        select(row)
        .select_from(wanted)
        .join(page, sa_true())
        .order_by(row.server_seq)
        .limit(PAGE_SIZE + 1)
    )


@router.get("/sync/pull", response_model=PullOut)
async def pull(
    cursor: str | None = Query(default=None),
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> PullOut:
    since = decode_cursor(cursor)
    profiles = await visible_profiles(session, caller)
    # Sent on every response, including this one. A caller who can see nothing
    # gets an empty map, which is the honest answer rather than a missing field.
    roles = {str(pid): role.value for pid, role in profiles.items()}

    if not profiles:
        return PullOut(cursor=encode_cursor(since), has_more=False, changes={}, roles=roles)

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
            await session.execute(page_query(model, profile_column, ids, since))
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

        wire = _row_to_wire(entity, row, role)
        if role is not Role.owner:
            projected = _project(entity, wire)
            if projected is None:
                continue
            wire = projected
        changes.setdefault(entity, []).append(wire)

    new_cursor = page[-1][0] if page else since
    return PullOut(
        cursor=encode_cursor(new_cursor), has_more=has_more, changes=changes, roles=roles
    )
