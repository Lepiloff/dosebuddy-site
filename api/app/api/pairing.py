"""Pairing, membership, and the devices endpoints that hang off them.

The profile owner issues the code, so the person whose health data is being
shared performs the act of sharing. That is the cleanest consent story under
GDPR, and it is why the issuing row is kept: it is the record that consent was
given, by whom, for what, and when.

Profiles themselves arrive over sync, which is not built yet. These endpoints
are complete and tested against profiles created directly; in production they
become usable once sync lands.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Caller, current_caller, get_session, sync_caller
from app.api.sync import decode_cursor, encode_cursor
from app.services import alerts
from app.core.security import (
    PAIRING_CODE_TTL,
    hash_pairing_code,
    mint_sync_token,
    new_pairing_code,
)
from app.db.models import (
    Device,
    DeviceReadiness,
    DoseEvent,
    Medication,
    PairingCode,
    Profile,
    ProfileMembership,
    Role,
    utcnow,
)

router = APIRouter(tags=["pairing"])

# A six-character code is ~10^9 possibilities, which is plenty against online
# guessing but nothing against an unthrottled loop.
MAX_REDEEM_ATTEMPTS_PER_HOUR = 10


class IssueCodeIn(BaseModel):
    profile_id: uuid.UUID
    role: Role = Field(description="viewer or with_alerts; owner cannot be granted")


class IssuedCode(BaseModel):
    code: str
    expires_at: datetime


class RedeemIn(BaseModel):
    code: str = Field(max_length=16)


class MemberOut(BaseModel):
    account_id: uuid.UUID
    role: Role
    created_at: datetime


class ProfileOut(BaseModel):
    id: uuid.UUID
    name: str
    role: Role


async def _owned_profile(session: AsyncSession, caller: Caller, profile_id: uuid.UUID) -> Profile:
    profile = (
        await session.execute(
            select(Profile).where(
                Profile.id == profile_id,
                Profile.owner_account_id == caller.account.id,
                Profile.deleted_at_ms.is_(None),
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        # 404 rather than 403, even when the profile exists but belongs to
        # someone else: a 403 would confirm that a given id is a real profile,
        # which is enough to enumerate other people's.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "profile_not_found")
    return profile


async def _resend_profile(session: AsyncSession, profile_id: uuid.UUID) -> None:
    """Put a profile's rows back at the head of the change feed.

    Pull hands out changes newer than the caller's cursor, and the cursor is one
    number covering every profile at once. So granting access to a profile whose
    rows are all older than the new watcher's cursor delivered **nothing** — not
    late, never. The membership was live, `roles` named the profile, and
    `changes` came back empty, which from the device is indistinguishable from
    "no changes". A caregiver added to someone who had been using the app for a
    while — the ordinary way this happens — got an empty profile for ever.

    Raising the sequence makes the grant look like what it is: rows this account
    has not seen. Devices already watching the profile receive them again, which
    costs them nothing — `updated_at` is untouched, so the upsert is a no-op.

    Only the three entities a watcher can actually receive (see
    `sync.WATCHER_FIELDS`). Schedules and stock events reach the owner alone, and
    the owner has them already; bumping those would re-send rows nobody is
    missing and make the owner's device recompute its alarms for nothing.

    `nextval` per row rather than one value for all of them: the cursor after a
    page is the largest sequence in it, so rows sharing a number that straddles
    the page boundary would be skipped and never offered again.
    """
    for model, key in (
        (Profile, Profile.id),
        (Medication, Medication.profile_id),
        (DoseEvent, DoseEvent.profile_id),
    ):
        await session.execute(
            sa_update(model)
            .where(key == profile_id)
            .values(server_seq=text("nextval('server_seq')"))
        )


@router.post("/pairing/codes", response_model=IssuedCode)
async def issue_code(
    body: IssueCodeIn,
    request: Request,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> IssuedCode:
    if body.role is Role.owner:
        # Ownership is not something to hand out over a code. Moving the
        # reminder authority is a separate, deliberate transfer (spec §1.4).
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "role_not_grantable")

    profile = await _owned_profile(session, caller, body.profile_id)

    code = new_pairing_code()
    expires_at = utcnow() + PAIRING_CODE_TTL
    session.add(
        PairingCode(
            profile_id=profile.id,
            issued_by_account_id=caller.account.id,
            role=body.role,
            code_hash=hash_pairing_code(request.app.state.settings.jwt_secret, code),
            expires_at=expires_at,
        )
    )
    await session.commit()

    # The plaintext code is returned once and never stored. If it is lost, a new
    # one is issued; there is nothing to recover.
    return IssuedCode(code=code, expires_at=expires_at)


@router.post("/pairing/redeem", response_model=ProfileOut)
async def redeem(
    body: RedeemIn,
    request: Request,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> ProfileOut:
    # Throttle attempts, not successes. Six characters is plenty against
    # guessing by hand and nothing against a loop, and the counter has to move
    # on failures or it protects nothing.
    attempts_key = f"pairing:attempts:{caller.account.id}"
    redis = request.app.state.redis
    attempts = await redis.incr(attempts_key)
    if attempts == 1:
        await redis.expire(attempts_key, 3600)
    if attempts > MAX_REDEEM_ATTEMPTS_PER_HOUR:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too_many_attempts")

    settings = request.app.state.settings
    code_hash = hash_pairing_code(settings.jwt_secret, body.code)

    code = (
        await session.execute(select(PairingCode).where(PairingCode.code_hash == code_hash))
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if code is None or code.redeemed_at is not None or code.expires_at <= now:
        # One answer for wrong, used and expired. Telling them apart turns the
        # endpoint into an oracle for which codes exist.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_code")

    profile = await session.get(Profile, code.profile_id)
    if profile is None or profile.deleted_at_ms is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_code")

    if profile.owner_account_id == caller.account.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "already_owner")

    existing = (
        await session.execute(
            select(ProfileMembership).where(
                ProfileMembership.profile_id == profile.id,
                ProfileMembership.account_id == caller.account.id,
                ProfileMembership.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Re-pairing with a different role is a legitimate way to change it.
        existing.role = code.role
    else:
        session.add(
            ProfileMembership(
                profile_id=profile.id, account_id=caller.account.id, role=code.role
            )
        )

    # Both paths, not only the new membership. "This account may see the profile"
    # and "this account's devices have the rows" are separate facts, and them
    # diverging is the whole bug — which leaves an account that is already a live
    # member with a device that will never be sent anything, and nothing about
    # that membership is going to change again on its own. Redeeming again is
    # then the only lever a person has, so it has to work.
    #
    # Rare, deliberate and idempotent, so doing it on every redeem costs nothing
    # against being unable to repair a phone showing an empty profile.
    await _resend_profile(session, profile.id)

    code.redeemed_at = now
    code.redeemed_by_account_id = caller.account.id
    await session.commit()

    return ProfileOut(id=profile.id, name=profile.name, role=code.role)


@router.get("/profiles/{profile_id}/members", response_model=list[MemberOut])
async def list_members(
    profile_id: uuid.UUID,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> list[MemberOut]:
    profile = await _owned_profile(session, caller, profile_id)
    rows = (
        await session.execute(
            select(ProfileMembership).where(
                ProfileMembership.profile_id == profile.id,
                ProfileMembership.revoked_at.is_(None),
            )
        )
    ).scalars()
    return [
        MemberOut(account_id=m.account_id, role=m.role, created_at=m.created_at) for m in rows
    ]


@router.delete(
    "/profiles/{profile_id}/members/{account_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_member(
    profile_id: uuid.UUID,
    account_id: uuid.UUID,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> None:
    profile = await _owned_profile(session, caller, profile_id)
    await session.execute(
        update(ProfileMembership)
        .where(
            ProfileMembership.profile_id == profile.id,
            ProfileMembership.account_id == account_id,
            ProfileMembership.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    await session.commit()
    # The row is revoked, not deleted: who could see what, and when, is a
    # question that has to be answerable later.


class PushTokenIn(BaseModel):
    fcm_token: str = Field(max_length=512)


@router.put("/devices/{device_id}/push-token", status_code=status.HTTP_204_NO_CONTENT)
async def set_push_token(
    device_id: uuid.UUID,
    body: PushTokenIn,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> None:
    device = await session.get(Device, device_id)
    if device is None or device.account_id != caller.account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device_not_found")
    device.push_token = body.fcm_token
    device.last_seen_at = utcnow()
    await session.commit()


class SyncToken(BaseModel):
    sync_token: str
    expires_in: int


@router.post("/devices/{device_id}/sync-token", response_model=SyncToken)
async def issue_sync_token(
    device_id: uuid.UUID,
    request: Request,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> SyncToken:
    """A credential the device's background worker may hold and reuse.

    Issued from the foreground, where an ordinary access token is available, and
    then kept in the Keystore beside the refresh token. It grants sync and
    nothing else, and it does not rotate — which is the point, because rotation
    is what stopped the background half of the app from using the network at all
    (see core.security.SYNC_TOKEN_TTL).

    Deliberately reissuable rather than one-per-device: the app asks whenever it
    is open and near expiry, and the old one keeps working until it expires. A
    device that has not been opened for months is the case this whole mechanism
    exists for, so a token that could be invalidated by asking for another would
    reintroduce the gap it closes.
    """
    device = await session.get(Device, device_id)
    if device is None or device.account_id != caller.account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device_not_found")

    token, expires_in = mint_sync_token(
        request.app.state.settings.jwt_secret, caller.account.id, device.id
    )
    return SyncToken(sync_token=token, expires_in=expires_in)


@router.post("/devices/{device_id}/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
async def heartbeat(
    device_id: uuid.UUID,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> None:
    """What makes `profile_stale` possible.

    Without a heartbeat, silence from a device is indistinguishable from having
    nothing to report — and those mean opposite things to whoever is watching.
    """
    device = await session.get(Device, device_id)
    if device is None or device.account_id != caller.account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device_not_found")
    device.last_seen_at = utcnow()
    await session.commit()


class ReminderAuthorityIn(BaseModel):
    device_id: uuid.UUID

    # Declared by the claim itself, not learned from a later pull. Sign-in
    # claims before the first ordinary `pull`, so `devices.ready_protocol_at`
    # is still empty exactly when the first handover happens — the moment the
    # strictness matters most. The app track found that; the flag is the answer.
    #
    # It turns on two things together, and they belong together: the handover
    # waits for a readiness report instead of moving at once, and *this*
    # handover is leased, meaning the server may hand it back if readiness
    # stops being renewed.
    reports_ready: bool = False


class AuthorityState(BaseModel):
    """Who holds reminders for a profile, who is waiting to, and since when.

    Sent for every owned profile on every `pull`, empty pages included, because
    it is state rather than an event: readiness changes without any row
    changing — a report lands, a lease runs out — and a device whose cursor is
    past the profile row would never hear about it from an incremental feed.
    """

    owner_device_id: uuid.UUID | None
    pending_device_id: uuid.UUID | None
    previous_device_id: uuid.UUID | None
    revision: str
    owner_ready: bool
    # When the owner's report stops counting, for handovers under the lease.
    # A reason to go and ask the server, never a licence to take the alarms
    # back: the owner may have renewed while this copy was in flight, and the
    # handback is the server's to make and to confirm with a new revision.
    ready_until: datetime | None
    # Orders responses. A block that arrives late, with an older `as_of` than
    # one already applied, is describing a world that has moved on.
    as_of: datetime


class ReminderAuthorityOut(BaseModel):
    """The receipt, which this endpoint used to compute and then throw away.

    It answered 204. The number was right there — `RETURNING server_seq` on the
    handover — and it went only into the nudge for the losing device, never to
    the caller that had just won. The caller therefore recorded no revision, and
    the fence it keeps against late nudges stayed open at whatever number it
    last saw.

    That is not the safe direction, which is what the client comment claimed.
    Take a device A holding revision 5, while the server has since moved
    authority A -> B (6) -> A (7) and the nudge for 6 is still queued at FCM. A
    claims, is told nothing, stays at 5. The nudge for 6 then arrives, clears
    "strictly newer than 5", and A disarms its alarms and records B as the owner
    — while the server has A as the owner and sends nothing more. If B is the
    phone that was replaced, nobody rings. Silence, not a tolerable duplicate.

    **`revision` is a JSON number here and a string on pull, and that is not an
    oversight.** The published client casts this one with `as num?`, which
    throws on a string, and parses pull's with a switch that accepts either;
    pull's must be a string because it also travels through FCM's
    `map<string,string>`. Making the two agree would break the build in the
    store, and that build cannot be changed.
    """

    owner_device_id: uuid.UUID
    revision: int

    # Set when the claim was made under `reports_ready` and the handover is
    # therefore waiting: `owner_device_id` above is still the phone that holds
    # the alarms, and this is the one that asked for them. The claimant reads
    # itself here, arms, checks, and only then reports — and if the response is
    # lost it finds the same thing in the `authority` block of its next pull.
    pending_device_id: uuid.UUID | None = None


@router.post(
    "/profiles/{profile_id}/reminder-authority", response_model=ReminderAuthorityOut
)
async def set_reminder_authority(
    profile_id: uuid.UUID,
    body: ReminderAuthorityIn,
    request: Request,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> ReminderAuthorityOut:
    """Move the reminder authority for a profile to another device.

    Exactly one device materialises alarms for a profile (spec §1.4), so this is
    a deliberate handover — a phone being replaced, or a profile moving between
    the phones of one account.

    **Deliberately not part of sync push.** Authority set through the ordinary
    change stream would be resolved by last-write-wins, and two devices that both
    believe they hold it is precisely the state the invariant exists to prevent.
    One endpoint, one writer, one answer.

    The previous device is nudged by push, but correctness does not rest on that
    push arriving: the authoritative signal is `owner_device_id` on the profile,
    which every device sees on its next pull. A device that finds an id other
    than its own stops arming alarms. Push only makes it happen sooner — and
    since it can be lost, anything that depended on it would eventually leave two
    phones ringing for one dose.
    """
    profile = await _owned_profile(session, caller, profile_id)

    device = await session.get(Device, body.device_id)
    if device is None or device.account_id != caller.account.id or device.revoked_at:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device_not_found")

    previous = profile.owner_device_id

    if body.reports_ready and previous is not None and previous != device.id:
        # Two-phase, and only here. Authority stays where the alarms are until
        # the claimant says it can ring, which is what lets a published client
        # in the losing role keep its alarms: it never sees a foreign owner id,
        # because there is not one yet.
        #
        # There is a brief overlap by construction — for the report to be true
        # the claimant must already have armed, and the old phone is still the
        # owner while it does. That is the acceptable side of invariant 1, and
        # reporting before arming would buy it back with the unacceptable one.
        #
        # Only when somebody already holds it: a first claim has nothing to
        # protect and nothing to wait for.
        await session.execute(
            sa_update(Profile)
            .where(Profile.id == profile.id)
            .values(pending_owner_device_id=device.id, authority_leased=True)
        )
        await session.commit()
        return ReminderAuthorityOut(
            owner_device_id=previous,
            revision=profile.server_seq,
            pending_device_id=device.id,
        )

    if previous == device.id:
        # Nothing to write, but everything still to say. This branch is exactly
        # the case the fence exists for: the caller is claiming what the server
        # already believes it holds, which is what a device that missed a
        # handover looks like. Answering it with the current `server_seq` is
        # what closes the gap — the device that knew least now knows the number.
        return ReminderAuthorityOut(
            owner_device_id=device.id, revision=profile.server_seq
        )

    # A new server_seq, so the change reaches every device through the ordinary
    # cursor rather than needing a delivery mechanism of its own.
    #
    # RETURNING, not a read of `profile.server_seq` afterwards. The statement is
    # the only thing that knows the number `nextval` produced: the loaded
    # instance predates it, and a Core UPDATE expires the attribute rather than
    # refreshing it, so reading it here raises MissingGreenlet — verified by
    # mutation, not assumed. Taking the value from the statement that generated
    # it needs no round trip and no reasoning about session state.
    revision = (
        await session.execute(
            sa_update(Profile)
            .where(Profile.id == profile.id)
            .values(
                owner_device_id=device.id,
                previous_owner_device_id=previous,
                pending_owner_device_id=None,
                # A handover claimed without the flag is not leased, and one
                # claimed with it has already returned above. Written here so a
                # profile that changes hands between protocols cannot keep a
                # lease the new claimant never asked for.
                authority_leased=False,
                server_seq=text("nextval('server_seq')"),
            )
            .returning(Profile.server_seq)
        )
    ).scalar_one()
    await session.commit()

    if previous is not None:
        old = await session.get(Device, previous)
        if old is not None and old.revoked_at is None:
            # Queued for the worker, not sent from here, and that is the whole
            # of the fix. This process has no FCM credentials — the compose file
            # gives them to the worker alone, deliberately, so the key stays out
            # of the process facing the internet — which meant every nudge sent
            # from here went to a log file instead of a phone, silently, from
            # the first day. `push.not_configured`, observed live 2026-08-15.
            #
            # The queue also brings what a bare send never had: retries, a
            # collapse key, a TTL, and a row afterwards recording whether it
            # went. That last one is the question acceptance asked and the
            # server could not answer.
            #
            # No push token is required to queue it. The worker resolves the
            # token when it sends, so a device that registers one within the
            # hour is still told — where the old code simply skipped it.
            await alerts.claim(
                session,
                alerts.authority_lost(
                    account_id=caller.account.id,
                    profile_id=profile.id,
                    device_id=old.id,
                    revision=revision,
                ),
                utcnow(),
            )
            await session.commit()

    return ReminderAuthorityOut(owner_device_id=device.id, revision=revision)


class ReadyIn(BaseModel):
    """What a device asserts about one profile, and what the server can check.

    `revision` is the profile's `server_seq` as the device saw it — the same
    number the claim response and the nudge carry. `applied_cursor` is how far
    it had applied when it said so, because the revision alone proves nothing:
    the client learns it from the claim response *before* loading a single row,
    so a report carrying only that could come from a phone holding none of the
    data.

    Both are strings on the wire for one reason: `revision` already crosses as a
    string in the FCM payload, where map<string,string> is FCM's type rather
    than anyone's choice, and a number that is a string in one channel and an
    integer in another is a parse bug waiting for a quiet afternoon.
    """

    profile_id: uuid.UUID
    revision: str
    applied_cursor: str


@router.post("/devices/{device_id}/ready", status_code=status.HTTP_204_NO_CONTENT)
async def report_ready(
    device_id: uuid.UUID,
    body: ReadyIn,
    caller: Caller = Depends(sync_caller),
    session: AsyncSession = Depends(get_session),
) -> None:
    """The device says it can actually ring for this profile now.

    Everything else the gate has ever had was inferred. `cursor_seq` says a page
    was produced; an acknowledged cursor would say bytes reached a database.
    Neither says an alarm exists, and the app track measured the distance: rows
    inside an applied page can sit quarantined or waiting for a parent, and the
    next page is requested before alarms are rebuilt.

    So the assertion comes from the only party that can make it, and it means
    what they defined on 2026-09-17: the rows for this profile are applied,
    materialisation and alarm reconciliation have run, nothing is left waiting
    or quarantined for it, and the alarm outbox holds no unfinished placement.

    The server cannot verify any of that. It verifies the three things it can,
    and refuses with `409` rather than storing a claim it knows is stale:

    * the report comes from the device that *currently* owns the profile —
      authority may have moved on while this was in flight;
    * the revision is the one the profile is at — a profile written again since
      the handover is a profile this device has not seen whole;
    * the cursor it applied through reaches that revision, which is what makes
      the claim about data rather than about a number it was told.

    Idempotent: saying it twice stores it once, and a device that repeats itself
    after a restart is doing the right thing.

    **Reachable by the sync token, and it has to be.** The background worker
    holds that credential and nothing else, and it is the half of the app that
    pulls while nobody is looking. Written against an access token — as this was
    at first — a background pull could turn the strict gate *on* by sending
    `?ready=1` and then never be able to report readiness, leaving the previous
    phone ringing until somebody opened the app. The app track found that before
    a phone did.

    It does not widen what a leaked sync token can do, which is the test the
    scope is meant to pass: that token already opens this gate the loose way, by
    pulling and advancing `cursor_seq`. Reporting readiness narrows the
    conditions under which the gate opens rather than adding a power — and the
    three checks below still hold, so it can only speak for its own device, for
    a profile that device owns, at the revision the profile is at.
    """
    if device_id != caller.device_id:
        # A device may speak for itself. Reporting readiness for another one
        # would let a phone open the gate that silences its own sibling.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not_your_device")

    profile = await session.get(Profile, body.profile_id)
    if profile is None or profile.deleted_at_ms is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no_such_profile")

    try:
        revision = int(body.revision)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unreadable_position") from None

    # The opaque cursor the client holds, read strictly. `decode_cursor` is
    # deliberately forgiving where `pull` uses it — a client that cannot sync is
    # worse than one that syncs too much — but forgiveness here would turn an
    # unreadable position into "applied nothing", and then into a refusal that
    # blames the phone for a string this endpoint mangled.
    applied_cursor = decode_cursor(body.applied_cursor)
    if applied_cursor == 0 and body.applied_cursor != encode_cursor(0):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unreadable_position")

    claiming = profile.pending_owner_device_id == device_id

    if not claiming and profile.owner_device_id != device_id:
        if profile.pending_owner_device_id is not None:
            # Somebody else claimed after this device did. Retrying will not
            # help and neither will arming: the claim this report belongs to no
            # longer exists.
            raise HTTPException(status.HTTP_409_CONFLICT, "claim_superseded")
        # Not a failure of this device: authority moved, and the answer is to
        # pull and find out rather than to retry this.
        raise HTTPException(status.HTTP_409_CONFLICT, "not_owner")

    if revision != profile.server_seq:
        raise HTTPException(status.HTTP_409_CONFLICT, "stale_revision")

    if applied_cursor < profile.server_seq:
        # Ready for a revision it has not been handed. Whatever the phone
        # believes, it cannot have the rows this one is about.
        raise HTTPException(status.HTTP_409_CONFLICT, "cursor_behind_revision")

    if claiming:
        # The report is what completes the handover. Until this line the alarms
        # were the previous phone's, which is the point of waiting: the claimant
        # has now armed and checked, so moving authority costs no silence.
        #
        # A fresh revision, because this is the handover: every device learns of
        # it by the ordinary cursor, and the previous phone is nudged as before.
        moved = (
            await session.execute(
                sa_update(Profile)
                .where(Profile.id == profile.id)
                .values(
                    owner_device_id=device_id,
                    previous_owner_device_id=profile.owner_device_id,
                    pending_owner_device_id=None,
                    server_seq=text("nextval('server_seq')"),
                )
                .returning(Profile.server_seq)
            )
        ).scalar_one()

        # The report named the revision it was ready for, and the handover has
        # just given the profile a new one. Storing the old number would leave
        # the gate holding against a device that is ready by every measure it
        # was asked about.
        #
        # Claiming to have applied through the new mark is not a fiction worth
        # worrying about: the only thing in that profile row this device has not
        # pulled is the ownership change it has just caused itself.
        revision = moved
        applied_cursor = max(applied_cursor, moved)

        losing = profile.owner_device_id
        if losing is not None:
            old_device = await session.get(Device, losing)
            if old_device is not None and old_device.revoked_at is None:
                await alerts.claim(
                    session,
                    alerts.authority_lost(
                        account_id=caller.account.id,
                        profile_id=profile.id,
                        device_id=old_device.id,
                        revision=moved,
                    ),
                    utcnow(),
                )

    await session.execute(
        insert(DeviceReadiness)
        .values(
            device_id=device_id,
            profile_id=profile.id,
            revision=revision,
            applied_cursor=applied_cursor,
            updated_at=utcnow(),
        )
        .on_conflict_do_update(
            index_elements=[DeviceReadiness.device_id, DeviceReadiness.profile_id],
            set_={
                "revision": revision,
                "applied_cursor": applied_cursor,
                "updated_at": utcnow(),
            },
        )
    )
    await session.commit()
