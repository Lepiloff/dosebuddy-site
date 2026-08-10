"""Liveness and readiness.

Two endpoints, because they answer different questions and the deploy depends
on the difference:

- /health says the process is up. It touches nothing else, so it stays true
  during a Postgres restart and cannot be dragged down by a dependency.
- /health/ready says the process can actually serve. It checks Postgres and
  Redis and answers 503 when either is unreachable.

Collapsing them into one endpoint means either a brief database blip looks like
a dead process, or a process with no database looks healthy. Both are worse
than having two.
"""

import asyncio

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_SECONDS = 3


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    return {"status": "ok", "version": request.app.state.settings.version}


async def _check_database(request: Request) -> bool:
    factory = request.app.state.sessionmaker
    async with factory() as session:
        await session.execute(text("SELECT 1"))
    return True


async def _check_redis(request: Request) -> bool:
    await request.app.state.redis.ping()
    return True


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict[str, object]:
    checks: dict[str, str] = {}

    for name, check in (("database", _check_database), ("redis", _check_redis)):
        try:
            await asyncio.wait_for(check(request), timeout=CHECK_TIMEOUT_SECONDS)
            checks[name] = "ok"
        except Exception as exc:  # noqa: BLE001 — any failure means not ready
            # The exception type, never its message: a connection error can
            # carry the DSN, and the DSN carries the password.
            checks[name] = f"unavailable: {type(exc).__name__}"

    ok = all(v == "ok" for v in checks.values())
    response.status_code = (
        status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return {"status": "ready" if ok else "not ready", "checks": checks}
