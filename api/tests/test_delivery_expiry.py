"""Absolute delivery deadlines through the real worker and FCM HTTP boundary.

SQL and dispatch are production code. OAuth and HTTP only consume a controlled
clock: no Google credentials, sockets, Firebase sends or device observations.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import worker
from app.db.models import AlertDelivery, AlertKind, AlertState, Device
from app.services import alerts, push as push_service
from app.services.push import Delivery, FcmPush
from tests.test_alert_eligibility import _pending
from tests.test_alerts import RecordingPush, _handover, _with_watcher

pytestmark = pytest.mark.asyncio


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    # Patch the production defaults, so these regressions also run on the old
    # sender without relying on a newly introduced argument to fail first.
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda *_: value()))
    monkeypatch.setattr(alerts, "utcnow", value, raising=False)
    monkeypatch.setattr(push_service, "utcnow", value, raising=False)
    return value


@pytest_asyncio.fixture
async def fcm(monkeypatch, clock):
    senders = []

    def create(*, oauth_wait=0, client_wait=0, http_wait=0, status=200):
        requests, refreshes = [], []

        class Credentials:
            token = "synthetic-oauth-token"

            def refresh(self, request):
                refreshes.append(clock())
                clock.advance(oauth_wait)

        monkeypatch.setattr(
            push_service.service_account.Credentials, "from_service_account_file",
            lambda *args, **kwargs: Credentials(),
        )

        class Client:
            def __init__(self, **kwargs):
                clock.advance(client_wait)

            async def aclose(self):
                pass

            async def post(self, url, *, headers, json):
                requests.append((clock(), json["message"]))
                await asyncio.sleep(0)
                clock.advance(http_wait)
                return SimpleNamespace(status_code=status)

        monkeypatch.setattr(push_service.httpx, "AsyncClient", Client)
        sender = FcmPush("synthetic-project", "/unused/synthetic-key.json")
        senders.append(sender)
        return sender, requests, refreshes

    yield create
    for sender in senders:
        await sender.aclose()


async def queued(session, caregiver, pid, clock, *, remaining, kind=AlertKind.dose_missed):
    _, delivery_id, _ = await _pending(session, caregiver, pid, kind, now=clock())
    await session.execute(update(AlertDelivery).where(AlertDelivery.id == delivery_id)
                          .values(expires_at=clock() + timedelta(seconds=remaining)))
    await session.commit()
    return delivery_id


async def row(maker, delivery_id):
    async with maker() as session:
        return await session.get(AlertDelivery, delivery_id)


def transient_delivery(clock, *, remaining, kind=AlertKind.dose_missed):
    return AlertDelivery(
        account_id=uuid.uuid4(), profile_id=uuid.uuid4(), kind=kind,
        subject_id="synthetic-event", expires_at=clock() + timedelta(seconds=remaining),
    )


async def test_worker_rechecks_expiry_between_rows(api, session, db_engine, clock):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    first = await queued(session, caregiver, pid, clock, remaining=3.5)
    second = await queued(session, caregiver, pid, clock, remaining=3.5)
    # Make ordering explicit; the second is still due at scan entry.
    await session.execute(update(AlertDelivery).where(AlertDelivery.id == first)
                          .values(next_attempt_at=clock() - timedelta(seconds=1)))
    await session.commit()

    class SlowPush(RecordingPush):
        async def send(self, *args, **kwargs):
            outcome = await super().send(*args)
            await asyncio.sleep(0)
            clock.advance(4)
            return outcome

    sender = SlowPush()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    assert await worker.scan_once(maker, sender) == 1
    assert len(sender.sent) == 1, "a previous row consumed this row's remaining life"
    assert (await row(maker, first)).state == AlertState.sent.value
    assert (await row(maker, second)).state == AlertState.expired.value


@pytest.mark.parametrize("status", [200, 503, 404])
async def test_worker_stops_tokens_at_expiry_and_keeps_best_outcome(
    api, session, db_engine, clock, fcm, status,
):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    session.add(Device(id=uuid.uuid4(), account_id=uuid.UUID(caregiver["account_id"]),
                       platform="android", push_token="second-token"))
    await session.commit()
    delivery_id = await queued(session, caregiver, pid, clock, remaining=3.5)
    sender, requests, _ = fcm(http_wait=4, status=status)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    assert await worker.scan_once(maker, sender) == (1 if status == 200 else 0)
    assert len(requests) == 1, "no new token may start after the absolute deadline"
    persisted = await row(maker, delivery_id)
    assert persisted.state == (AlertState.sent.value if status == 200 else AlertState.expired.value)
    assert (persisted.sent_at is not None) == (status == 200)
    assert requests[0][1]["android"]["ttl"] == "3s"


@pytest.mark.parametrize("kind", [AlertKind.dose_missed, AlertKind.profile_stale])
async def test_worker_retry_uses_remaining_life_not_a_fresh_window(
    api, session, db_engine, clock, fcm, kind,
):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    delivery_id = await queued(session, caregiver, pid, clock, remaining=65.5, kind=kind)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    sender, requests, _ = fcm(status=503)
    assert await worker.scan_once(maker, sender) == 0
    initial = await row(maker, delivery_id)
    assert initial.state == AlertState.pending.value
    assert initial.expires_at == clock() + timedelta(seconds=65.5)
    clock.advance(61)
    sender, retry_requests, _ = fcm(oauth_wait=1)
    assert await worker.scan_once(maker, sender) == 1
    assert requests[0][1]["android"]["ttl"] == "65s"
    assert retry_requests[0][1]["android"]["ttl"] == "3s"
    assert (await row(maker, delivery_id)).expires_at == initial.expires_at


@pytest.mark.parametrize("remaining", [-1, 0, 0.999])
async def test_direct_dispatch_retires_zero_or_subsecond_life(clock, remaining):
    sender = RecordingPush()
    result = await alerts.deliver(sender, transient_delivery(clock, remaining=remaining), ("token",))
    assert sender.sent == [], "ttl=0 is immediate delivery in FCM, not an expired message"
    assert result.value == "expired", "expiry must remain distinct from an unregistered token"


@pytest.mark.parametrize("remaining", [-1, 0, 0.999])
async def test_worker_retires_zero_or_subsecond_life(api, session, db_engine, clock, remaining):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    delivery_id = await queued(session, caregiver, pid, clock, remaining=remaining)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    sender = RecordingPush()
    assert await worker.scan_once(maker, sender) == 0
    assert sender.sent == []
    persisted = await row(maker, delivery_id)
    assert persisted.state == AlertState.expired.value
    assert persisted.sent_at is None and persisted.attempts == 0


@pytest.mark.parametrize("oauth_wait,client_wait", [(6, 0), (2, 4)])
async def test_fcm_rechecks_deadline_after_oauth_and_client_wait(
    clock, fcm, oauth_wait, client_wait,
):
    sender, requests, _ = fcm(oauth_wait=oauth_wait, client_wait=client_wait)
    outcome = await sender.send("token", {"type": "dose_missed"}, "collapse", 5)
    assert requests == [], "OAuth/client setup exhausted the original send budget"
    assert outcome.value == "expired"


async def test_fcm_reduces_ttl_after_oauth_and_client_wait(clock, fcm):
    sender, requests, _ = fcm(oauth_wait=2, client_wait=1)
    assert await sender.send("token", {"type": "dose_missed"}, "collapse", 7) is Delivery.ok
    assert requests[0][1]["android"]["ttl"] == "4s"


@pytest.mark.parametrize("ttl", [0, -1])
async def test_fcm_zero_life_never_refreshes_credentials(clock, fcm, ttl):
    sender, requests, refreshes = fcm()
    outcome = await sender.send("token", {"type": "dose_missed"}, "collapse", ttl)
    assert refreshes == [] and requests == []
    assert outcome.value == "expired"


async def test_worker_checks_expiry_after_eligibility_wait(api, session, db_engine, clock, monkeypatch):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    delivery_id = await queued(session, caregiver, pid, clock, remaining=3)
    original = alerts.caregiver_eligible

    async def slow_eligibility(*args, **kwargs):
        eligible = await original(*args, **kwargs)
        clock.advance(4)
        return eligible

    monkeypatch.setattr(alerts, "caregiver_eligible", slow_eligibility)
    sender = RecordingPush()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    assert await worker.scan_once(maker, sender) == 0
    assert sender.sent == []
    assert (await row(maker, delivery_id)).state == AlertState.expired.value


@pytest.mark.parametrize("oauth_wait,expected", [(2, "3s"), (6, None)])
async def test_authority_nudge_uses_same_fcm_deadline(
    api, session, db_engine, clock, fcm, oauth_wait, expected,
):
    await _handover(api, session, db_engine)
    delivery_id = (await session.execute(select(AlertDelivery.id))).scalar_one()
    deadline = clock() + timedelta(seconds=5.5)
    await session.execute(update(AlertDelivery).where(AlertDelivery.id == delivery_id)
                          .values(next_attempt_at=clock(), expires_at=deadline))
    await session.commit()
    sender, requests, _ = fcm(oauth_wait=oauth_wait)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    await worker.scan_once(maker, sender)
    persisted = await row(maker, delivery_id)
    if expected is None:
        assert requests == []
        assert persisted.state == AlertState.expired.value
    else:
        assert requests[0][1]["android"]["ttl"] == expected
        assert requests[0][1]["data"]["expires_at"] == deadline.isoformat().replace("+00:00", "Z")
        assert persisted.state == AlertState.sent.value


@pytest.mark.parametrize("first_outcome", [Delivery.ok, Delivery.retry, Delivery.gone])
async def test_dispatch_checks_each_token_before_start(clock, first_outcome):
    class SlowPush(RecordingPush):
        async def send(self, *args, **kwargs):
            await super().send(*args)
            await asyncio.sleep(0)
            clock.advance(4)
            return first_outcome

    sender = SlowPush()
    outcome = await alerts.deliver(sender, transient_delivery(clock, remaining=3.5),
                                   ("first-token", "second-token"))
    assert len(sender.sent) == 1
    assert sender.ttl == [3]
    assert outcome.value == ("ok" if first_outcome is Delivery.ok else "expired")


async def test_dispatch_reduces_remaining_ttl_for_each_token(clock):
    class SlowPush(RecordingPush):
        async def send(self, *args, **kwargs):
            result = await super().send(*args)
            clock.advance(2)
            return result

    sender = SlowPush()
    assert await alerts.deliver(sender, transient_delivery(clock, remaining=5.5),
                                ("first-token", "second-token")) is Delivery.ok
    assert sender.ttl == [5, 3]


async def test_explicit_clock_moves_with_a_frozen_detection_snapshot(api, session, db_engine, clock):
    _, caregiver, pid, *_ = await _with_watcher(api, session)
    delivery_id = await queued(session, caregiver, pid, clock, remaining=3.5)
    snapshot = clock()
    clock.advance(4)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    sender = RecordingPush()
    assert await worker.scan_once(maker, sender, now=snapshot, clock=clock) == 0
    assert sender.sent == []
    assert (await row(maker, delivery_id)).state == AlertState.expired.value
