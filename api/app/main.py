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

from app.api import auth, errors, health, pairing, sync
from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.core.observability import RequestLog
from app.db.session import create_engine, create_sessionmaker
from app.services.google import RealGoogleVerifier
from app.services.push import build_push


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
    app.add_middleware(RequestLog)
    app.state.push = build_push(settings)
    errors.install(app)

    # Operational endpoints sit at the root, outside the versioned contract:
    # they answer to the deploy and to monitoring, not to the mobile client,
    # and they must not move when the contract version does.
    app.include_router(health.router)

    # The versioned surface the app talks to.
    v1 = APIRouter(prefix=settings.api_prefix)
    v1.include_router(auth.router)
    v1.include_router(pairing.router)
    v1.include_router(sync.router)
    app.include_router(v1)

    # Verifying a Google ID token is behind an interface so tests can drive the
    # auth flow, including its failure paths, without a Google account and
    # without network. Tests replace this after the app is built.
    if settings.google_client_id:
        app.state.google_verifier = RealGoogleVerifier(settings.google_client_id)
    else:
        app.state.google_verifier = None

    return app


# Deliberately no module-level `app = create_app()`. That would read settings at
# import time, making this module impossible to import without a full
# environment — including in tests, and in anything that merely inspects the
# code. uvicorn is given `--factory` instead, and tests build the app with
# settings of their own.
