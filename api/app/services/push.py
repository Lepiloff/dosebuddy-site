"""Sending a push through FCM.

Behind an interface for the same reason Google verification is: the tests that
matter are about who gets told and how often, and those must not need Firebase
credentials or a network. It also means the alert loop can run in production
before the credentials exist — logging what it would have sent instead of
failing, which is a better state to deploy into than one that crashes on the
first missed dose.
"""

from __future__ import annotations

from typing import Protocol

import httpx
import structlog
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import service_account

log = structlog.get_logger(__name__)


class Push(Protocol):
    async def send(self, token: str, data: dict[str, str]) -> bool: ...


class LoggingPush:
    """What runs until a Firebase service account is configured.

    Says plainly that nothing was delivered. A no-op that logged nothing would
    let a broken alert path look identical to a quiet one.
    """

    async def send(self, token: str, data: dict[str, str]) -> bool:
        log.warning("push.not_configured", type=data.get("type"), token_suffix=token[-6:])
        return False


class FcmPush:
    """FCM HTTP v1.

    The payload is data-only and carries no medication name — see
    services.alerts.Alert. A notification body written here would put article 9
    content through Google for the sake of some text on a lock screen.
    """

    def __init__(self, project_id: str, credentials_path: str):
        self._project_id = project_id
        self._credentials_path = credentials_path
        self._session = None

    async def send(self, token: str, data: dict[str, str]) -> bool:
        creds = service_account.Credentials.from_service_account_file(
            self._credentials_path,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"],
        )
        creds.refresh(GoogleRequest())

        url = f"https://fcm.googleapis.com/v1/projects/{self._project_id}/messages:send"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {creds.token}"},
                json={"message": {"token": token, "data": data}},
            )

        if r.status_code == 200:
            return True

        # A token that Google no longer recognises is normal: the app was
        # reinstalled, or the device wiped. Worth a line, not an exception.
        log.warning("push.rejected", status=r.status_code, type=data.get("type"))
        return False
