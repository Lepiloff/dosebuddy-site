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
