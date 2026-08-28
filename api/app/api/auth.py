"""Auth: sign in with Google, refresh, log out, delete the account."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
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
from app.db.models import Account, Device, Profile, RefreshToken, utcnow
from app.core.observability import redact
from app.services.google import GoogleIdentity, InvalidGoogleToken

router = APIRouter(tags=["auth"])
log = structlog.get_logger(__name__)


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
    if verifier is None:
        # GOOGLE_CLIENT_ID is unset, so there is no audience to verify against
        # and accepting anything would take a token Google issued for any other
        # application. Say so plainly rather than failing with a 500.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "google_sign_in_not_configured"
        )

    try:
        identity: GoogleIdentity = verifier.verify(body.id_token)
    except InvalidGoogleToken as exc:
        # The caller still learns only that the token was rejected: telling them
        # whether it expired or had the wrong audience tells an attacker the
        # same. But the reason is recorded here, along with the audience we
        # expect, because "wrong audience" and "expired" look identical from
        # outside and are a day apart to debug.
        log.warning(
            "auth.google_rejected",
            reason=redact(str(exc)),
            expected_audience=request.app.state.settings.google_client_id or "(unset)",
        )
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
    """Signing out has to reach every credential the device holds.

    It used to revoke refresh tokens and stop there, which left two others
    working:

    - the **push token**, so a signed-out phone went on receiving alerts about
      someone's doses. Article 9 data, arriving at a device whose user has
      deliberately left the account.
    - the **sync token**, which lives ninety days and does not rotate. Wiping the
      app's copy is not revocation; a copy taken beforehand kept full read access
      to the account for the rest of its life.

    The second one had no revocation path at all. `deps` refuses a device whose
    `revoked_at` is set — a check `security.SYNC_TOKEN_TTL` describes as the
    whole reason a long-lived token is safe to hand out — and **nothing in the
    codebase ever set it**. The mechanism existed; nothing pulled the lever.

    Setting it here is what makes that comment true. Signing in again clears it
    (see `sign_in_with_google`), so this locks nobody out of their own account.
    """
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.device_id == caller.device_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )

    device = await session.get(Device, caller.device_id)
    if device is not None:
        # Cleared as well as revoked. Revoking alone would stop the alerts,
        # since the alert query skips revoked devices, but it would leave a
        # token in the row that FCM has already forgotten — and the row is what
        # someone reads when asking why a phone is silent.
        device.push_token = None
        device.revoked_at = utcnow()

    await session.commit()
    # The access token lives out its remaining minutes. Checking a revocation
    # list on every request would cost a query per call to save a few minutes on
    # a deliberate sign-out — and the device check above already closes the
    # window for the credentials that outlive it.


@router.delete("/account", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    caller: Caller = Depends(current_caller), session: AsyncSession = Depends(get_session)
) -> None:
    """Deletes now, with no grace period.

    A Play obligation that arrived with accounts. Profiles the account only
    watched are left alone — they are not its data, and removing them would
    delete someone else's history on their behalf. Its membership of them goes
    with the account row, because `profile_memberships.account_id` cascades.

    There used to be an UPDATE above that DELETE, marking those memberships
    revoked first, on the reasoning that a record of who could see what is
    worth keeping. It could not survive the statement below it: the cascade
    removed the very rows it had just written. So the code described a
    retention that never happened. Erasure is the stronger obligation here
    anyway — what went is what should have gone, and only the account-deletion
    half of the rule in ProfileMembership was wrong.
    """
    account_id = caller.account.id

    # Profiles it owns go with it, and their memberships cascade — anyone
    # watching loses access because the data no longer exists.
    await session.execute(delete(Profile).where(Profile.owner_account_id == account_id))
    await session.execute(delete(Account).where(Account.id == account_id))
    await session.commit()
