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
from collections import Counter
from datetime import timedelta
from typing import Any

import structlog

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import cast as sa_cast
from sqlalchemy import column as sa_column
from sqlalchemy import func, literal as sa_literal, select, union_all
from sqlalchemy import text as sql_text
from sqlalchemy import update as sa_update
from sqlalchemy import true as sa_true
from sqlalchemy import tuple_ as sa_tuple
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.deps import Caller, get_session, sync_caller
from app.services.alerts import _profile_high_water
from app.api.schemas import Changes, Outcome, PullOut, PushIn, PushOut
from app.db.models import (
    IMMUTABLE_PARENT_SQLSTATE,
    SERVER_SEQ,
    Device,
    DeviceReadiness,
    DoseEvent,
    Medication,
    Profile,
    ProfileMembership,
    Role,
    Schedule,
    StockEvent,
    utcnow,
)

log = structlog.get_logger(__name__)

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


def _sqlstate(exc: DBAPIError) -> str | None:
    """The five characters Postgres sent, whatever the driver wrapped them in.

    Read from the code and never from the message: the message is written to be
    read by a person and is free to change, while the code is the part the
    schema and this file agreed on.
    """
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


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
    caller: Caller = Depends(sync_caller),
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
        except DBAPIError as exc:
            if _sqlstate(exc) == IMMUTABLE_PARENT_SQLSTATE:
                # The row asked to change its parent and the database refused
                # the statement whole (models.py, migration 0014). Nothing of
                # this record was written, which is the point: the client keeps
                # one version per row and cannot apply half of one.
                #
                # Reached when the row was not on the server at the start of
                # this batch but is by the time it is written — another device
                # created it in between, or it arrives twice in this same batch.
                # The check before the loop catches every other case earlier.
                # `profiles` says it differently: the row on disk belongs to
                # another account, which is a fact about permission rather than
                # about a link between two rows of ours.
                refuse(
                    entity,
                    row_id,
                    "forbidden_role" if entity == "profiles" else "immutable_parent",
                )
            elif isinstance(exc, IntegrityError):
                later(entity, row_id, "missing_parent")
            else:
                later(entity, row_id, "conflict")
        except SQLAlchemyError:
            later(entity, row_id, "conflict")

    batch_profiles = [p.id for p in changes.profiles]
    stored_owner = await _stored_parents(
        session, Profile, (Profile.owner_account_id,), batch_profiles
    )

    # Parents before children: foreign keys need it, and a medication arriving
    # in the same batch as its schedule has to exist before the schedule can be
    # checked against it.
    for p in changes.profiles:
        if _moved(stored_owner, p.id, caller.account.id):
            # The row on disk belongs to another account. The check this
            # replaced asked whether the caller could *see* the profile, which
            # let through the one caller who most obviously must not write it: a
            # caregiver whose access was revoked still knows the id, and no
            # longer sees the profile the revocation took away. Pushing the row
            # then rewrote its owner to them, and with it went the schedules a
            # watcher is never sent.
            refuse("profiles", p.id, "forbidden_role")
            continue
        await apply("profiles", Profile, p.id, {
            **_sync_values(p, dev),
            "owner_account_id": caller.account.id,
            "name": p.name,
            "color": p.color,
            "sort_order": p.sort_order,
        })

    # What the caller may write under is what the database says it owns, not
    # what `apply` failed to complain about.
    #
    # `apply` swallows its outcome by design — one bad row must not cost the
    # batch — so a profile that was refused, or that silently lost
    # last-write-wins to a row another account created between the read above
    # and this write, used to be added to `mine` all the same. Every child of it
    # in the same batch was then free to write itself into somebody else's
    # profile, and did: measured 2026-09-09, the medication landed.
    settled = await _stored_parents(
        session, Profile, (Profile.owner_account_id,), batch_profiles
    )
    mine |= {pid for pid, (owner,) in settled.items() if owner == caller.account.id}

    # A profile this server has never seen is a parent that has not arrived, not
    # a parent that belongs to somebody else.
    #
    # Both used to answer `forbidden_role`, which is final, and the app track
    # measured what that costs: their outbox can put a medication in one batch
    # and the profile it belongs to in the next, and the medication then came
    # back finally refused. The client quarantines a final refusal, a refused
    # row keeps its server_seq so no pull ever brings it back, and the row lived
    # on one phone from then on. Silently — the failure of `retry` is a row that
    # waits, the failure of `refuse` is a row that is gone.
    #
    # Telling the two apart says whether a profile id exists, which is the
    # objection to doing it. It does not hold here: `_medication_profiles`
    # already answers exactly that question for every medication id on every
    # push, and has since the beginning. This makes the profile edge behave like
    # the medication edge rather than adding a new kind of answer, and a v4 uuid
    # is not a space anybody enumerates.
    named = (
        {m.profile_id for m in changes.medications}
        | {d.profile_id for d in changes.dose_events}
    ) - mine
    unknown = named - set(
        await _stored_parents(session, Profile, (Profile.owner_account_id,), list(named))
    )

    def profile_out_of_reach(entity: str, row_id: uuid.UUID, profile_id: uuid.UUID) -> None:
        """Say why a child cannot be written, in the terms the client acts on."""
        if profile_id in unknown:
            later(entity, row_id, "missing_parent")
        else:
            refuse(entity, row_id, "forbidden_role")

    stored_medication_parent = await _stored_parents(
        session, Medication, (Medication.profile_id,), [m.id for m in changes.medications]
    )

    for m in changes.medications:
        if m.profile_id not in mine:
            profile_out_of_reach("medications", m.id, m.profile_id)
            continue
        if _moved(stored_medication_parent, m.id, m.profile_id):
            # A medication does not change profile (contract §4.2). Final: the
            # row will not be accepted however often it is sent, and the client
            # quarantines it rather than retrying.
            #
            # Refused before the write is even attempted, so the answer does not
            # depend on how old the row is. The database refuses the move too,
            # but only when the update actually runs — a write that loses on
            # last-write-wins never reaches it, and would leave the client
            # believing a row the server ignored had been applied.
            refuse("medications", m.id, "immutable_parent")
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

    stored_schedule_parent = await _stored_parents(
        session, Schedule, (Schedule.medication_id,), [sc.id for sc in changes.schedules]
    )

    for sc in changes.schedules:
        owner_profile = med_owner.get(sc.medication_id)
        if owner_profile is None:
            later("schedules", sc.id, "missing_parent")
            continue
        if owner_profile not in mine:
            refuse("schedules", sc.id, "forbidden_role")
            continue
        if _moved(stored_schedule_parent, sc.id, sc.medication_id):
            # Re-pointing a schedule strands the doses already built from it:
            # they keep naming the medication, and the profile, they were
            # materialised for.
            refuse("schedules", sc.id, "immutable_parent")
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

    stored_dose_parents = await _stored_parents(
        session,
        DoseEvent,
        (DoseEvent.profile_id, DoseEvent.medication_id),
        [d.id for d in changes.dose_events],
    )

    for d in changes.dose_events:
        if d.profile_id not in mine:
            profile_out_of_reach("dose_events", d.id, d.profile_id)
            continue
        # The medication is checked too, and it was not checked at all. A dose
        # names three things, and owning the profile it claims said nothing
        # about the medication it points at — which could be any row in the
        # table, including another account's.
        #
        # `schedule_id` is left unchecked: it is nullable, it is set null when
        # the schedule goes, and its ids never leave the owner's own device.
        medication_profile = med_owner.get(d.medication_id)
        if medication_profile is None:
            later("dose_events", d.id, "missing_parent")
            continue
        if medication_profile not in mine:
            refuse("dose_events", d.id, "forbidden_role")
            continue
        if _moved(stored_dose_parents, d.id, d.profile_id, d.medication_id):
            # Both parents, and the second one was the gap this closes. Moving
            # the profile had the widest reach — dose ids are sent to every
            # watcher by design (contract §4.3), so a caregiver could resend one
            # under a profile of their own and the row left the owner's feed for
            # theirs. Moving the *medication* was quieter and worse: reminder
            # authority is read from profile_id, so the phone goes on ringing on
            # time, while the text it reads comes from the medication and the
            # confirmation takes stock off that medication. A reminder for the
            # wrong medicine, and the wrong packet counted down.
            refuse("dose_events", d.id, "immutable_parent")
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

    stored_stock_parent = await _stored_parents(
        session, StockEvent, (StockEvent.medication_id,), [e.id for e in changes.stock_events]
    )

    for e in changes.stock_events:
        owner_profile = med_owner.get(e.medication_id)
        if owner_profile is None:
            later("stock_events", e.id, "missing_parent")
            continue
        if owner_profile not in mine:
            refuse("stock_events", e.id, "forbidden_role")
            continue
        if _moved(stored_stock_parent, e.id, e.medication_id):
            # The stock balance is the sum of the journal (contract §4.2), so a
            # moved event silently changes two of them.
            refuse("stock_events", e.id, "immutable_parent")
            continue
        await apply("stock_events", StockEvent, e.id, {
            **_sync_values(e, dev),
            "medication_id": e.medication_id,
            "delta": e.delta,
            "reason": e.reason,
            "dose_event_id": e.dose_event_id,
        })

    await session.commit()

    if total and len(retry) == total:
        # Every record held, none applied and none refused — so the client's
        # journal loses nothing, and the next page it sends is this page again.
        # One line is not a stall; the same device repeating it is, and that is
        # the only way anyone finds out.
        #
        # Both known causes are the client sending a child ahead of its parent,
        # and both were invisible from here until they were reported: a
        # medication ahead of its profile, and — measured in production on
        # 2026-09-09 — the doses of a medication ahead of the medication, which
        # an ordinary edit is enough to arrange. The app track's fix orders the
        # journal parents-first, so this line should fall silent as that release
        # lands; if it does not, the assumption to check is that one.
        #
        # Identifiers and counts only. Which rows were held says which entities
        # a device is stuck on; what is in them is article 9 material and has no
        # business in a log (core/logging.py).
        log.warning(
            "sync.push_no_progress",
            device_id=str(caller.device_id),
            account_id=str(caller.account.id),
            records=total,
            held=dict(Counter(f"{o.entity}:{o.code}" for o in retry)),
        )

    high = (await session.execute(sql_text("SELECT last_value FROM server_seq"))).scalar_one()
    return PushOut(cursor=encode_cursor(int(high)), rejected=rejected, retry=retry)


