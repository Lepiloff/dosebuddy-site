"""Alerts.

The rules, not the transport. What is worth testing here is who gets told, when,
how often, and — the one with teeth — what the payload does not contain.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import (
    AlertDelivery,
    AlertKind,
    AlertState,
    Device,
    Profile,
    ProfileMembership,
    utcnow,
)
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
        self.ttl: list[int] = []

    async def send(self, token: str, data: dict, collapse: str, ttl_seconds: int) -> Delivery:
        self.sent.append((token, data))
        self.collapse.append(collapse)
        self.ttl.append(ttl_seconds)
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

    async def send(self, token: str, data: dict, collapse: str, ttl_seconds: int) -> Delivery:
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            return self.outcome
        return Delivery.ok


async def _owner_device(session, owner) -> Device:
    return (
        await session.execute(
            select(Device).where(Device.account_id == uuid.UUID(owner["account_id"]))
        )
    ).scalars().first()


async def _with_watcher(api, session, role="with_alerts"):
    owner, pid, mid, sid, did = await _owner_with_data(api)
    caregiver = await _pair(api, owner, pid, role)

    # The owner's phone claims reminder authority, as a real one does on first
    # run. Without it the profile has no reminding device, and staleness — which
    # is about that device specifically — has nothing to be about.
    owners_phone = await _owner_device(session, owner)
    r = await api.post(
        f"/v1/profiles/{pid}/reminder-authority",
        headers=auth_header(owner),
        json={"device_id": str(owners_phone.id)},
    )
    assert r.status_code in (200, 204), r.text

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

    owner_device = await _owner_device(session, owner)
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

    owner_device = await _owner_device(session, owner)
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

    owner_device = await _owner_device(session, owner)
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

    owner_device = await _owner_device(session, owner)
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


# ---------------------------------------------------------------------------
# Which device, and which profile — the two ways this signal went quiet
# ---------------------------------------------------------------------------


async def test_a_second_device_does_not_mask_the_silent_one(api, session, db_engine):
    """Only the device that arms the alarms counts.

    This asked the owner's account for its most recently seen device, so a
    tablet still signed in kept the answer fresh while the phone that actually
    reminds their parent lay dead. The signal said all was well in exactly the
    case it exists to catch.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)

    profile = await session.get(Profile, uuid.UUID(pid))
    reminding = await session.get(Device, profile.owner_device_id)
    assert reminding is not None, "the fixture must have claimed authority"

    # The phone that reminds has been silent for two days.
    reminding.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=48)

    # A second device on the same account, syncing happily.
    session.add(
        Device(
            id=uuid.uuid4(),
            account_id=uuid.UUID(owner["account_id"]),
            platform="android",
            last_seen_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)

    kinds = {p["type"] for _, p in pusher.sent}
    assert "profile_stale" in kinds, "the reminding phone is silent and nobody was told"


async def test_two_watched_profiles_each_get_their_own_stale_alert(api, session, db_engine):
    """One caregiver, two parents, two alerts.

    The uniqueness key left out the profile, and for profile_stale the subject
    is only a date — so the first profile scanned took the key for the day and
    the second was deduplicated into silence. The more people a caregiver looks
    after, the more the key hid.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    owner_a, caregiver, pid_a, *_ = await _with_watcher(api, session)

    # The same caregiver takes on a second profile, from a different owner.
    owner_b, pid_b, mid_b, sid_b, did_b = await _owner_with_data(api)
    phone_b = await _owner_device(session, owner_b)
    await api.post(f"/v1/profiles/{pid_b}/reminder-authority",
                   headers=auth_header(owner_b), json={"device_id": str(phone_b.id)})
    code = (await api.post("/v1/pairing/codes", headers=auth_header(owner_b),
                           json={"profile_id": pid_b, "role": "with_alerts"})).json()["code"]
    await api.post("/v1/pairing/redeem", headers=auth_header(caregiver),
                   json={"code": code})

    # And the caregiver has a device to be told on.
    cg_device = (await session.execute(
        select(Device).where(Device.account_id == uuid.UUID(caregiver["account_id"]))
    )).scalars().first()
    cg_device.push_token = "token-" + caregiver["account_id"][:8]

    stale = datetime.now(timezone.utc) - timedelta(hours=48)
    session.expire_all()
    for pid in (pid_a, pid_b):
        profile = await session.get(Profile, uuid.UUID(pid))
        assert profile.owner_device_id is not None, f"{pid} never claimed authority"
        device = await session.get(Device, profile.owner_device_id)
        device.last_seen_at = stale
    await session.commit()

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)

    told_about = {p["profile_id"] for _, p in pusher.sent if p["type"] == "profile_stale"}
    assert told_about == {pid_a, pid_b}, f"only heard about {told_about}"

    # And the two do not replace each other on the phone.
    assert len(set(pusher.collapse)) == len(pusher.collapse)


async def test_an_alert_with_nobody_to_tell_waits_instead_of_spinning(api, session, db_engine):
    """No push token is not a failed attempt, but it must not be a free one.

    `next_attempt_at` was left alone, so the row was selected, locked and
    released on every pass for the whole of its TTL — several hundred times to
    discover the same absence. Nor may it burn attempts: those are for sends
    that were tried, and using them up here would exhaust the alert before a
    device ever appears.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)

    # The caregiver has signed in but has no device registered for push.
    cg_device = (await session.execute(
        select(Device).where(Device.account_id == uuid.UUID(caregiver["account_id"]))
    )).scalars().first()
    cg_device.push_token = None
    await session.commit()

    await push(api, owner, dose_events=[
        dose(did, mid, pid, sid, "missed", at=ms() + 1000, planned=ms() - 3600_000)
    ])

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    t0 = datetime.now(timezone.utc)
    assert await scan_once(maker, RecordingPush(), now=t0) == 0

    d = await _only_delivery(session)
    assert d.attempts == 0, "nothing was attempted, so nothing may be counted"
    assert d.next_attempt_at > t0, "it would be re-locked on every pass for its whole TTL"
    assert d.state == AlertState.pending.value

    # A pass a minute later leaves it alone entirely.
    later = t0 + timedelta(minutes=1)
    assert await scan_once(maker, RecordingPush(), now=later) == 0
    again = await _only_delivery(session)
    assert again.attempts == 0
    assert again.next_attempt_at == d.next_attempt_at


