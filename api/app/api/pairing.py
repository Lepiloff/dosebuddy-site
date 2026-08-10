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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Caller, current_caller, get_session
from app.core.security import PAIRING_CODE_TTL, hash_pairing_code, new_pairing_code
from app.db.models import Device, PairingCode, Profile, ProfileMembership, Role, utcnow

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
                Profile.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        # 404 rather than 403, even when the profile exists but belongs to
        # someone else: a 403 would confirm that a given id is a real profile,
        # which is enough to enumerate other people's.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "profile_not_found")
    return profile


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
    if profile is None or profile.deleted_at is not None:
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
