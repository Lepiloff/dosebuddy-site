"""Purpose/shape/version/stable verifier checks; no network or database."""
import base64
import hashlib
import hmac
from dataclasses import replace
import uuid

import pytest

from app.core.caregiver_cursor import CursorCodec, CursorExpired, CursorInvalid, CursorUnavailable, FeedReset, Position, PURPOSE, TTL_MS
from tests.conftest import make_settings

KEY = base64.urlsafe_b64encode(b"r" * 32).rstrip(b"=").decode()


def test_stable_cursor_boundaries_and_purpose():
    settings = make_settings(caregiver_cursor_signing_key=KEY)
    codec = CursorCodec(settings)
    position = Position(str(uuid.uuid4()), str(uuid.uuid4()), 3, 4, "continue", 1000, 1000 + TTL_MS)
    token = codec.encode(position)
    assert CursorCodec(settings).decode(token, position.account_id, position.feed_epoch, 1001) == position
    with pytest.raises(CursorExpired):
        codec.decode(token, position.account_id, position.feed_epoch, position.expires_at)
    prefix, payload, _ = token.split(".")
    raw = base64.urlsafe_b64decode(KEY+"=")
    mac = base64.urlsafe_b64encode(hmac.digest(raw, b"other-purpose"+(prefix+"."+payload).encode(), hashlib.sha256)).rstrip(b"=").decode()
    with pytest.raises(CursorInvalid):
        codec.decode(prefix+"."+payload+"."+mac, position.account_id, position.feed_epoch, 1001)
    signed = "ce2."+payload
    mac = base64.urlsafe_b64encode(hmac.digest(raw, PURPOSE+signed.encode(), hashlib.sha256)).rstrip(b"=").decode()
    with pytest.raises(FeedReset):
        codec.decode(signed+"."+mac, position.account_id, position.feed_epoch, 1001)


@pytest.mark.parametrize("changes", [dict(mode="future"), dict(p=3, h=3), dict(issued_at=2000),
    dict(expires_at=1000), dict(expires_at=1001+TTL_MS), dict(h=2**63), dict(account_id="wrong")])
def test_impossible_signed_cursor_is_not_accepted(changes):
    codec = CursorCodec(make_settings(caregiver_cursor_signing_key=KEY))
    position = Position(str(uuid.uuid4()), str(uuid.uuid4()), 3, 4, "continue", 1000, 1000+TTL_MS)
    with pytest.raises(CursorInvalid):
        codec.decode(codec.encode(replace(position, **changes)), position.account_id, position.feed_epoch, 1001)


@pytest.mark.parametrize("key", ["", "wrong", "a"*42, "a"*44])
def test_optional_missing_or_bad_key_is_rejected_only_when_reader_needs_it(key):
    settings = make_settings(caregiver_cursor_signing_key=key)
    with pytest.raises(CursorUnavailable):
        CursorCodec(settings)
