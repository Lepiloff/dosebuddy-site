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
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Caller, current_caller, get_session
from app.core.security import (
    PAIRING_CODE_TTL,
    hash_pairing_code,
    mint_sync_token,
    new_pairing_code,
)
from app.db.models import (
    Device,
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
        # Re-pairing with a different role is a legitimate way to change it. No
        # resend: this account could already see the profile, so its devices
        # have the rows. Every non-owner role sees the same fields — the role
        # decides who gets alerted, not what crosses the wire.
        existing.role = code.role
    else:
        session.add(
            ProfileMembership(
                profile_id=profile.id, account_id=caller.account.id, role=code.role
            )
        )
        # Visibility just widened, so the feed has to carry the profile again.
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


@router.post(
    "/profiles/{profile_id}/reminder-authority", status_code=status.HTTP_204_NO_CONTENT
)
async def set_reminder_authority(
    profile_id: uuid.UUID,
    body: ReminderAuthorityIn,
    request: Request,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> None:
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
    if previous == device.id:
        return

    # A new server_seq, so the change reaches every device through the ordinary
    # cursor rather than needing a delivery mechanism of its own.
    await session.execute(
        sa_update(Profile)
        .where(Profile.id == profile.id)
        .values(owner_device_id=device.id, server_seq=text("nextval('server_seq')"))
    )
    await session.commit()

    if previous is not None:
        old = await session.get(Device, previous)
        if old is not None and old.push_token and old.revoked_at is None:
            # Not retried and not recorded, unlike an alert. This only nudges a
            # device to stop ringing sooner than its next sync would tell it, so
            # losing the nudge costs one duplicate reminder, not a missed dose.
            await request.app.state.push.send(
                old.push_token,
                {"type": "reminder_authority_lost", "profile_id": str(profile.id)},
                f"reminder_authority_lost:{profile.id}",
                # An hour. The next sync tells the device the same thing from
                # the data, so a nudge that arrives a day late is noise.
                3600,
            )