def _moved(stored: dict[uuid.UUID, tuple], row_id: uuid.UUID, *claimed) -> bool:
    """True when the row is already on the server under a different parent.

    A row that is not there yet cannot have moved, so the claim stands in for
    the stored value and the answer is no.
    """
    return stored.get(row_id, claimed) != claimed


async def _authority(
    session: AsyncSession, mine: set[uuid.UUID], lease: timedelta | None
) -> dict[str, dict[str, Any]]:
    """Who holds reminders for each owned profile, and whether they can ring.

    On every response, empty ones included, for the same reason `roles` is: it
    is state, not an event. Readiness changes with no row changing — a report
    lands, a lease runs out — so a device whose cursor is past the profile row
    would never learn of it from an incremental feed. The app track made that
    correction, and it is the whole reason this is a block rather than a field.

    Owned profiles only. Which of somebody's phones rings for their mother is
    not a caregiver's business, which is why `owner_device_id` is already absent
    from the watcher projection, and a block that reinstated it beside the
    projection that removes it would be a strange way to keep that promise.
    """
    if not mine:
        return {}

    rows = (
        await session.execute(select(Profile).where(Profile.id.in_(mine)))
    ).scalars().all()
    ready_rows = {
        (r.device_id, r.profile_id): r
        for r in (
            await session.execute(
                select(DeviceReadiness).where(DeviceReadiness.profile_id.in_(mine))
            )
        ).scalars()
    }

    now = utcnow()
    block: dict[str, dict[str, Any]] = {}
    for profile in rows:
        ready = ready_rows.get((profile.owner_device_id, profile.id))
        # Ready means ready for the profile as it stands, not as it stood when
        # the report was made: a dose written since is a dose the owner may not
        # have, and readiness is the claim that it can ring for what exists.
        covered = bool(
            ready
            and ready.revision >= profile.server_seq
            and ready.applied_cursor >= await _profile_high_water(session, profile)
        )
        until = (
            ready.updated_at + lease
            if ready and profile.authority_leased and lease
            else None
        )
        block[str(profile.id)] = {
            "owner_device_id": str(profile.owner_device_id) if profile.owner_device_id else None,
            "pending_device_id": (
                str(profile.pending_owner_device_id)
                if profile.pending_owner_device_id
                else None
            ),
            "previous_device_id": (
                str(profile.previous_owner_device_id)
                if profile.previous_owner_device_id
                else None
            ),
            "revision": str(profile.server_seq),
            "owner_ready": covered,
            "ready_until": until.isoformat().replace("+00:00", "Z") if until else None,
            "as_of": now.isoformat().replace("+00:00", "Z"),
        }
    return block


