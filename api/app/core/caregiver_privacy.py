"""Private endpoint responses, including failures outside route handlers."""

from starlette.datastructures import MutableHeaders
import structlog

from app.api.errors import envelope

PATH = "/v1/caregiver-events"
HEADERS = {"Cache-Control": "private, no-store", "Pragma": "no-cache", "Vary": "Authorization"}
log = structlog.get_logger(__name__)


class CaregiverPrivacy:
    def __init__(self, app, path=PATH):
        self.app, self.path = app, path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] not in (self.path, self.path + "/"):
            return await self.app(scope, receive, send)
        started, completed = False, False
        async def private_send(message):
            nonlocal started, completed
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name in ("etag", "last-modified"):
                    if name in headers:
                        del headers[name]
                for name, value in HEADERS.items():
                    headers[name] = value
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True
            await send(message)
        try:
            if scope["path"].endswith("/"):
                return await envelope("not_found", "not found", 404)(scope, receive, private_send)
            await self.app(scope, receive, private_send)
        except Exception:
            # Do not let uvicorn print exception messages/SQL parameters/request
            # URI. This endpoint emits finite JSON, never streaming user data.
            log.warning("caregiver_events.failed", path=self.path, status=500)
            if not started:
                await envelope("internal_error", "internal error", 500)(scope, receive, private_send)
            elif not completed:
                await private_send({"type": "http.response.body", "body": b"", "more_body": False})