async def test_each_signal_keeps_its_own_ttl_at_fcm(api, session, db_engine):
    """FCM must not give up sooner than the server does.

    One flat six-hour TTL went to every message, so `profile_stale` — which the
    server keeps for a day — was dropped by FCM after six hours. The server had
    already recorded it sent and would never retry, so a phone switched off
    overnight simply never heard, and nothing anywhere recorded a loss.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    owner, caregiver, pid, mid, sid, did = await _with_watcher(api, session)
    await push(api, owner, dose_events=[
        dose(did, mid, pid, sid, "missed", at=ms() + 1000, planned=ms() - 3600_000)
    ])
    owner_device = await _owner_device(session, owner)
    owner_device.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=48)
    await session.commit()

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)

    ttl_of = {p["type"]: t for (_, p), t in zip(pusher.sent, pusher.ttl)}
    assert ttl_of["dose_missed"] == int(alerts.TTL[AlertKind.dose_missed].total_seconds())
    assert ttl_of["profile_stale"] == int(alerts.TTL[AlertKind.profile_stale].total_seconds())
    assert ttl_of["profile_stale"] != ttl_of["dose_missed"], "one figure for both is the bug"


# ---------------------------------------------------------------------------
# The authority nudge, which rides this queue without being an alert
# ---------------------------------------------------------------------------


async def _handover(api, session, db_engine):
    """One account, two handsets, authority moved from the first to the second."""
    from app.db.models import Device as DeviceModel

    subject = f"owner-{uuid.uuid4()}"
    owner = await sign_in(api, subject)
    await sign_in(api, subject)

    pid = str(uuid.uuid4())
    await push(api, owner, profiles=[{
        "id": pid, "created_at": ms(), "updated_at": ms(), "deleted_at": None,
        "op_seq": 90001, "name": "Mum", "color": 4283215696, "sort_order": 0,
    }])

    devices = (await session.execute(
        select(DeviceModel).where(DeviceModel.account_id == uuid.UUID(owner["account_id"]))
        .order_by(DeviceModel.created_at)
    )).scalars().all()
    losing, winning = devices[0], devices[1]
    losing.push_token = "token-losing"
    winning.push_token = "token-winning"
    await session.commit()

    for device in (losing, winning):
        r = await api.post(f"/v1/profiles/{pid}/reminder-authority",
                           headers=auth_header(owner), json={"device_id": str(device.id)})
        assert r.status_code == 200

    # The winner pulls, which is what releases the nudge. Done explicitly here
    # rather than hidden in a fixture: it is the precondition the whole gate is
    # about, and a test that got it for free would not notice if it vanished.
    await _winner_has_pulled(session, winning, pid)
    return owner, pid, losing, winning


async def _winner_has_pulled(session, winning, pid) -> None:
    """Mark the winning device as having pulled the handover.

    The real path is a `GET /sync/pull` on that device's own token; this writes
    the column that pull writes, because these tests are about what the worker
    does with the fact, not about how the fact is recorded. The recording itself
    is covered in test_sync.
    """
    from app.db.models import Device as DeviceModel

    profile = await session.get(Profile, uuid.UUID(pid))
    device = await session.get(DeviceModel, winning.id)
    device.cursor_seq = profile.server_seq
    await session.commit()


async def test_the_worker_sends_the_queued_authority_nudge(api, session, db_engine):
    """Everything the receiving device checks, delivered by the process that
    actually holds the FCM credential."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    owner, pid, losing, winning = await _handover(api, session, db_engine)

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)

    assert len(pusher.sent) == 1
    token, payload = pusher.sent[0]
    assert token == "token-losing", "the device that kept authority must not be told"
    assert payload["type"] == "reminder_authority_lost"
    assert payload["profile_id"] == pid
    assert payload["owner_device_id"] == str(winning.id)

    profile = await session.get(Profile, uuid.UUID(pid))
    assert payload["revision"] == str(profile.server_seq)
    assert payload["expires_at"].endswith("Z")

    # FCM's data is map<string,string>; saying so here keeps the sending side
    # honest about what the other end receives.
    assert all(isinstance(v, str) for v in payload.values()), payload

    # Level with the row's own deadline rather than a fresh hour per attempt.
    assert 3500 < pusher.ttl[0] <= 3600

    row = (await session.execute(
        select(AlertDelivery).where(AlertDelivery.kind == AlertKind.reminder_authority_lost)
    )).scalars().one()
    await session.refresh(row)
    assert row.state == AlertState.sent.value
    assert row.sent_at is not None, "and now there is a record of whether it went"


