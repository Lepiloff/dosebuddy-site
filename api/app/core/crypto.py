"""Encryption for the columns that carry article 9 content.

Medication names, profile names and notes are stored encrypted; times and
statuses are not, because the server has to detect a missed dose and the
caregiver alerts are built on that (contract §6).

**Every ciphertext carries its scheme version.** That byte is the whole reason
moving to client-side encryption later is a change of who holds the key rather
than a schema migration: a column can hold v1 and v2 values at once while it is
being rewritten, and code can tell them apart without guessing.

The key lives in the environment, not in KMS. Reaching KMS from EC2 needs an
instance role whose credentials any code on the box can read, so it does not
defend against the compromise people expect it to; volume encryption already
covers offline disk access. What this does cover is a leaked dump — which is
what actually leaks from small services, and which volume encryption does
nothing for.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import LargeBinary, TypeDecorator

SCHEME_V1 = 1
NONCE_BYTES = 12


class EncryptionKeyMissing(RuntimeError):
    pass


def _key() -> bytes:
    raw = os.environ.get("ENCRYPTION_KEY", "")
    if not raw:
        raise EncryptionKeyMissing(
            "ENCRYPTION_KEY is not set. It is required — starting without it "
            "would write article 9 data in the clear."
        )
    import base64

    key = base64.b64decode(raw)
    if len(key) != 32:
        raise EncryptionKeyMissing("ENCRYPTION_KEY must decode to 32 bytes")
    return key


def encrypt(plaintext: str) -> bytes:
    """version byte ‖ nonce ‖ AES-GCM(ciphertext ‖ tag)."""
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return bytes([SCHEME_V1]) + nonce + ct


def decrypt(blob: bytes) -> str:
    scheme = blob[0]
    if scheme != SCHEME_V1:
        # Loud rather than silent. A value written by a scheme this build does
        # not know is not something to guess at.
        raise ValueError(f"unknown encryption scheme {scheme}")
    nonce = blob[1 : 1 + NONCE_BYTES]
    return AESGCM(_key()).decrypt(nonce, blob[1 + NONCE_BYTES :], None).decode("utf-8")


class EncryptedString(TypeDecorator):
    """Transparent at the ORM boundary, so no call site can forget.

    Nonces are random, so the same plaintext encrypts differently every time.
    That rules out querying or indexing by value — deliberately: identity is the
    Google `sub`, and nothing needs to look an account up by email.
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> bytes | None:
        return None if value is None else encrypt(value)

    def process_result_value(self, value: bytes | None, dialect) -> str | None:
        return None if value is None else decrypt(bytes(value))
