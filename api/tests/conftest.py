"""Test fixtures.

Two levels, on purpose.

The health tests drive the app with stand-ins so they can exercise the failure
paths — a readiness check that only ever sees a working database is not testing
the thing that matters.

The auth and pairing tests run against a real Postgres. Encrypted columns,
partial unique indexes and a sequence are not things SQLite can stand in for,
and a test that passes against a substitute proves nothing about the schema that
actually ships.
"""

import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.security import mint_access_token
from app.db.models import Account, Device, Profile
from app.db.session import Base
from app.main import create_app
from app.services.google import GoogleIdentity, InvalidGoogleToken

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/dosebuddy_test",
)
TEST_KEY = "0" * 43 + "="  # 32 zero bytes, base64


def make_settings(**overrides) -> Settings:
    base = {
        "environment": "local",
        "database_url": TEST_DATABASE_URL,
        "redis_url": "redis://localhost:6379/0",
        "jwt_secret": "test-secret-long-enough-for-hs256-abcdef",
        "encryption_key": TEST_KEY,
    }
    return Settings(**{**base, **overrides})


# --------------------------------------------------------------------------
# Stand-ins, for the health tests
# --------------------------------------------------------------------------


class FakeSession:
    def __init__(self, fail: bool = False):
        self._fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_args, **_kwargs):
        if self._fail:
            raise ConnectionError("database is down")
        return object()


class FakeSessionmaker:
    def __init__(self, fail: bool = False):
        self._fail = fail

    def __call__(self):
        return FakeSession(self._fail)


class FakeRedis:
    """Enough Redis for the rate limiter, and no more."""

    def __init__(self, fail: bool = False):
        self._fail = fail
        self.values: dict[str, int] = {}

    async def ping(self):
        if self._fail:
            raise ConnectionError("redis is down")
        return True

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def aclose(self):
        return None


class FakeGoogle:
    """Maps an id_token straight to a subject, so a test can say who is signing
    in without a Google account or a network."""

    def __init__(self):
        self.identities: dict[str, GoogleIdentity] = {}

    def add(self, token: str, subject: str, email: str | None = None) -> None:
        self.identities[token] = GoogleIdentity(subject=subject, email=email)

    def verify(self, id_token: str) -> GoogleIdentity:
        if id_token not in self.identities:
            raise InvalidGoogleToken("unknown token")
        return self.identities[id_token]


@pytest.fixture
def app():
    os.environ["ENCRYPTION_KEY"] = TEST_KEY
    return create_app(make_settings())


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            app.state.sessionmaker = FakeSessionmaker()
            app.state.redis = FakeRedis()
            yield c


# --------------------------------------------------------------------------
# The real thing, for auth and pairing
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    os.environ["ENCRYPTION_KEY"] = TEST_KEY
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE SEQUENCE IF NOT EXISTS server_seq")
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def api(app, db_engine):
    """The app wired to a real database, a fake Redis and a fake Google."""
    app.state.sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    app.state.redis = FakeRedis()
    app.state.google_verifier = FakeGoogle()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


@pytest_asyncio.fixture
async def session(api, db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        yield s


async def sign_in(api, subject: str, email: str | None = None) -> dict:
    """Sign a new account in and return the token pair."""
    token = f"google-token-{subject}"
    api.app.state.google_verifier.add(token, subject, email)
    r = await api.post(
        "/v1/auth/google",
        json={
            "id_token": token,
            "device": {"id": str(uuid.uuid4()), "platform": "android", "app_version": "1.1.0"},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def auth_header(pair: dict) -> dict:
    return {"Authorization": f"Bearer {pair['access_token']}"}


async def make_profile(session, account_id: str, name: str = "Profile") -> uuid.UUID:
    """Profiles arrive over sync, which is not built yet, so tests create them
    directly rather than pretending an endpoint exists."""
    profile = Profile(id=uuid.uuid4(), owner_account_id=uuid.UUID(account_id), name=name)
    session.add(profile)
    await session.commit()
    return profile.id
