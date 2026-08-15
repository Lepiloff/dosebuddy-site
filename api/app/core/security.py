"""Tokens: minting, hashing, verifying.

Two kinds, on purpose. The access token is a short-lived JWT the server can
check without touching the database. The refresh token is opaque and stored
hashed, so it can actually be revoked — "remove this caregiver" and "unlink this
device" have to take effect now, not whenever a JWT happens to expire.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt

ACCESS_TOKEN_TTL = timedelta(minutes=15)
REFRESH_TOKEN_TTL = timedelta(days=90)
PAIRING_CODE_TTL = timedelta(minutes=10)

# A background task on the device cannot use the refresh token. Refresh rotates
# on use, and the app has two halves — the screen and the background worker —
# which would present the same token at once, look exactly like a stolen copy
# being replayed, and get the whole device signed out. So the background half
# was forbidden the network, and the cost of that was the point of the product:
# a dose confirmed from a notification, or missed while its owner slept, sat on
# the phone until someone opened the app. The caregiver learned nothing.
#
# This token exists for that background half, and its whole design is the two
# things that make it safe to hold for a long time:
#
#   - it grants **only** sync, so a copy cannot move reminder authority, pair a
#     new caregiver, or delete the account;
#   - it dies with the device, because deps.current_caller re-reads the device
#     row on every request and refuses a revoked one. That check already existed
#     for "unlink this device now", and it is what makes a long-lived signed
#     token revocable without storing anything.
#
# What it gives up: rotation, and with it the ability to notice a stolen copy in
# use. Accepted knowingly. The device already holds a refresh token that lives
# just as long and can do strictly more, so this widens no door that is shut.
SYNC_TOKEN_TTL = timedelta(days=90)

# The claim naming what a token may do. Absent means an ordinary access token:
# everything. Present means restricted to exactly that, and nothing else.
SCOPE_SYNC = "sync"

# Crockford-ish: no I, L, O, U. Someone reads this aloud off one phone and types
# it into another, so the characters that get misheard or misread are removed
# rather than validated after the fact.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 6


def mint_access_token(secret: str, account_id: uuid.UUID, device_id: uuid.UUID) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(account_id),
        "did": str(device_id),
        "iat": int(now.timestamp()),
        "exp": int((now + ACCESS_TOKEN_TTL).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm="HS256"), int(ACCESS_TOKEN_TTL.total_seconds())


def mint_sync_token(secret: str, account_id: uuid.UUID, device_id: uuid.UUID) -> tuple[str, int]:
    """Long-lived, and good for sync alone. See SYNC_TOKEN_TTL."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(account_id),
        "did": str(device_id),
        "scope": SCOPE_SYNC,
        "iat": int(now.timestamp()),
        "exp": int((now + SYNC_TOKEN_TTL).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm="HS256"), int(SYNC_TOKEN_TTL.total_seconds())


def read_access_token(secret: str, token: str) -> dict:
    """Raises jwt.PyJWTError on anything wrong — expiry, signature, shape."""
    return jwt.decode(token, secret, algorithms=["HS256"])


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    """Plain SHA-256 is enough here: the token is 256 bits of randomness, so
    there is no dictionary to run against it."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_pairing_code() -> str:
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return f"{body[:3]}-{body[3:]}"


def hash_pairing_code(secret: str, code: str) -> str:
    """HMAC rather than a bare hash.

    Six characters is ~10^9 possibilities — trivially brute-forced offline if
    the table leaks. Keying the hash with the server secret means a leaked table
    on its own reveals nothing.
    """
    normalised = code.replace("-", "").replace(" ", "").upper()
    return hmac.new(secret.encode(), normalised.encode(), hashlib.sha256).hexdigest()
