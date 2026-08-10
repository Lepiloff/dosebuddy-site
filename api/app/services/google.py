"""Verifying a Google ID token.

Behind an interface for one reason: the tests must be able to exercise the auth
flow, including its failure paths, without a Google account and without network.
A test that can only run against Google is a test that does not run.
"""

from __future__ import annotations

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
    def verify(self, id_token: str) -> GoogleIdentity: ...


class RealGoogleVerifier:
    def __init__(self, client_id: str):
        if not client_id:
            raise RuntimeError(
                "GOOGLE_CLIENT_ID is empty. Verifying without an audience would "
                "accept a token Google issued for any other application."
            )
        self._client_id = client_id

    def verify(self, id_token: str) -> GoogleIdentity:
        try:
            claims = google_id_token.verify_oauth2_token(
                id_token, google_requests.Request(), self._client_id
            )
        except Exception as exc:  # noqa: BLE001 — anything wrong means not authenticated
            raise InvalidGoogleToken(str(exc)) from exc

        # verify_oauth2_token checks signature, expiry and audience. Issuer is
        # checked here because an attacker controlling any other issuer would
        # otherwise only need a matching audience claim.
        if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            raise InvalidGoogleToken("unexpected issuer")
        sub = claims.get("sub")
        if not sub:
            raise InvalidGoogleToken("token has no subject")

        return GoogleIdentity(subject=sub, email=claims.get("email"))