async def test_a_nudge_whose_authority_came_back_is_retired_not_sent(api, session, db_engine):
    """The objection that made queueing look worse than sending inline.

    A to B fails to send; B goes back to A; the retry tells A it is not the
    owner while A is exactly the owner, and A falls silent. Rebuilding the
    payload at send time removes the case at its root — by the time the retry
    runs, the device it addresses holds authority again and there is nothing
    left to say.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    owner, pid, losing, winning = await _handover(api, session, db_engine)

    # Authority goes back before the worker ever runs.
    r = await api.post(f"/v1/profiles/{pid}/reminder-authority",
                       headers=auth_header(owner), json={"device_id": str(losing.id)})
    assert r.status_code == 200

    # And the device it went back to pulls, which is what releases the nudge now
    # owed to the other one. Without this the gate holds that nudge — correctly,
    # and the assertion below would be measuring the gate rather than the thing
    # this test is about.
    await _winner_has_pulled(session, losing, pid)

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)

    rows = {r.device_id: r for r in (await session.execute(
        select(AlertDelivery).where(AlertDelivery.kind == AlertKind.reminder_authority_lost)
    )).scalars().all()}

    assert rows[losing.id].state == AlertState.expired.value, (
        "telling the current owner it lost authority is the failure this avoids"
    )
    assert [t for t, _ in pusher.sent] == ["token-winning"], (
        "only the device that actually lost it is told"
    )


async def test_a_losing_device_with_no_token_waits_instead_of_failing(api, session, db_engine):
    """Nothing was tried, so nothing should be counted against the attempts."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    owner, pid, losing, winning = await _handover(api, session, db_engine)
    losing.push_token = None
    await session.commit()

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)

    assert pusher.sent == []
    row = (await session.execute(
        select(AlertDelivery).where(AlertDelivery.kind == AlertKind.reminder_authority_lost)
    )).scalars().one()
    await session.refresh(row)
    assert row.state == AlertState.pending.value
    assert row.attempts == 0
    assert row.next_attempt_at > utcnow()


