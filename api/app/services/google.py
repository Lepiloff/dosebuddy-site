"""Verifying a Google ID token.

Behind an interface for one reason: the tests must be able to exercise the auth
flow, including its failure paths, without a Google account and without network.
A test that can only run against Google is a test that does not run.
"""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

# Imported at module level, not inside verify(), deliberately.
#
# `google.auth.transport.requests` needs the `requests` package, which
# google-auth does not pull in by default. Imported lazily, a missing dependency
# surfaced as a 500 on the first real sign-in — past the build, past CI, past
# the health check, at the worst possible moment. Up here it cannot get further
# than starting the process.
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str | None


class InvalidGoogleToken(Exception):
    pass


class GoogleVerifier(Protocol):
    async def verify(self, id_token: str) -> GoogleIdentity: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class _CertificateResponse:
    status: int
    data: bytes
    headers: dict[str, str]


class _CertificateRequest:
    """A single-worker transport: cache only fresh successful cert responses."""

    def __init__(self, timeout: float):
        self._request = google_requests.Request()
        self._timeout = timeout
        self._cached: _CertificateResponse | None = None
        self._cached_url: str | None = None
        self._expires_at = 0.0

    def __call__(self, url, method="GET", **kwargs):
        started = time.monotonic()
        if (method == "GET" and url == self._cached_url
                and self._cached is not None and started < self._expires_at):
            return self._cached

        # google-auth otherwise defaults to a 120-second blocking HTTP call.
        # Bound socket connect/read waits as well as the caller's async budget.
        kwargs["timeout"] = self._timeout
        response = self._request(url, method=method, **kwargs)
        headers = {key.lower(): value for key, value in response.headers.items()}
        copied = _CertificateResponse(response.status, response.data, headers)
        self._cached = None
        self._cached_url = None

        if method == "GET" and response.status == 200:
            directives = {
                part.strip().lower().split("=", 1)[0]: part.strip().split("=", 1)[-1].strip('"')
                for part in headers.get("cache-control", "").split(",")
            }
            try:
                # No guessed lifetime, no expired/stale fallback after errors.
                # Age and the fetch duration reduce the server's lifetime.
                lifetime = max(0, min(3600, int(directives.get("max-age", "0"))))
                age = max(0, int(headers.get("age", "0")))
                valid_json = isinstance(json.loads(copied.data), dict)
            except (ValueError, TypeError):
                lifetime, age, valid_json = 0, 0, False
            if (valid_json and "no-store" not in directives and "no-cache" not in directives
                    and lifetime > age):
                self._cached = copied
                self._cached_url = url
                self._expires_at = started + lifetime - age
        return copied

    def close(self) -> None:
        self._request.session.close()
        self._cached = None


class RealGoogleVerifier:
    def __init__(
        self, client_id: str, *, timeout: float = 5.0,
        transport_timeout: float = 3.0, max_pending: int = 8,
    ):
        if not client_id:
            raise RuntimeError(
                "GOOGLE_CLIENT_ID is empty. Verifying without an audience would "
                "accept a token Google issued for any other application."
            )
        self._client_id = client_id
        self._timeout = timeout
        self._max_pending = max_pending
        self._pending = 0
        self._closed = False
        # The Session and cert cache have exactly one owner. Only one job is
        # submitted to the executor at a time; the bounded queue is async.
        self._slot = asyncio.Semaphore(1)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="google-verify")
        self._request = _CertificateRequest(transport_timeout)

    async def verify(self, id_token: str) -> GoogleIdentity:
        if self._closed:
            raise InvalidGoogleToken("Google verification is closed")
        if self._pending >= self._max_pending:
            raise InvalidGoogleToken("Google verification is busy")

        self._pending += 1
        acquired = False
        submitted = False

        async def run() -> GoogleIdentity:
            nonlocal acquired, submitted
            await self._slot.acquire()
            acquired = True
            if self._closed:
                raise InvalidGoogleToken("Google verification is closed")
            future = asyncio.get_running_loop().run_in_executor(
                self._executor, self._verify, id_token,
            )
            future.add_done_callback(self._finished)
            submitted = True
            # Cancelling/timing out the HTTP caller cannot stop a thread.
            # Keep its slot until the actual job finishes: repeated timeouts
            # must not start more workers or accumulate executor work items.
            identity = await asyncio.shield(future)
            if self._closed:
                raise InvalidGoogleToken("Google verification is closed")
            return identity

        try:
            # Admission, queue wait, network and validation share one budget.
            return await asyncio.wait_for(run(), timeout=self._timeout)
        except asyncio.TimeoutError:
            raise InvalidGoogleToken("Google verification timed out") from None
        finally:
            if not submitted:
                self._pending -= 1
                if acquired:
                    self._slot.release()

    def _finished(self, future: asyncio.Future) -> None:
        self._pending -= 1
        self._slot.release()
        # A caller may have gone away before an exception arrived. Retrieve it
        # so that late transport failures do not become unhandled loop errors.
        if not future.cancelled():
            future.exception()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Wake admitted callers so they fail closed without awaiting Google.
        for _ in range(self._pending):
            self._slot.release()
        # Close the Session on its owner thread after any in-flight job. Never
        # close its socket underneath a request, or block the event loop joining
        # it. Shutdown accepts no more submissions; at most one worker drains.
        self._executor.submit(self._request.close)
        self._executor.shutdown(wait=False)

    def _verify(self, id_token: str) -> GoogleIdentity:
        try:
            claims = google_id_token.verify_oauth2_token(
                id_token, self._request, self._client_id
            )
        except Exception as exc:  # noqa: BLE001 — anything wrong means not authenticated
            raise InvalidGoogleToken(str(exc)) from exc

        # verify_oauth2_token checks signature, expiry and audience. Issuer is
        # checked here because an attacker controlling any other issuer would
        # otherwise only need a matching audience claim.
        if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            raise InvalidGoogleToken("unexpected issuer")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise InvalidGoogleToken("token has no subject")

        return GoogleIdentity(subject=sub, email=claims.get("email"))
