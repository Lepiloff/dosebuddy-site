"""Request logging, and the rule about what must never appear in it.

Written for the moment the first real client tries to sign in. Until now a
failure was invisible from both ends: the client is told `invalid_google_token`
and nothing else — deliberately, since distinguishing expired from
wrong-audience for a caller also distinguishes them for an attacker — and the
server said nothing at all. That is fine for an attacker and useless for the
person integrating.

**Never in a log line:** medication names, profile names, notes, request or
response bodies, tokens. Identifiers are fine — a profile id says which row,
not what is in it. Logs are copied, shipped and kept far more casually than a
database, so anything written here should be assumed permanent and widely read.
"""

from __future__ import annotations

import re
import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

log = structlog.get_logger("api.request")

# The container health check runs every ten seconds. Left in, it drowns the
# handful of lines that matter.
QUIET_PATHS = {"/health", "/health/ready"}

# Anything shaped like a JWT. An exception message that echoes the token would
# put a live credential in the log, and the log outlives the token.
_JWT = re.compile(r"\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")


def redact(message: str, limit: int = 300) -> str:
    return _JWT.sub("<token>", message)[:limit]


class RequestLog(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in QUIET_PATHS:
            return await call_next(request)

        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - started) * 1000)

        log.info(
            "http",
            method=request.method,
            # The path, not the query string: a cursor is harmless but the query
            # string is the easiest place for something unintended to end up.
            path=request.url.path,
            status=response.status_code,
            ms=elapsed_ms,
        )
        return response