async def _stored_parents(
    session: AsyncSession, model, columns: tuple, ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple]:
    """Which parents each incoming row already has on the server, where it exists.

    The question `push` used not to ask. Every check here reads the parent a row
    *claims*, which says nothing about whether the caller may touch the row that
    is already there — and for five entities the difference was the whole of the
    authorisation.

    Read without a lock, deliberately. The stale answers this can give are the
    two that cost nothing, because the write is guarded as well (models.py): if
    the row appears after this read, the trigger refuses the move at the moment
    it is written; if the row is read here and hard-deleted before the write,
    the insert that follows creates it under the parent this check approved.
    There is no reading of this map that lets a move through.

    It exists for the case the trigger cannot see: a write that loses on
    last-write-wins is never applied, so no update runs and no trigger fires,
    and a row proposing a new parent would come back neither rejected nor
    applied. The client reads that silence as success.

    More than one column, because a dose has two parents and guarding one of
    them guarded nothing: a dose re-pointed at another medication keeps ringing
    on time and names the wrong medicine while it does it.
    """
    if not ids:
        return {}
    rows = (
        await session.execute(select(model.id, *columns).where(model.id.in_(ids)))
    ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


async def _medication_profiles(session: AsyncSession, changes: Changes) -> dict[uuid.UUID, uuid.UUID]:
    """Which profile each referenced medication belongs to.

    Read after the medications in this batch are written, so a schedule arriving
    alongside its brand-new medication resolves rather than being refused for
    referring to something that "does not exist".
    """
    ids = (
        {s.medication_id for s in changes.schedules}
        | {e.medication_id for e in changes.stock_events}
        | {d.medication_id for d in changes.dose_events}
    )
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
            # The same number the authority nudge carries, so the device can
            # compare the two channels. Without it the device stores nothing to
            # compare against, its "strictly newer" check passes every push, and
            # a retried nudge can undo a handover that has since been redone.
            #
            # A string, because FCM's `data` is map<string,string> and the push
            # side has no choice; one type in both channels means one parse.
            #
            # No new column: server_seq already exists, is already monotonic,
            # and already moves when authority moves. That it also moves on
            # every other write to the profile makes the check conservative in
            # the safe direction — the device keeps the newer number it pulled
            # and drops the older push, which is right, because it already holds
            # the fresher truth.
            "revision": str(row.server_seq),
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


def _key_branch(entity: str, model, key_column, ids, since: int):
    """`(server_seq, entity, id)` for one entity — keys only, no row bodies."""
    wanted = func.unnest(
        sa_cast(list(ids), ARRAY(PgUUID(as_uuid=True)))
    ).alias(f"wanted_{entity}")
    page = (
        select(model.server_seq.label("server_seq"), model.id.label("id"))
        .where(key_column == wanted.column, model.server_seq > since)
        .order_by(model.server_seq)
        .limit(PAGE_SIZE + 1)
        .lateral(f"page_{entity}")
    )
    return (
        select(
            page.c.server_seq,
            sa_literal(entity).label("entity"),
            page.c.id,
        )
        .select_from(wanted)
        .join(page, sa_true())
    )


def feed_query(plan, since: int):
    """The page boundary, decided once across every entity.

    `plan` is a sequence of `(entity, model, key_column, ids)`.

    Fetching each entity separately and cutting afterwards was correct but paid
    for far more than it kept: every entity returned up to PAGE_SIZE rows *per
    visible profile*, all of them fully hydrated — encrypted names, notes, the
    lot — and then Python threw away everything past the first 500. A caregiver
    watching four profiles moved around twelve thousand rows to be sent five
    hundred, and the excess grew with both the number of profiles and the number
    of entities.

    So the boundary is decided on keys alone: `(server_seq, entity, id)` is
    narrow, Postgres merges the branches and stops at the limit, and only the
    rows that survived the cut are then read in full. What crosses the wire is
    unchanged — same rows, same order, same cursor — which is what the pull
    tests assert.
    """
    branches = [
        _key_branch(entity, model, key_column, ids, since)
        for entity, model, key_column, ids in plan
        if ids
    ]
    if not branches:
        return None
    keys = branches[0] if len(branches) == 1 else union_all(*branches)
    return (
        select(keys.subquery().alias("feed"))
        .order_by(sa_column("server_seq"))
        .limit(PAGE_SIZE + 1)
    )


async def _page(
    session: AsyncSession, caller: Caller, cursor: str | None, lease: timedelta | None
) -> tuple[PullOut, int]:
    """One page of the feed, and where it ends.

    The body of both `/sync/pull` and `/sync/preview`, which differ by exactly
    one thing: whether the device is recorded as having been handed the page.
    Kept as one function so the difference stays one line rather than two
    implementations that agree today.
    """
    since = decode_cursor(cursor)
    profiles = await visible_profiles(session, caller)
    # Sent on every response, including this one. A caller who can see nothing
    # gets an empty map, which is the honest answer rather than a missing field.
    roles = {str(pid): role.value for pid, role in profiles.items()}

    if not profiles:
        return (
            PullOut(cursor=encode_cursor(since), has_more=False, changes={}, roles=roles),
            since,
        )

    mine = owned_ids(profiles)
    all_ids = set(profiles)

    # Every table draws from one sequence, so the whole stream is cut at one
    # global boundary. Cutting per table would advance the cursor past rows in
    # another table that had not been sent yet — and those rows would never be
    # seen again, because the cursor only moves forward.
    plan: list[tuple[str, Any, Any, set[uuid.UUID]]] = [
        ("profiles", Profile, Profile.id, all_ids),
        ("medications", Medication, Medication.profile_id, all_ids),
        ("dose_events", DoseEvent, DoseEvent.profile_id, all_ids),
    ]
    # Schedules and stock events reach only the owner, and they hang off a
    # medication rather than a profile, so the visible set is resolved through it.
    if mine:
        med_ids = set(
            (
                await session.execute(
                    select(Medication.id).where(Medication.profile_id.in_(mine))
                )
            ).scalars()
        )
        if med_ids:
            plan.append(("schedules", Schedule, Schedule.medication_id, med_ids))
            plan.append(("stock_events", StockEvent, StockEvent.medication_id, med_ids))

    feed = feed_query(plan, since)
    keys = list((await session.execute(feed)).all()) if feed is not None else []

    has_more = len(keys) > PAGE_SIZE
    wanted_keys = keys[:PAGE_SIZE]

    # Only now are the rows themselves read, and only the ones that survived the
    # cut. Reading them before deciding the boundary meant hydrating up to a
    # page per entity per profile and discarding almost all of it.
    models = {entity: model for entity, model, _key, _ids in plan}
    by_entity: dict[str, list[uuid.UUID]] = {}
    for _seq, entity, row_id in wanted_keys:
        by_entity.setdefault(entity, []).append(row_id)

    loaded: dict[tuple[str, uuid.UUID], Any] = {}
    for entity, ids in by_entity.items():
        model = models[entity]
        for row in (
            await session.execute(select(model).where(model.id.in_(ids)))
        ).scalars():
            loaded[(entity, row.id)] = row

    page = [
        (seq, entity, loaded[(entity, row_id)])
        for seq, entity, row_id in wanted_keys
        if (entity, row_id) in loaded
    ]

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

    return (
        PullOut(
            cursor=encode_cursor(new_cursor),
            has_more=has_more,
            changes=changes,
            roles=roles,
            authority=await _authority(session, mine, lease),
        ),
        new_cursor,
    )


@router.get("/sync/pull", response_model=PullOut)
async def pull(
    request: Request,
    cursor: str | None = Query(default=None),
    ready: bool = Query(
        default=False,
        description="This client reports readiness per profile; gate on that, not on the cursor.",
    ),
    caller: Caller = Depends(sync_caller),
    session: AsyncSession = Depends(get_session),
) -> PullOut:
    page, reached = await _page(
        session, caller, cursor, request.app.state.settings.authority_lease
    )

    # Remember how far this device has been handed. The cursor is still the
    # client's — this is a copy of the last value it was given, kept for one
    # decision: whether the device taking over a profile has seen that it did.
    # Until it has, the previous phone keeps ringing, because a gap where
    # neither rings is worse than a moment where both do.
    #
    # Written only when it moves. A client replaying an old cursor — a retry of
    # a request whose response was lost, a device restored from a backup — must
    # not walk this backwards and shut a gate on a device that has since caught
    # up, so the guard sits in the WHERE rather than in the value: the statement
    # matches no row instead of writing a smaller number.
    #
    # What it records is "handed out", not "applied" — see `/sync/preview` and
    # contract §4.3 for why that distinction has an endpoint of its own, and
    # `?ready=1` for the signal that replaces it where it is load-bearing.
    await session.execute(
        sa_update(Device)
        .where(
            Device.id == caller.device_id,
            func.coalesce(Device.cursor_seq, -1) < reached,
        )
        .values(cursor_seq=reached)
    )

    if ready:
        # Recorded here rather than at sign-in, and the app track is right that
        # it has to be: speaking this protocol is a property of the build, and a
        # phone that updates keeps its token and goes on pulling in the
        # background without ever authenticating again. A flag on the token
        # endpoint would miss exactly the devices that matter.
        #
        # Same transaction as the cursor, so there is no page for which the
        # weaker rule applied to a device that deserved the stronger one.
        #
        # Written once and left alone: it is a fact about the build, and
        # refreshing the timestamp every pull would be a write per request that
        # records nothing new.
        await session.execute(
            sa_update(Device)
            .where(Device.id == caller.device_id, Device.ready_protocol_at.is_(None))
            .values(ready_protocol_at=utcnow())
        )

    await session.commit()
    return page


@router.get("/sync/preview", response_model=PullOut)
async def preview(
    request: Request,
    cursor: str | None = Query(default=None),
    caller: Caller = Depends(sync_caller),
    session: AsyncSession = Depends(get_session),
) -> PullOut:
    """The same feed, read without admitting to having read it.

    Same authorisation, same visibility, same projection by role, same paging,
    same body. The single difference is that `Device.cursor_seq` does not move,
    and the difference is the whole endpoint.

    The client needs to show a person what is on the server before deciding
    whether to adopt it — which profile is theirs, how many medications it
    holds — and doing that with `/sync/pull` made the server believe the device
    had been handed rows it has not applied and may never apply. `cursor_seq` is
    not bookkeeping: it is the evidence the authority gate waits for before
    telling the previous phone to stop ringing. Reading ahead with `pull` could
    therefore silence a phone on the strength of a page that was only ever
    looked at.

    So a read that does not commit the reader to anything gets its own route
    rather than a flag on the old one: a caller that does not know about this
    endpoint cannot accidentally get its semantics, and an old client keeps the
    behaviour it was written against.
    """
    page, _reached = await _page(
        session, caller, cursor, request.app.state.settings.authority_lease
    )
    return page
