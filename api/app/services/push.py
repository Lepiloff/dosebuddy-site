"""Sending a push through FCM.

Behind an interface for the same reason Google verification is: the tests that
matter are about who gets told and how often, and those must not need Firebase
credentials or a network. It also means the alert loop can run in production
before the credentials exist — logging what it would have sent instead of
failing, which is a better state to deploy into than one that crashes on the
first missed dose.

`send` reports one of three outcomes rather than true/false. A boolean forces
the caller to guess which kind of failure it had, and the two kinds want
opposite treatment: retrying a dead token is pointless forever, and giving up on
a 503 loses an alert to a blip lasting seconds.
"""

from __future__ import annotations

import enum
from typing import Protocol

import httpx
import structlog
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import service_account

log = structlog.get_logger(__name__)


class Delivery(enum.Enum):
    ok = "ok"
    # Try again later: the message is fine, the moment was not.
    retry = "retry"
    # This token will never accept anything again — reinstalled app, wiped
    # device. Retrying wastes attempts an alert may still need for a live token.
    gone = "gone"


class Push(Protocol):
    async def send(self, token: str, data: dict[str, str], collapse: str) -> Delivery: ...


class LoggingPush:
    """What runs until a Firebase service account is configured.

    Reports `retry`, not `gone`: nothing about the message failed, and once
    credentials exist the alert should still go. Saying `gone` here would burn
    through the attempts of every alert raised before configuration.
    """

    async def send(self, token: str, data: dict[str, str], collapse: str) -> Delivery:
        log.warning("push.not_configured", type=data.get("type"), token_suffix=token[-6:])
        return Delivery.retry


# What FCM's errors mean for us. Anything unlisted is treated as retryable: a
# new error code we have not seen is more likely a transient we should ride out
# than a reason to drop a caregiver's alert on the floor.
_PERMANENT = {
    400,  # INVALID_ARGUMENT — malformed token or payload
    404,  # UNREGISTERED — the token is dead
}


class FcmPush:
    """FCM HTTP v1.

    The payload is data-only and carries no medication name — see
    services.alerts.Alert. A notification body written here would put article 9
    content through Google for the sake of some text on a lock screen.
    """

    def __init__(self, project_id: str, credentials_path: str, ttl_seconds: int = 21600):
        self._project_id = project_id
        self._credentials_path = credentials_path
        self._ttl_seconds = ttl_seconds

    async def send(self, token: str, data: dict[str, str], collapse: str) -> Delivery:
        creds = service_account.Credentials.from_service_account_file(
            self._credentials_path,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"],
        )
        creds.refresh(GoogleRequest())

        url = f"https://fcm.googleapis.com/v1/projects/{self._project_id}/messages:send"
        message = {
            "token": token,
            "data": data,
            "android": {
                # Two sends of the same alert land as one notification. This is
                # what makes retrying safe: without it, at-least-once delivery
                # would mean a caregiver occasionally woken twice for one dose,
                # and a caregiver woken twice learns to ignore the third time.
                "collapse_key": collapse,
                "priority": "high",
                # FCM drops it rather than delivering something stale if the
                # phone has been off longer than the alert stays useful.
                "ttl": f"{self._ttl_seconds}s",
            },
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {creds.token}"},
                    json={"message": message},
                )
        except httpx.HTTPError as exc:
            log.warning("push.unreachable", error=type(exc).__name__, type=data.get("type"))
            return Delivery.retry

        if r.status_code == 200:
            return Delivery.ok

        outcome = Delivery.gone if r.status_code in _PERMANENT else Delivery.retry
        log.warning(
            "push.rejected",
            status=r.status_code,
            outcome=outcome.value,
            type=data.get("type"),
        )
        return outcome


def build_push(settings) -> Push:
    """One place that decides which sender is in use.

    Lives here rather than in the worker because the API sends too: handing
    reminder authority to another device nudges the previous one to stop.
    """
    if settings.fcm_project_id and settings.fcm_credentials_path:
        return FcmPush(settings.fcm_project_id, settings.fcm_credentials_path)
    return LoggingPush()
