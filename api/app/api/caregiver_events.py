"""Read-only caregiver projection. No delivery acknowledgement or TTL renewal."""

import re
from datetime import date, datetime, timedelta, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Caller, get_session, lock_caller, sync_caller
from app.core.caregiver_cursor import (
    CursorCodec, CursorExpired, CursorInvalid, CursorUnavailable, FeedReset, Position, TTL_MS,
)
from app.db.models import AlertDelivery, AlertKind, CaregiverRecoveryControl, CaregiverRecoveryEvent, ProfileMembership, utcnow
from app.services.alerts import _eligible_watchers

router = APIRouter()
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def milliseconds(value) -> int:
    elapsed = value - EPOCH
    return elapsed.days * 86400000 + elapsed.seconds * 1000 + elapsed.microseconds // 1000


def query(request: Request) -> tuple[str | None, int]:
    params = request.query_params
    for key in ("cursor", "limit"):
        if len(params.getlist(key)) > 1:
            raise HTTPException(422, "invalid_request")
    cursor = params.get("cursor")
    if cursor is not None and (not cursor or not cursor.isascii() or len(cursor) > 2048):
        raise HTTPException(422, "invalid_request")
    limit = params.get("limit", "50")
    if not re.fullmatch(r"[1-9][0-9]?", limit) or not 1 <= int(limit) <= 50:
        raise HTTPException(422, "invalid_request")
    return cursor, int(limit)


def event_json(recovery: CaregiverRecoveryEvent, delivery: AlertDelivery, as_of) -> dict:
    # A malformed stored producer row must fail the page, not advance its cursor
    # while silently dropping an event. Only stable fields cross the wire.
    lifetime = timedelta(hours=6 if delivery.kind == AlertKind.dose_missed else 24)
    if (delivery.kind not in (AlertKind.dose_missed, AlertKind.profile_stale)
            or recovery.expires_at - recovery.raised_at != lifetime
            or milliseconds(recovery.raised_at) > milliseconds(as_of)):
        raise ValueError("invalid caregiver source row")
    if delivery.kind == AlertKind.dose_missed:
        if str(uuid.UUID(delivery.subject_id)) != delivery.subject_id:
            raise ValueError("invalid caregiver subject")
    elif date.fromisoformat(delivery.subject_id).isoformat() != delivery.subject_id:
        raise ValueError("invalid caregiver subject")
    return {"event_id": str(delivery.id), "event_seq": str(recovery.event_seq),
            "type": delivery.kind.value, "account_id": str(delivery.account_id),
            "profile_id": str(delivery.profile_id), "subject_id": delivery.subject_id,
            "raised_at": milliseconds(recovery.raised_at), "expires_at": milliseconds(recovery.expires_at)}


@router.get("/caregiver-events")
async def read_events(
    request: Request,
    caller: Caller = Depends(sync_caller),
    session: AsyncSession = Depends(get_session),
):
    # Recheck account/device/generation after any publication wait, before
    # revealing cursor facts. Auth and eligibility remain fresh on every page.
    await lock_caller(session, caller)
    cursor, limit = query(request)
    control = (await session.execute(select(CaregiverRecoveryControl)
                                    .execution_options(populate_existing=True))).scalar_one()
    try:
        if not control.producer_enabled or not control.reader_enabled:
            raise CursorUnavailable()
        codec = CursorCodec(request.app.state.settings)
    except CursorUnavailable:
        raise HTTPException(503, "caregiver_events_unavailable", headers={"Retry-After": "60"}) from None
    as_of = utcnow()  # sample after fence wait and auth recheck
    now_ms = milliseconds(as_of)
    if now_ms < milliseconds(control.coverage_started_at):
        raise ValueError("server clock precedes coverage")
    account, epoch = str(caller.account.id), str(control.epoch)
    try:
        previous = codec.decode(cursor, account, epoch, now_ms) if cursor is not None else None
        if previous is not None and previous.h > control.committed_frontier:
            raise FeedReset()
    except CursorInvalid:
        raise HTTPException(400, "caregiver_cursor_invalid") from None
    except CursorExpired:
        raise HTTPException(410, "caregiver_cursor_expired") from None
    except FeedReset:
        raise HTTPException(410, "caregiver_feed_reset") from None
    if previous is None or previous.mode == "checkpoint":
        p = previous.h if previous else 0
        h, issued, expiry = control.committed_frontier, now_ms, now_ms + TTL_MS
    else:
        p, h, issued, expiry = previous.p, previous.h, previous.issued_at, previous.expires_at
    eligible_profiles = (_eligible_watchers().with_only_columns(ProfileMembership.profile_id)
                         .where(ProfileMembership.account_id == caller.account.id))
    rows = (await session.execute(select(CaregiverRecoveryEvent, AlertDelivery)
        .join(AlertDelivery, CaregiverRecoveryEvent.delivery_id == AlertDelivery.id)
        .where(AlertDelivery.account_id == caller.account.id,
               AlertDelivery.profile_id.in_(eligible_profiles),
               CaregiverRecoveryEvent.epoch == control.epoch,
               CaregiverRecoveryEvent.retired_at.is_(None),
               # Wire timestamps are integer milliseconds. A sub-ms remainder
               # must not produce expires_at == as_of on a supposedly valid page.
               CaregiverRecoveryEvent.expires_at >= EPOCH + timedelta(milliseconds=now_ms + 1),
               CaregiverRecoveryEvent.event_seq > p, CaregiverRecoveryEvent.event_seq <= h)
        .order_by(CaregiverRecoveryEvent.event_seq).limit(limit + 1))).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    events = [event_json(recovery, delivery, as_of) for recovery, delivery in rows]
    next_position = rows[-1][0].event_seq if has_more else h
    token = codec.encode(Position(account, epoch, next_position, h,
                                  "continue" if has_more else "checkpoint", issued, expiry))
    result = {"version": 1, "account_id": account, "feed_epoch": epoch,
              "coverage_started_at": milliseconds(control.coverage_started_at), "as_of": now_ms,
              "events": events, "cursor": token, "cursor_expires_at": expiry, "has_more": has_more}
    # No server cursor/ack state; release publication before sending JSON.
    await session.rollback()
    return result
