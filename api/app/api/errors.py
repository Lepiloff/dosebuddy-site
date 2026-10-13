"""One error envelope, as the contract specifies.

    {"error": {"code": "profile_not_found", "message": "…"}}

`code` is a stable string a client can branch on. `message` is for a developer
reading logs — not localised, and free to change.

**Nothing from the request is echoed back.** That is a rule, not a style
preference. FastAPI's default validation response includes the offending input,
which sounds helpful until the offending input is a NaN: the error response then
cannot be serialised at all, and a client that sent one bad float gets a 500 and
an unhandled exception in the logs instead of a clean 422. Echoing input also
quietly puts medication names into error payloads and, from there, into whatever
logs those responses.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from app.db.errors import sqlstate

log = structlog.get_logger("api.validation")


def envelope(
    code: str, message: str, status_code: int, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}},
        headers=headers,
    )


def install(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field names and reasons, never values. Enough to fix the client,
        # nothing that could carry article 9 content or an unserialisable float.
        where = ", ".join(
            ".".join(str(p) for p in e.get("loc", ())[1:]) or "body" for e in exc.errors()
        )

        # And the same thing into the log, which it was not before. A client
        # sent `push_token` where the contract says `fcm_token`, so every attempt
        # to register for push was refused — thirteen times, over two days, with
        # nothing on this side recording which field was wrong. The reason
        # existed only in a response body nobody kept, so from here the failure
        # was thirteen anonymous 422s, and the caregiver alerts that silently
        # depended on it took a bug report and an evening to trace back.
        #
        # Same rule as the response: names and reasons, no values.
        log.warning(
            "request.rejected",
            method=request.method,
            path=request.url.path,
            fields=[
                {
                    "at": ".".join(str(p) for p in e.get("loc", ())[1:]) or "body",
                    "why": e.get("type", "unknown"),
                }
                for e in exc.errors()
            ],
        )
        return envelope("invalid_request", f"invalid or missing: {where}", 422)

    @app.exception_handler(HTTPException)
    async def _http(_request: Request, exc: HTTPException) -> JSONResponse:
        # Handlers raise HTTPException with the stable code as the detail, so
        # the code stays where it is thrown rather than being mapped here.
        code = exc.detail if isinstance(exc.detail, str) else "error"
        return envelope(code, code.replace("_", " "), exc.status_code, exc.headers)

    @app.exception_handler(DBAPIError)
    async def _database(_request: Request, exc: DBAPIError) -> JSONResponse:
        # Request-scoped session cleanup finishes before exception handling.
        # Only PostgreSQL lock timeout uses the existing transient 429 contract;
        # pool exhaustion, disconnects and other database errors stay private.
        if sqlstate(exc) == "55P03":
            return envelope("rate_limited", "rate limited", 429, {"Retry-After": "5"})
        return await _unhandled(_request, exc)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        # Deliberately says nothing. A stack trace or an exception message can
        # carry a connection string, and the connection string carries the
        # password.
        return envelope(
            "internal_error",
            "internal error",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
