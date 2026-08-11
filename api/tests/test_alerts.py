"""Alerts.

The rules, not the transport. What is worth testing here is who gets told, when,
how often, and — the one with teeth — what the payload does not contain.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import AlertKind, Device, ProfileMembership
from app.services import alerts
from app.worker import scan_once
from tests.conftest import auth_header, sign_in
from tests.test_sync import _owner_with_data, _pair, dose, ms, push

pytestmark = pytest.mark.asyncio


class RecordingPush:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send(self, token: str, data: dict) -> bool:
        self.sent.append((token, data))
        return True


async def _with_watcher(api, session, role="with_alerts"):
    owner, pid, mid, sid, did = await _owner_with_data(api)
    caregiver = await _pair(api, owner, pid, role)

    # A push token, or there is nobody to send to.
    device = (
        await session.execute(
            select(Device).where(Device.account_id == uuid.UUID(caregiver["account_id"]))
        )
    ).scalars().first()
    device.push_token = "token-" + caregiver["account_id"][:8]
    await session.commit()

    return owner, caregiver, pid, mid, sid, did


async def test_a_reported_miss_alerts_the_watcher(api, session, db_engine):
    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)
    await push(api, owner, dose_events=[
        dose(did, mid, pid, sid, "missed", at=ms() + 1000, planned=ms() - 3600_000)
    ])

    from sqlalchemy.ext.asyncio import async_sessionmaker

    pusher = RecordingPush()
    sent = await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)

    assert sent == 1
    _, payload = pusher.sent[0]
    assert payload["type"] == "dose_missed"
    assert payload["profile_id"] == pid


async def test_the_payload_carries_no_medication_name(api, session, db_engine):
    """FCM is Google. A notification body naming the drug would hand a third
    party exactly what the rest of the design keeps from them; the app already
    has the data and writes the wording itself."""
    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)
    await push(api, owner, dose_events=[
        dose(did, mid, pid, sid, "missed", at=ms() + 1000, planned=ms() - 3600_000)
    ])

    from sqlalchemy.ext.asyncio import async_sessionmaker

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)

    blob = str(pusher.sent)
    assert "Aspirin" not in blob
    assert "name" not in pusher.sent[0][1]


async def test_the_same_dose_is_not_alerted_twice(api, session, db_engine):
    """A caregiver told twice about one dose learns to ignore the third."""
    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)
    await push(api, owner, dose_events=[
        dose(did, mid, pid, sid, "missed", at=ms() + 1000, planned=ms() - 3600_000)
    ])

    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    pusher = RecordingPush()

    assert await scan_once(maker, pusher) == 1
    assert await scan_once(maker, pusher) == 0


async def test_a_miss_within_the_threshold_waits(api, session, db_engine):
    """Thirty minutes by default, so a dose taken a little late does not summon
    the family."""
    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)
    await push(api, owner, dose_events=[dose(did, mid, pid, sid, "missed", at=ms())])

    from sqlalchemy.ext.asyncio import async_sessionmaker

    pusher = RecordingPush()
    assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher) == 0


async def test_a_viewer_is_not_interrupted(api, session, db_engine):
    """`viewer` asked to be able to look, not to be woken."""
    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session, role="viewer")
    await push(api, owner, dose_events=[
        dose(did, mid, pid, sid, "missed", at=ms() + 1000, planned=ms() - 3600_000)
    ])

    from sqlalchemy.ext.asyncio import async_sessionmaker

    pusher = RecordingPush()
    assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher) == 0


async def test_silence_is_reported_separately_from_a_miss(api, session, db_engine):
    """Not as a miss. The server cannot tell "did not take it" from "phone is
    off", and dressing one up as the other is a lie in whichever direction."""
    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)

    owner_device = (
        await session.execute(
            select(Device).where(Device.account_id == uuid.UUID(owner["account_id"]))
        )
    ).scalars().first()
    owner_device.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=20)
    await session.commit()

    from sqlalchemy.ext.asyncio import async_sessionmaker

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)

    kinds = {p["type"] for _, p in pusher.sent}
    assert kinds == {"profile_stale"}


async def test_a_profile_that_has_never_synced_is_not_stale(api, session, db_engine):
    """Silence from a device that never spoke is a profile not yet started, not
    one gone quiet. Greeting a new caregiver with an alarm would be wrong."""
    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)

    owner_device = (
        await session.execute(
            select(Device).where(Device.account_id == uuid.UUID(owner["account_id"]))
        )
    ).scalars().first()
    owner_device.last_seen_at = None
    await session.commit()

    from sqlalchemy.ext.asyncio import async_sessionmaker

    pusher = RecordingPush()
    assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher) == 0


async def test_a_revoked_watcher_stops_being_told(api, session, db_engine):
    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)
    await push(api, owner, dose_events=[
        dose(did, mid, pid, sid, "missed", at=ms() + 1000, planned=ms() - 3600_000)
    ])

    await api.delete(
        f"/v1/profiles/{pid}/members/{caregiver['account_id']}", headers=auth_header(owner)
    )

    from sqlalchemy.ext.asyncio import async_sessionmaker

    pusher = RecordingPush()
    assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher) == 0


async def test_the_real_sender_can_be_constructed():
    """Constructs FcmPush rather than a stand-in, for the reason the Google
    verifier has the same test: a stand-in cannot catch a missing dependency,
    and that exact omission already shipped once — httpx was imported inside the
    send method and declared nowhere, so it would have failed on the first
    missed dose rather than at startup."""
    from app.services.push import FcmPush

    sender = FcmPush("some-project", "/nonexistent/key.json")
    assert sender is not None
