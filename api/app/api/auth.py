"""Auth: sign in with Google, refresh, log out, delete the account."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Caller, current_caller, get_session
from app.core.security import (
    REFRESH_TOKEN_TTL,
    hash_refresh_token,
    mint_access_token,
    new_refresh_token,
)
from app.db.models import Account, Device, Profile, ProfileMembership, RefreshToken, utcnow
from app.services.google import GoogleIdentity, InvalidGoogleToken

router = APIRouter(tags=["auth"])


class DeviceIn(BaseModel):
    id: uuid.UUID
    platform: str = Field(max_length=32)
    app_version: str | None = Field(default=None, max_length=32)


class GoogleSignIn(BaseModel):
    id_token: str
    device: DeviceIn


class TokenPair(BaseModel):
    access_token: str
    expires_in: int
    refresh_token: str
    account_id: uuid.UUID


class RefreshIn(BaseModel):
    refresh_token: str


async def _issue(session: AsyncSession, settings, account: Account, device: Device) -> TokenPair:
    access, expires_in = mint_access_token(settings.jwt_secret, account.id, device.id)
    raw = new_refresh_token()
    session.add(
        RefreshToken(
            device_id=device.id,
            token_hash=hash_refresh_token(raw),
            expires_at=utcnow() + REFRESH_TOKEN_TTL,
        )
    )
    return TokenPair(
        access_token=access, expires_in=expires_in, refresh_token=raw, account_id=account.id
    )


@router.post("/auth/google", response_model=TokenPair)
async def sign_in_with_google(
    body: GoogleSignIn, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    verifier = request.app.state.google_verifier
    try:
        identity: GoogleIdentity = verifier.verify(body.id_token)
    except InvalidGoogleToken:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_google_token") from None

    account = (
        await session.execute(select(Account).where(Account.google_sub == identity.subject))
    ).scalar_one_or_none()

    if account is None:
        # First sign-in creates the account. There is no separate registration:
        # one fewer screen, and one fewer state to be half-way through.
        account = Account(google_sub=identity.subject, email=identity.email)
        session.add(account)
        await session.flush()
    elif account.deleted_at is not None:
        # Signing in again after deletion starts over rather than resurrecting.
        # Deleted meant deleted.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account_deleted")

    device = await session.get(Device, body.device.id)
    if device is None:
        device = Device(
            id=body.device.id,
            account_id=account.id,
            platform=body.device.platform,
            app_version=body.device.app_version,
        )
        session.add(device)
    elif device.account_id != account.id:
        # The same device id under a different account means the id was copied
        # rather than generated. Refusing is safer than silently reassigning it.
        raise HTTPException(status.HTTP_409_CONFLICT, "device_belongs_to_another_account")
    else:
        device.app_version = body.device.app_version
        device.revoked_at = None

    device.last_seen_at = utcnow()
    await session.flush()

    pair = await _issue(session, request.app.state.settings, account, device)
    await session.commit()
    return pair


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshIn, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    token_hash = hash_refresh_token(body.refresh_token)
    token = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    ).scalar_one_or_none()

    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_refresh_token")

    if token.replaced_by_id is not None:
        # This token was already rotated away, so two copies are in circulation
        # and one of them is not the user's. Revoke every session on the device
        # rather than just this token: the honest client will sign in again,
        # and whoever else has a copy loses it.
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.device_id == token.device_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh_token_reused")

    now = datetime.now(timezone.utc)
    if token.revoked_at is not None or token.expires_at <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_refresh_token")

    device = await session.get(Device, token.device_id)
    if device is None or device.revoked_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "device_revoked")

    account = await session.get(Account, device.account_id)
    if account is None or account.deleted_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_refresh_token")

    pair = await _issue(session, request.app.state.settings, account, device)
    await session.flush()

    successor = (
        await session.execute(
            select(RefreshToken)
            .where(RefreshToken.device_id == device.id)
            .order_by(RefreshToken.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    token.replaced_by_id = successor.id
    token.revoked_at = utcnow()

    device.last_seen_at = utcnow()
    await session.commit()
    return pair


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    caller: Caller = Depends(current_caller), session: AsyncSession = Depends(get_session)
) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.device_id == caller.device_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await session.commit()
    # The access token lives out its remaining minutes. Checking a revocation
    # list on every request would cost a query per call to save a few minutes on
    # a deliberate sign-out; unlinking a device, which is the case that matters,
    # is checked per request in deps.current_caller.


@router.delete("/account", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    caller: Caller = Depends(current_caller), session: AsyncSession = Depends(get_session)
) -> None:
    """Deletes now, with no grace period.

    A Play obligation that arrived with accounts. Profiles the account only
    watched are left alone — they are not its data, and removing them would
    delete someone else's history on their behalf. Its membership of them goes.
    """
    account_id = caller.account.id

    await session.execute(
        update(ProfileMembership)
        .where(ProfileMembership.account_id == account_id, ProfileMembership.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    # Profiles it owns go with it, and their memberships cascade — anyone
    # watching loses access because the data no longer exists.
    await session.execute(delete(Profile).where(Profile.owner_account_id == account_id))
    await session.execute(delete(Account).where(Account.id == account_id))
    await session.commit()
