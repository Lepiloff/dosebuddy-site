"""Alerts.

The rules, not the transport. What is worth testing here is who gets told, when,
how often, and — the one with teeth — what the payload does not contain.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import AlertDelivery, AlertKind, AlertState, Device, ProfileMembership
from app.services import alerts
from app.services.push import Delivery
from app.worker import scan_once
from tests.conftest import auth_header, sign_in
from tests.test_sync import _owner_with_data, _pair, dose, ms, push

pytestmark = pytest.mark.asyncio


class RecordingPush:
    """A sender that always succeeds, and remembers what it was asked to send."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.collapse: list[str] = []

    async def send(self, token: str, data: dict, collapse: str) -> Delivery:
        self.sent.append((token, data))
        self.collapse.append(collapse)
        return Delivery.ok


class FailingPush:
    """Fails a set number of times, then succeeds.

    The point of the retry path is that an alert survives a failure, and nothing
    proves that except a sender that fails and then does not.
    """

    def __init__(self, failures: int, outcome: Delivery = Delivery.retry):
        self.remaining = failures
        self.outcome = outcome
        self.attempts = 0

    async def send(self, token: str, data: dict, collapse: str) -> Delivery:
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            return self.outcome
        return Delivery.ok


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
    owner_device.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=48)
    await session.commit()

    from sqlalchemy.ext.asyncio import async_sessionmaker

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)

    kinds = {p["type"] for _, p in pusher.sent}
    assert kinds == {"profile_stale"}


async def test_a_night_of_silence_is_not_reported(api, session, db_engine):
    """An idle Android phone stops talking to the network for hours at a time.

    At the old twelve-hour threshold that was indistinguishable from a phone
    switched off, so the caregiver would have been told most mornings — and an
    alert that is usually wrong is one they learn to dismiss unread.
    """
    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)

    owner_device = (
        await session.execute(
            select(Device).where(Device.account_id == uuid.UUID(owner["account_id"]))
        )
    ).scalars().first()
    owner_device.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=14)
    await session.commit()

    from sqlalchemy.ext.asyncio import async_sessionmaker

    pusher = RecordingPush()
    assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher) == 0


async def test_syncing_counts_as_being_seen(api, session, db_engine):
    """Staleness must mean "has not been in touch", not "has not signed in".

    It read `last_seen_at`, which only auth and pairing wrote. A device can sync
    all day on one access token and never touch either, so a phone doing exactly
    what it should was on course to be reported silent.
    """
    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)

    owner_device = (
        await session.execute(
            select(Device).where(Device.account_id == uuid.UUID(owner["account_id"]))
        )
    ).scalars().first()
    owner_device.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=48)
    await session.commit()

    # A plain pull: no sign-in, no refresh, nothing but the work the app does.
    assert (await api.get("/v1/sync/pull", headers=auth_header(owner))).status_code == 200

    from sqlalchemy.ext.asyncio import async_sessionmaker

    pusher = RecordingPush()
    assert await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher) == 0


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


# ---------------------------------------------------------------------------
# Delivery: what happens when the send does not work first time
# ---------------------------------------------------------------------------


async def _only_delivery(session) -> AlertDelivery:
    rows = list((await session.execute(select(AlertDelivery))).scalars())
    assert len(rows) == 1, f"expected one delivery, got {len(rows)}"
    await session.refresh(rows[0])
    return rows[0]


async def _a_missed_dose(api, session):
    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)
    await push(api, owner, dose_events=[
        dose(did, mid, pid, sid, "missed", at=ms() + 1000, planned=ms() - 3600_000)
    ])
    return owner


