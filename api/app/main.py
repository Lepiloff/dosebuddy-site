"""The DoseBuddy API.

Skeleton only, on purpose. Auth, pairing and sync are not here: they follow the
API contract in part 5 of the v1.1 spec, which is the agreed seam between this
track and the app track. Building them before that seam exists is how the two
sides end up disagreeing about it.

What is here is everything the contract will need underneath it — configuration,
a database and cache connection with a lifecycle, logging, and health checks the
deploy can trust.
"""

from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, FastAPI

from app.api import health
from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.db.session import create_engine, create_sessionmaker


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    log = structlog.get_logger(__name__)

    engine = create_engine(settings)
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)
    app.state.redis = aioredis.from_url(
        str(settings.redis_url), decode_responses=True
    )

    # Deliberately no connection attempt at startup. A container that refuses
    # to boot because Postgres is a few seconds behind turns a slow dependency
    # into an outage; /health/ready is what reports the difference, and the
    # deploy waits on that.
    log.info("api.startup", environment=settings.environment, version=settings.version)

    try:
        yield
    finally:
        await app.state.redis.aclose()
        await engine.dispose()
        log.info("api.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings)

    app = FastAPI(
        title="DoseBuddy API",
        version=settings.version,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings

    # Operational endpoints sit at the root, outside the versioned contract:
    # they answer to the deploy and to monitoring, not to the mobile client,
    # and they must not move when the contract version does.
    app.include_router(health.router)

    # The versioned surface the app talks to. Empty until part 5 is agreed.
    v1 = APIRouter(prefix=settings.api_prefix)
    app.include_router(v1)

    return app


# Deliberately no module-level `app = create_app()`. That would read settings at
# import time, making this module impossible to import without a full
# environment — including in tests, and in anything that merely inspects the
# code. uvicorn is given `--factory` instead, and tests build the app with
# settings of their own.
