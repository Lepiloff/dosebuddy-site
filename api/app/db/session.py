"""Database engine and session.

Async throughout: the work this service will do is almost entirely waiting on
Postgres and on FCM, so a thread per request would buy nothing.

There are no models here yet, and that is deliberate. The server schema mirrors
the device model, which is designed on the app side (spec 0.5), and the schema
itself is an open decision in part 6 to be agreed rather than assumed.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import Settings


class Base(DeclarativeBase):
    """Declarative base for the models that arrive with the agreed schema."""


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        str(settings.database_url),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # Connections idle in the pool are dropped by anything in the middle
        # long before Postgres itself gives up on them. Checking liveness on
        # checkout turns a stale-connection error into a reconnect.
        pool_pre_ping=True,
        echo=False,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session bound to the app's engine."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with factory() as session:
        yield session