async def test_a_signed_out_device_is_not_chased(api, session, db_engine):
    """It arms nothing, so there is nothing to tell it to stop."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    owner, pid, losing, winning = await _handover(api, session, db_engine)
    losing.revoked_at = utcnow()
    await session.commit()

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)

    assert pusher.sent == []
    row = (await session.execute(
        select(AlertDelivery).where(AlertDelivery.kind == AlertKind.reminder_authority_lost)
    )).scalars().one()
    await session.refresh(row)
    assert row.state == AlertState.expired.value


async def test_the_nudge_and_the_pull_agree_on_the_revision(api, session, db_engine):
    """One number, both channels. The whole reason the revision exists.

    The device drops an authority push whose revision is not strictly newer than
    the one it last pulled. Asserting the two are *equal* is the point:
    asserting each is merely present would pass with two unrelated numbers,
    which is the defect.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from tests.test_sync import pull

    owner, pid, losing, winning = await _handover(api, session, db_engine)

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)
    _token, payload = pusher.sent[0]

    prof = [p for p in (await pull(api, owner))["changes"]["profiles"] if p["id"] == pid][0]
    assert prof["owner_device_id"] == str(winning.id)
    assert payload["revision"] == prof["revision"], (
        "the push and the pull must name the same revision, or the device's "
        "'strictly newer' check compares numbers from two different worlds"
    )


async def test_the_nudge_waits_until_the_new_owner_has_pulled(api, session, db_engine):
    """The whole of variant (b), and the reason it was chosen.

    Silencing the previous phone before the new one knows it took over leaves
    nobody ringing. Measured on 18.08: 2 min 36 s of silence, against 28 s of
    two phones ringing on the slower build. Invariant 1 puts reliability first,
    so a duplicate is the failure to prefer — the previous phone keeps ringing
    until the new one has actually pulled the handover.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import Device as DeviceModel

    owner, pid, losing, winning = await _handover(api, session, db_engine)

    # Undo what the helper arranged: the winner has not pulled after all.
    device = await session.get(DeviceModel, winning.id)
    device.cursor_seq = None
    await session.commit()

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)

    assert pusher.sent == [], "the losing phone must keep ringing until the winner is ready"
    row = (await session.execute(
        select(AlertDelivery).where(AlertDelivery.kind == AlertKind.reminder_authority_lost)
    )).scalars().one()
    await session.refresh(row)
    assert row.state == AlertState.pending.value
    assert row.attempts == 0, "nothing was tried, so nothing is spent"

    # And once it has pulled, the same scan lets it through.
    await _winner_has_pulled(session, winning, pid)
    row.next_attempt_at = utcnow() - timedelta(seconds=1)
    await session.commit()

    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)
    assert [t for t, _ in pusher.sent] == ["token-losing"]


async def test_a_stale_cursor_does_not_open_the_gate(api, session, db_engine):
    """Reaching the revision at the moment of handover is not enough.

    A profile written again since then sits at a higher sequence, and a pull
    that stopped short of it never carried the row naming the new owner. The
    gate compares against the profile as it stands, not against the number the
    nudge was queued with.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import Device as DeviceModel

    owner, pid, losing, winning = await _handover(api, session, db_engine)

    row = (await session.execute(
        select(AlertDelivery).where(AlertDelivery.kind == AlertKind.reminder_authority_lost)
    )).scalars().one()
    handover_revision = int(row.subject_id)

    # The profile moves on, and the winner's cursor stops at the old number.
    profile = await session.get(Profile, uuid.UUID(pid))
    profile.server_seq = handover_revision + 50
    device = await session.get(DeviceModel, winning.id)
    device.cursor_seq = handover_revision
    await session.commit()

    pusher = RecordingPush()
    await scan_once(async_sessionmaker(db_engine, expire_on_commit=False), pusher)
    assert pusher.sent == []
