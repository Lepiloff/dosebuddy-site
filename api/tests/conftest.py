"""Test fixtures.

The health tests deliberately do not need a real Postgres or Redis. They drive
the app with stand-ins so that the readiness endpoint can be tested in both
directions — including the failure direction, which is the one that matters and
the one a test needing live infrastructure never exercises.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


def make_settings(**overrides) -> Settings:
    base = {
        "environment": "local",
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/test",
        "redis_url": "redis://localhost:6379/0",
    }
    return Settings(**{**base, **overrides})


class FakeResult:
    pass


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
        return FakeResult()


class FakeSessionmaker:
    def __init__(self, fail: bool = False):
        self._fail = fail

    def __call__(self):
        return FakeSession(self._fail)


class FakeRedis:
    def __init__(self, fail: bool = False):
        self._fail = fail

    async def ping(self):
        if self._fail:
            raise ConnectionError("redis is down")
        return True

    async def aclose(self):
        return None


@pytest.fixture
def app():
    return create_app(make_settings())


@pytest.fixture
async def client(app):
    """A client over the real app, with the dependency lifecycle already run.

    Stand-ins are installed after startup so they replace what lifespan built
    rather than racing it.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        async with app.router.lifespan_context(app):
            app.state.sessionmaker = FakeSessionmaker()
            app.state.redis = FakeRedis()
            yield c
