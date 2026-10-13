"""Sending a push through FCM.

Behind an interface for the same reason Google verification is: the tests that
matter are about who gets told and how often, and those must not need Firebase
credentials or a network. It also means the alert loop can run in production
before the credentials exist — logging what it would have sent instead of
failing, which is a better state to deploy into than one that crashes on the
first missed dose.

`send` reports distinct outcomes rather than true/false. A boolean forces
the caller to guess which kind of failure it had, and the two kinds want
opposite treatment: retrying a dead token is pointless forever, and giving up on
a 503 loses an alert to a blip lasting seconds.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx
import structlog
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import service_account

log = structlog.get_logger(__name__)

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def remaining_ttl(expires_at: datetime, now: datetime) -> int:
    """Whole useful seconds without rounding past the row's deadline."""
    return max(0, int((expires_at - now).total_seconds()))


class Delivery(enum.Enum):
    ok = "ok"
    # Try again later: the message is fine, the moment was not.
    retry = "retry"
    # This token will never accept anything again — reinstalled app, wiped
    # device. Retrying wastes attempts an alert may still need for a live token.
    gone = "gone"
    # The intent outlived its useful life before the next HTTP send began.
    # This says nothing about whether the token is registered.
    expired = "expired"


class Push(Protocol):
    async def send(
        self, token: str, data: dict[str, str], collapse: str | None, ttl_seconds: int,
        *, expires_at: datetime | None = None, clock: Clock | None = None,
    ) -> Delivery: ...


class LoggingPush:
    """What runs until a Firebase service account is configured.

    Reports `retry`, not `gone`: nothing about the message failed, and once
    credentials exist the alert should still go. Saying `gone` here would burn
    through the attempts of every alert raised before configuration.
    """

    async def send(
        self, token: str, data: dict[str, str], collapse: str | None, ttl_seconds: int,
        *, expires_at: datetime | None = None, clock: Clock | None = None,
    ) -> Delivery:
        if ttl_seconds <= 0 or (
            expires_at is not None and remaining_ttl(expires_at, (clock or utcnow)()) <= 0
        ):
            return Delivery.expired
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

    def __init__(self, project_id: str, credentials_path: str):
        self._project_id = project_id
        self._credentials_path = credentials_path

    async def send(
        self, token: str, data: dict[str, str], collapse: str | None, ttl_seconds: int,
        *, expires_at: datetime | None = None, clock: Clock | None = None,
    ) -> Delivery:
        clock = clock or utcnow
        # Direct callers without a SQL deadline still spend their supplied TTL
        # while acquiring credentials, rather than starting it again afterwards.
        expires_at = expires_at or clock() + timedelta(seconds=ttl_seconds)
        ttl_seconds = min(ttl_seconds, remaining_ttl(expires_at, clock()))
        if ttl_seconds <= 0:
            return Delivery.expired

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
                "priority": "high",
                # Filled at the HTTP boundary, after credential/client waits.
            },
        }
        if collapse is not None:
            # Only replaceable nudges use collapse. Independent data-only
            # events omit the field entirely, rather than sending null/empty.
            message["android"]["collapse_key"] = collapse

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # OAuth refresh is synchronous, and client entry may also wait.
                # Neither may renew the original delivery window. FCM ttl=0
                # means immediate delivery, so an exhausted budget must not post.
                ttl_seconds = min(ttl_seconds, remaining_ttl(expires_at, clock()))
                if ttl_seconds <= 0:
                    return Delivery.expired
                message["android"]["ttl"] = f"{ttl_seconds}s"
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

    Called by the worker, and only by the worker. It used to say it lived here
    because the API sends too — the authority nudge — and that was the shape of
    a real defect rather than a stale comment: the API had a sender and no
    credentials, so `build_push` handed it the logging stand-in and every nudge
    was written to a file. The nudge is now queued for the worker instead
    (`services/alerts.resolve_nudge`), and the API has no sender at all.
    """
    if settings.fcm_project_id and settings.fcm_credentials_path:
        return FcmPush(settings.fcm_project_id, settings.fcm_credentials_path)
    return LoggingPush()
