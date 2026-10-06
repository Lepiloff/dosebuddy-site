"""Shared request dependencies."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CREDENTIAL_GENERATION_CLAIM, SCOPE_SYNC, read_access_token
from app.db.publication import lock_publication
from app.db.models import Account, Device

bearer = HTTPBearer(auto_error=False)

# How precisely "last seen" is worth recording. The only reader compares it
# against a threshold in the tens of hours.
SEEN_RESOLUTION = timedelta(minutes=5)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = request.app.state.sessionmaker
    async with factory() as session:
        yield session


class Caller:
    """Who is asking, from which device, and how far their token reaches."""

    def __init__(
        self, account: Account, device_id: uuid.UUID, scope: str | None = None,
        credential_generation: int = 0,
    ):
        self.account = account
        self.device_id = device_id
        self.credential_generation = credential_generation
        # None for an ordinary access token: everything. A string restricts the
        # caller to exactly that and nothing else.
        self.scope = scope


async def recheck_caller(session: AsyncSession, caller: Caller) -> Device:
    """Check the original bearer again while publication is already held.

    A handler can have authenticated before waiting for logout -> sign-in. SQL
    snapshots and ORM objects must both be refreshed, and the claim must still
    match; an unrevoked device alone no longer proves this session is valid.
    """
    account = await session.get(Account, caller.account.id, populate_existing=True)
    if account is None or account.deleted_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_token")
    device = await session.get(Device, caller.device_id, populate_existing=True)
    if (device is None or device.account_id != account.id or device.revoked_at is not None
            or device.credential_generation != caller.credential_generation):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "device_revoked")
    return device


async def lock_caller(session: AsyncSession, caller: Caller) -> Device:
    await lock_publication(session)
    return await recheck_caller(session, caller)


async def _authenticated(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    session: AsyncSession,
    allowed_scopes: frozenset[str | None],
) -> Caller:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing_token")

    try:
        claims = read_access_token(request.app.state.settings.jwt_secret, credentials.credentials)
    except jwt.PyJWTError:
        # One answer for expired, forged and malformed alike. Distinguishing
        # them tells an attacker which part of the guess was right.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_token") from None

    scope = claims.get("scope")
    if scope not in allowed_scopes:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "token_scope_insufficient")

    # Check and last-seen mutation share the revocation fence. Release it before
    # the handler: some handlers do Google/Redis work before their DB operation.
    await lock_publication(session)
    account = await session.get(Account, uuid.UUID(claims["sub"]), populate_existing=True)
    if account is None or account.deleted_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_token")
    caller = Caller(account, uuid.UUID(claims["did"]), scope,
                    claims.get(CREDENTIAL_GENERATION_CLAIM, 0))
    device = await recheck_caller(session, caller)

    # `profile_stale` reads this column to decide whether a phone has gone quiet,
    # so it has to mean "last talked to us" — not "last refreshed a token", which
    # is what it meant when only auth and pairing touched it. A device can sync
    # all day on one access token, and did: staleness was measured against an
    # event the app has no reason to produce.
    #
    # Throttled, because at the resolution that matters — hours — a write per
    # request would be a row lock and a WAL record to sharpen nothing.
    now = datetime.now(timezone.utc)
    if device.last_seen_at is None or now - device.last_seen_at > SEEN_RESOLUTION:
        device.last_seen_at = now
    await session.commit()
    return caller


async def current_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> Caller:
    """Full rights: an ordinary access token, and nothing narrower."""
    return await _authenticated(request, credentials, session, frozenset({None}))


async def sync_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> Caller:
    """Sync only. Accepts a full access token too, because the foreground uses
    the same endpoints and has no reason to hold a second token to do it."""
    return await _authenticated(
        request, credentials, session, frozenset({None, SCOPE_SYNC})
    )