async def test_a_refused_push_is_retried_rather_than_lost(api, session, db_engine):
    """The failure this whole mechanism exists for.

    Before, the delivery row was written as `sent_at` before the send was
    attempted, so FCM refusing it lost the alert permanently and silently — and
    the row said it had been delivered, so nothing could find it afterwards.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await _a_missed_dose(api, session)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    t0 = datetime.now(timezone.utc)

    pusher = FailingPush(failures=1)
    assert await scan_once(maker, pusher, now=t0) == 0

    d = await _only_delivery(session)
    assert d.state == AlertState.pending.value
    assert d.attempts == 1
    assert d.sent_at is None, "nothing was delivered, so nothing may claim it was"
    assert d.next_attempt_at > t0

    # The backoff having passed, the same alert goes again and arrives.
    assert await scan_once(maker, pusher, now=t0 + timedelta(minutes=2)) == 1

    d = await _only_delivery(session)
    assert d.state == AlertState.sent.value
    assert d.sent_at is not None


async def test_a_retry_carries_the_same_collapse_key(api, session, db_engine):
    """What makes retrying safe.

    Two deliveries of one alert must land as one notification, or at-least-once
    delivery would occasionally wake a caregiver twice for a single dose — and
    someone woken twice starts ignoring the third time.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await _a_missed_dose(api, session)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    t0 = datetime.now(timezone.utc)

    pusher = RecordingPush()
    await scan_once(maker, pusher, now=t0)

    d = await _only_delivery(session)
    d.state = AlertState.pending.value  # as if the send had failed
    await session.commit()

    await scan_once(maker, pusher, now=t0 + timedelta(minutes=2))

    assert len(pusher.collapse) == 2
    assert pusher.collapse[0] == pusher.collapse[1]


async def test_a_dead_token_is_not_retried(api, session, db_engine):
    """A reinstalled app leaves a token that will never accept anything again.

    Retrying it burns attempts an alert may still need for a live device, and
    fills the log with a failure nobody can act on.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await _a_missed_dose(api, session)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    pusher = FailingPush(failures=99, outcome=Delivery.gone)
    assert await scan_once(maker, pusher, now=datetime.now(timezone.utc)) == 0

    d = await _only_delivery(session)
    assert d.state == AlertState.given_up.value
    assert d.attempts == 1, "a dead token deserves one attempt, not five"


async def test_attempts_run_out_and_say_so(api, session, db_engine):
    """An alert that cannot be delivered ends in a state that admits it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await _a_missed_dose(api, session)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    pusher = FailingPush(failures=99)

    now = datetime.now(timezone.utc)
    for _ in range(alerts.MAX_ATTEMPTS):
        await scan_once(maker, pusher, now=now)
        now += timedelta(hours=2)

    d = await _only_delivery(session)
    assert d.state in (AlertState.given_up.value, AlertState.expired.value)
    assert d.sent_at is None
    assert d.last_error, "why it stopped has to survive, or nobody can debug it"


async def test_an_alert_that_outlived_its_use_is_not_sent_late(api, session, db_engine):
    """A worker down for a day must not wake a caregiver about yesterday.

    Expiry is checked before delivery precisely so that catching up does not
    mean delivering a backlog of things nobody can act on any more.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await _a_missed_dose(api, session)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    t0 = datetime.now(timezone.utc)

    # Raised while the worker was up, and not delivered before it went down.
    failing = FailingPush(failures=99)
    assert await scan_once(maker, failing, now=t0) == 0
    assert (await _only_delivery(session)).state == AlertState.pending.value

    # It comes back after the alert has stopped being worth sending.
    pusher = RecordingPush()
    late = t0 + alerts.TTL[AlertKind.dose_missed] + timedelta(hours=1)
    assert await scan_once(maker, pusher, now=late) == 0

    assert pusher.sent == [], "an alert this old is noise, not help"
    assert (await _only_delivery(session)).state == AlertState.expired.value


async def test_a_new_caregiver_is_not_told_the_whole_history(api, session, db_engine):
    """Pairing must not fire one alert per missed dose ever recorded.

    Detection had no floor: it matched every dose whose planned time was far
    enough in the past, so the first scan after someone was given alerts would
    have raised one for last spring as readily as for this morning. On a phone
    that arrives as a wall of notifications with today's buried in it.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)
    long_ago = ms() - int(timedelta(days=30).total_seconds() * 1000)
    await push(api, owner, dose_events=[
        dose(did, mid, pid, sid, "missed", at=ms() + 1000, planned=long_ago)
    ])

    pusher = RecordingPush()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    assert await scan_once(maker, pusher, now=datetime.now(timezone.utc)) == 0
