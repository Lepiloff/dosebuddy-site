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

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def envelope(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


def install(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field names and reasons, never values. Enough to fix the client,
        # nothing that could carry article 9 content or an unserialisable float.
        where = ", ".join(
            ".".join(str(p) for p in e.get("loc", ())[1:]) or "body" for e in exc.errors()
        )
        return envelope("invalid_request", f"invalid or missing: {where}", 422)

    @app.exception_handler(HTTPException)
    async def _http(_request: Request, exc: HTTPException) -> JSONResponse:
        # Handlers raise HTTPException with the stable code as the detail, so
        # the code stays where it is thrown rather than being mapped here.
        code = exc.detail if isinstance(exc.detail, str) else "error"
        return envelope(code, code.replace("_", " "), exc.status_code)

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
