"""Account-bound recovery cursors. Keys are optional until explicitly enabled."""

import base64
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass

from app.core.config import Settings

TTL_MS = 24 * 60 * 60 * 1000
MAX_SEQ = 2**63 - 1
PURPOSE = b"sonadose/caregiver-events/cursor\x00"
_PART = re.compile(r"[A-Za-z0-9_-]+\Z")


class CursorInvalid(ValueError):
    pass


class CursorExpired(ValueError):
    pass


class FeedReset(ValueError):
    pass


class CursorUnavailable(ValueError):
    pass


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not _PART.fullmatch(value):
        raise ValueError("invalid encoding")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _encode(raw) != value:
        raise ValueError("noncanonical encoding")
    return raw


@dataclass(frozen=True)
class Position:
    account_id: str
    feed_epoch: str
    p: int
    h: int
    mode: str
    issued_at: int
    expires_at: int


class CursorCodec:
    def __init__(self, settings: Settings):
        try:
            # Dedicated random 256-bit keys, not JWT/pairing secrets. Old keys
            # remain verifiers >=24h after the last issuer stopped using them.
            keys = [settings.caregiver_cursor_signing_key, *settings.caregiver_cursor_verifier_keys]
            self.keys = tuple(_decode(key) for key in keys)
            if any(len(key) != 32 for key in self.keys):
                raise ValueError("invalid key size")
        except (ValueError, TypeError):
            raise CursorUnavailable("caregiver cursor key configuration unavailable") from None

    def encode(self, position: Position) -> str:
        payload = json.dumps(position.__dict__, sort_keys=True, separators=(",", ":")).encode()
        signed = "ce1." + _encode(payload)
        mac = hmac.digest(self.keys[0], PURPOSE + signed.encode(), hashlib.sha256)
        return signed + "." + _encode(mac)

    def decode(self, token: str, account_id: str, epoch: str, now_ms: int) -> Position:
        try:
            version, payload, signature = token.split(".")
            if not re.fullmatch(r"ce[1-9][0-9]*", version):
                raise ValueError("invalid version")
            signed = version + "." + payload
            mac = _decode(signature)
            if not any(hmac.compare_digest(mac, hmac.digest(key, PURPOSE + signed.encode(), hashlib.sha256))
                       for key in self.keys):
                raise ValueError("invalid signature")
            data = json.loads(_decode(payload))
            if not isinstance(data, dict) or data.get("account_id") != account_id:
                raise ValueError("foreign account")
            if version != "ce1":
                raise FeedReset()
            if not isinstance(data, dict) or set(data) != set(Position.__dataclass_fields__):
                raise ValueError("invalid shape")
            position = Position(**data)
            for value in (position.account_id, position.feed_epoch):
                if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                    raise ValueError("invalid identity")
            if position.account_id != account_id:
                raise ValueError("foreign account")
            if any(type(value) is not int for value in
                   (position.p, position.h, position.issued_at, position.expires_at)):
                raise ValueError("invalid numbers")
            if not 0 <= position.p <= position.h <= MAX_SEQ:
                raise ValueError("invalid positions")
            if (position.mode not in ("continue", "checkpoint")
                    or (position.mode == "continue" and position.p >= position.h)
                    or (position.mode == "checkpoint" and position.p != position.h)):
                raise ValueError("invalid mode")
            if not 0 <= position.issued_at <= now_ms or not 0 < position.expires_at - position.issued_at <= TTL_MS:
                raise ValueError("invalid lifetime")
            if position.feed_epoch != epoch:
                raise FeedReset()
            if now_ms >= position.expires_at:
                raise CursorExpired()
            return position
        except (FeedReset, CursorExpired):
            raise
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise CursorInvalid() from None
