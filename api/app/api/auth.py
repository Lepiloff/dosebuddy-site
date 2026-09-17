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


class DeleteRetry(BaseModel):
    """What a client has left when the answer to `DELETE /account` was lost.

    Not a session: the device may hold nothing usable by then — that is the
    situation this exists for — so the Google token is the credential and the
    account id says which row the caller means.
    """

    account_id: uuid.UUID
    id_token: str


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


MAX_DELETE_RETRIES_PER_HOUR = 10


async def _erase_account(session: AsyncSession, account_id: uuid.UUID) -> None:
    """The erasure itself, in one place so the two doors cannot drift apart.

    Profiles it owns go with it, and their memberships cascade — anyone
    watching loses access because the data no longer exists.
    """
    await session.execute(delete(Profile).where(Profile.owner_account_id == account_id))
    await session.execute(delete(Account).where(Account.id == account_id))
    await session.commit()


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
    await _erase_account(session, caller.account.id)


@router.post("/account/delete-retry", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account_retry(
    body: DeleteRetry,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Finish a deletion whose answer was lost, on the account that was meant.

    `DELETE /account` is idempotent in the only sense that mattered when it was
    written: send it twice and the row is gone once. It is not idempotent in the
    sense the client needs, because the second attempt has to be made by a
    *session*, and by then there may not be one — the account is gone, so the
    refresh fails, so the phone signs in again, and `/auth/google` creates a new
    account. The retry then deleted a row that had existed for ten seconds while
    the row it was aimed at stayed. The client now compares ids before every
    DELETE and stops when they differ, which is correct and leaves it stuck:
    correct about not deleting the wrong thing, stuck because nothing can
    delete the right one.

    So this takes the id explicitly and proves the caller by the Google token
    rather than by a session. It never calls `/auth/google` and never creates an
    account — creating one here would reintroduce exactly the row that caused
    the confusion.

    **Three answers, and each says only what the caller is entitled to know.**
    Gone already is 204: the operation the caller is retrying has succeeded,
    whoever completed it. Theirs is deleted and 204. Somebody else's is 403 and
    untouched.

    That third answer does tell a caller holding a live Google identity that
    some uuid is an account and is not theirs, which is more than
    `/pairing/redeem` will say about a code. The difference is that a pairing
    code is six characters and this is a v4 uuid: there is no enumeration to
    protect against, only a question about an id the caller already has. The
    alternative — 204 for a stranger's account — would end the client's retry
    loop with "deleted" while the account stood, which is the one answer here
    that could be acted on wrongly. Attempts are counted per Google subject all
    the same, because a rate that only matters under a leak still matters.
    """
    verifier = request.app.state.google_verifier
    if verifier is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "google_sign_in_not_configured"
        )

    try:
        identity: GoogleIdentity = verifier.verify(body.id_token)
    except InvalidGoogleToken as exc:
        log.warning("account.delete_retry_rejected", reason=redact(str(exc)))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_google_token") from None

    # Counted after the token is proven and keyed by the subject it proves:
    # before that there is no caller to attribute attempts to, and an unproven
    # one could spend somebody else's budget by naming their id.
    attempts_key = f"account:delete_retry:{identity.subject}"
    redis = request.app.state.redis
    attempts = await redis.incr(attempts_key)
    if attempts == 1:
        await redis.expire(attempts_key, 3600)
    if attempts > MAX_DELETE_RETRIES_PER_HOUR:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too_many_attempts")

    account = await session.get(Account, body.account_id)
    if account is None:
        # Already gone, by this path or the other. The caller asked for an
        # outcome, not for an action.
        log.info("account.delete_retry", account_id=str(body.account_id), outcome="absent")
        return

    if account.google_sub != identity.subject:
        log.warning(
            "account.delete_retry", account_id=str(body.account_id), outcome="not_yours"
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not_your_account")

    await _erase_account(session, account.id)
    log.info("account.delete_retry", account_id=str(body.account_id), outcome="erased")
