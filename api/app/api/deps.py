"""Shared request dependencies."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import read_access_token
from app.db.models import Account, Device

bearer = HTTPBearer(auto_error=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = request.app.state.sessionmaker
    async with factory() as session:
        yield session


class Caller:
    """Who is asking, and from which device."""

    def __init__(self, account: Account, device_id: uuid.UUID):
        self.account = account
        self.device_id = device_id


async def current_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> Caller:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing_token")

    try:
        claims = read_access_token(request.app.state.settings.jwt_secret, credentials.credentials)
    except jwt.PyJWTError:
        # One answer for expired, forged and malformed alike. Distinguishing
        # them tells an attacker which part of the guess was right.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_token") from None

    account = await session.get(Account, uuid.UUID(claims["sub"]))
    if account is None or account.deleted_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_token")

    # The device is re-checked on every request rather than trusted from the
    # token. Unlinking a device has to take effect immediately, and a 15-minute
    # window in which a revoked device still works is 15 minutes too long for
    # access to someone else's health data.
    device_id = uuid.UUID(claims["did"])
    device = (
        await session.execute(
            select(Device).where(Device.id == device_id, Device.revoked_at.is_(None))
        )
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "device_revoked")

    return Caller(account=account, device_id=device_id)
