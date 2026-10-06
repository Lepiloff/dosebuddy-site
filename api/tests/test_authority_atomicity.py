"""Authority transitions use current state through commit on real PostgreSQL."""

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import auth, pairing
from app.api.deps import Caller, get_session
from app.api.sync import decode_cursor, encode_cursor
from app.core.security import mint_access_token, read_access_token
from app.db.models import (
    Account, AlertDelivery, Device, DeviceReadiness, Profile, Schedule, utcnow,
)
from app.db.publication import NEXT_SERVER_SEQ, lock_publication
from app.services import alerts
from tests.conftest import auth_header
from tests.test_sync import _owner_with_data
from tests.test_sync_publication import _pid, _waiting

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def world(api, db_engine):
    owner, pid, mid, sid, did = await _owner_with_data(api)
    account_id = uuid.UUID(owner["account_id"])
    device_a = uuid.UUID(read_access_token(
        api.app.state.settings.jwt_secret, owner["access_token"]
    )["did"])
    device_b, device_c = uuid.uuid4(), uuid.uuid4()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        session.add_all([
            Device(id=dev, account_id=account_id, platform="android", last_seen_at=utcnow(),
                   push_token=f"synthetic-{dev}", ready_protocol_at=utcnow())
            for dev in (device_b, device_c)
        ])
        await session.execute(update(Device).where(Device.id == device_a)
                              .values(push_token="synthetic-a", last_seen_at=utcnow()))
        await session.execute(update(Profile).where(Profile.id == uuid.UUID(pid))
                              .values(owner_device_id=device_a))
        await session.commit()
    headers = {
        dev: auth_header({"access_token": mint_access_token(
            api.app.state.settings.jwt_secret, account_id, dev
        )[0]}) for dev in (device_a, device_b, device_c)
    }
    # Keep the authentication dependency and all production endpoint wiring;
    # only record each connection's PID so a database wait is observable.
    pids = {}

    async def tracked_session(request: Request):
        async with maker() as session:
            pids.setdefault(request.url.path, []).append(await _pid(session))
            yield session

    api.app.dependency_overrides[get_session] = tracked_session
    # Observe the production 500 envelope instead of re-raising in the test transport.
    transport = api._transport
    old_raise = transport.raise_app_exceptions
    transport.raise_app_exceptions = False
    yield SimpleNamespace(api=api, maker=maker, engine=db_engine, account_id=account_id,
                          pid=uuid.UUID(pid), sid=uuid.UUID(sid), a=device_a, b=device_b,
                          c=device_c, headers=headers, pids=pids)
    api.app.dependency_overrides.pop(get_session, None)
    transport.raise_app_exceptions = old_raise


async def _claim(w, device, reports_ready=True):
    return await w.api.post(f"/v1/profiles/{w.pid}/reminder-authority",
                            headers=w.headers[device],
                            json={"device_id": str(device), "reports_ready": reports_ready})


async def _body(w, device):
    page = await w.api.get("/v1/sync/pull?ready=1", headers=w.headers[device])
    assert page.status_code == 200, page.text
    async with w.maker() as session:
        revision = (await session.get(Profile, w.pid)).server_seq
    return {"profile_id": str(w.pid), "revision": str(revision),
            "applied_cursor": page.json()["cursor"]}


async def _ready(w, device, body):
    return await w.api.post(f"/v1/devices/{device}/ready", headers=w.headers[device], json=body)


async def _state(w):
    async with w.maker() as session:
        profile = await session.get(Profile, w.pid)
        queued = (await session.execute(select(AlertDelivery))).scalars().all()
        ready = await session.get(DeviceReadiness, (w.b, w.pid))
        return profile, ready, queued


async def _wait_request(w, task, path, index=0):
    for _ in range(200):
        if len(w.pids.get(path, [])) > index:
            return await _waiting(task, w.engine, w.pids[path][index])
        if task.done():
            task.result()
            return False
        await asyncio.sleep(0.01)
    return False


def _pause_high_water(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = alerts.profile_high_water
    first = True

    async def paused(session, profile):
        nonlocal first
        result = await original(session, profile)
        if first:
            first = False
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(alerts, "profile_high_water", paused)
    return entered, release


async def test_ready_first_serializes_a_later_claim_and_preserves_it(world, monkeypatch):
    w = world
    assert (await _claim(w, w.b)).status_code == 200
    body = await _body(w, w.b)
    entered, release = _pause_high_water(monkeypatch)
    ready = asyncio.create_task(_ready(w, w.b, body))
    await entered.wait()
    later_claim = asyncio.create_task(_claim(w, w.c))
    try:
        assert await _wait_request(w, later_claim, f"/v1/profiles/{w.pid}/reminder-authority", 1), (
            "claim committed while an earlier ready had already checked authority/high-water"
        )
    finally:
        release.set()
        responses = await asyncio.gather(ready, later_claim)
    assert [r.status_code for r in responses] == [204, 200]
    profile, readiness, queued = await _state(w)
    assert (profile.owner_device_id, profile.pending_owner_device_id) == (w.b, w.c)
    assert responses[1].json()["revision"] == profile.server_seq
    assert isinstance(responses[1].json()["revision"], int)
    assert readiness.applied_cursor == decode_cursor(body["applied_cursor"])
    assert readiness.applied_cursor < profile.server_seq
    assert len(queued) == 1 and queued[0].device_id == w.a
    assert queued[0].subject_id == str(profile.server_seq)


async def test_two_inflight_ready_reports_recheck_the_revision(world, monkeypatch):
    w = world
    await _claim(w, w.b)
    body = await _body(w, w.b)
    entered, release = _pause_high_water(monkeypatch)
    first = asyncio.create_task(_ready(w, w.b, body))
    await entered.wait()
    second = asyncio.create_task(_ready(w, w.b, body))
    try:
        assert await _wait_request(w, second, f"/v1/devices/{w.b}/ready", 1)
    finally:
        release.set()
        responses = await asyncio.gather(first, second)
    assert [r.status_code for r in responses] == [204, 409]
    assert responses[1].json()["error"]["code"] == "stale_revision"
    profile, ready, queued = await _state(w)
    assert profile.owner_device_id == w.b and profile.pending_owner_device_id is None
    assert ready.revision == profile.server_seq
    assert len(queued) == 1


async def test_committed_new_claim_refuses_an_old_ready_even_with_cached_profile(world):
    w = world
    await _claim(w, w.b)
    body = await _body(w, w.b)
    async with w.maker() as stale:
        account = await stale.get(Account, w.account_id)
        cached = await stale.get(Profile, w.pid)
        assert cached.pending_owner_device_id == w.b
        assert (await _claim(w, w.c)).status_code == 200
        with pytest.raises(HTTPException) as refused:
            await pairing.report_ready(w.b, pairing.ReadyIn(**body), Caller(account, w.b), stale)
        assert refused.value.status_code == 409
        assert refused.value.detail == "claim_superseded"
        await stale.rollback()
    response = await _ready(w, w.b, body)
    assert response.status_code == 409 and response.json()["error"]["code"] == "claim_superseded"
    profile, ready, queued = await _state(w)
    assert (profile.owner_device_id, profile.pending_owner_device_id) == (w.a, w.c)
    assert ready is None and queued == []


@pytest.mark.parametrize("cached", [False, True])
async def test_claim_uses_current_owner_and_revision_after_waiting(world, cached):
    w = world
    async with w.maker() as claimant:
        account = await claimant.get(Account, w.account_id)
        retained = await claimant.get(Profile, w.pid) if cached else None
        await _claim(w, w.b, reports_ready=False)
        response = await pairing.set_reminder_authority(
            w.pid, pairing.ReminderAuthorityIn(device_id=w.c, reports_ready=True),
            Request({"type": "http"}), Caller(account, w.c), claimant,
        )
        assert response.owner_device_id == w.b
        async with w.maker() as verify:
            profile = await verify.get(Profile, w.pid)
            assert response.revision == profile.server_seq
            assert profile.pending_owner_device_id == w.c
        if retained is not None:
            assert retained.owner_device_id == w.b


@pytest.mark.parametrize("mutation", ["update", "delete"])
async def test_child_write_cannot_cross_the_ready_high_water_check(world, monkeypatch, mutation):
    w = world
    await _claim(w, w.b)
    body = await _body(w, w.b)
    entered, release = _pause_high_water(monkeypatch)
    ready = asyncio.create_task(_ready(w, w.b, body))
    await entered.wait()
    async with w.maker() as writer:
        writer_pid = await _pid(writer)

        async def child_write():
            values = {"server_seq": text(NEXT_SERVER_SEQ), "updated_at_ms": 9_000_000_000_000}
            values.update({"deleted_at_ms": 9_000_000_000_000} if mutation == "delete"
                          else {"times": '["11:00"]'})
            await writer.execute(update(Schedule).where(Schedule.id == w.sid).values(**values))
            await writer.commit()

        child = asyncio.create_task(child_write())
        try:
            assert await _waiting(child, w.engine, writer_pid), "child changed after readiness check"
        finally:
            release.set()
            response, _ = await asyncio.gather(ready, child)
    assert response.status_code == 204
    profile, readiness, queued = await _state(w)
    assert readiness.applied_cursor == decode_cursor(body["applied_cursor"])
    async with w.maker() as verify:
        delivery = await verify.get(AlertDelivery, queued[0].id)
        assert await alerts.resolve_nudge(verify, delivery, utcnow()) == alerts.NoNudge.awaiting_winner
    assert (await _ready(w, w.b, {**body, "revision": str(profile.server_seq)})).status_code == 409
    assert (await _ready(w, w.b, await _body(w, w.b))).status_code == 204


async def test_child_committed_before_ready_is_rejected(world):
    w = world
    await _claim(w, w.b)
    body = await _body(w, w.b)
    async with w.maker() as writer:
        await writer.execute(update(Schedule).where(Schedule.id == w.sid)
                             .values(server_seq=text(NEXT_SERVER_SEQ), times='["13:00"]'))
        await writer.commit()
    response = await _ready(w, w.b, body)
    assert response.status_code == 409 and response.json()["error"]["code"] == "cursor_behind_revision"
    profile, ready, queued = await _state(w)
    assert (profile.owner_device_id, profile.pending_owner_device_id) == (w.a, w.b)
    assert ready is None and queued == []


@pytest.mark.parametrize("operation", ["ready", "claim"])
@pytest.mark.parametrize("lifecycle", ["logout", "delete_account"])
async def test_authenticated_request_rechecks_eligibility_after_lifecycle_commit(
    world, operation, lifecycle,
):
    w = world
    await _claim(w, w.b)
    body = await _body(w, w.b)
    async with w.maker() as lifecycle_session:
        account = await lifecycle_session.get(Account, w.account_id)
        await lock_publication(lifecycle_session)
        request = asyncio.create_task(_ready(w, w.b, body) if operation == "ready"
                                      else _claim(w, w.b))
        path = (f"/v1/devices/{w.b}/ready" if operation == "ready"
                else f"/v1/profiles/{w.pid}/reminder-authority")
        try:
            assert await _wait_request(w, request, path, 0 if operation == "ready" else 1)
            if lifecycle == "logout":
                await auth.logout(auth.LogoutIn(reminders_stopped=True),
                                  Caller(account, w.b), lifecycle_session)
            else:
                await auth._erase_account(lifecycle_session, w.account_id)
        finally:
            await lifecycle_session.rollback()
            response = await request
    assert response.status_code == 401, response.text
    async with w.maker() as verify:
        assert (await verify.execute(select(DeviceReadiness))).scalars().all() == []
        assert (await verify.execute(select(AlertDelivery))).scalars().all() == []
        profile = await verify.get(Profile, w.pid)
        if lifecycle == "logout":
            assert profile.owner_device_id == w.a and profile.pending_owner_device_id is None
            assert (await verify.get(Device, w.b)).revoked_at is not None
        else:
            assert profile is None and await verify.get(Account, w.account_id) is None


@pytest.mark.parametrize("operation", ["ready", "claim"])
async def test_stale_cached_device_cannot_become_authority_after_logout(world, operation):
    w = world
    await _claim(w, w.b)
    body = await _body(w, w.b)
    async with w.maker() as stale:
        account = await stale.get(Account, w.account_id)
        retained = await stale.get(Device, w.b)
        profile = await stale.get(Profile, w.pid)
        response = await w.api.post("/v1/auth/logout", headers=w.headers[w.b],
                                    json={"reminders_stopped": True})
        assert response.status_code == 204
        with pytest.raises(HTTPException) as refused:
            if operation == "ready":
                await pairing.report_ready(w.b, pairing.ReadyIn(**body), Caller(account, w.b), stale)
            else:
                await pairing.set_reminder_authority(
                    w.pid, pairing.ReminderAuthorityIn(device_id=w.b, reports_ready=True),
                    Request({"type": "http"}), Caller(account, w.b), stale,
                )
        assert refused.value.status_code == 401
        assert retained.revoked_at is not None
        await stale.rollback()
    profile, ready, queued = await _state(w)
    assert (profile.owner_device_id, profile.pending_owner_device_id) == (w.a, None)
    assert ready is None and queued == []


async def _lease(w):
    now = utcnow()
    async with w.maker() as seed:
        seq = (await seed.execute(update(Profile).where(Profile.id == w.pid).values(
            owner_device_id=w.b, previous_owner_device_id=w.a, pending_owner_device_id=None,
            authority_leased=True, server_seq=text(NEXT_SERVER_SEQ),
        ).returning(Profile.server_seq))).scalar_one()
        seed.add(DeviceReadiness(device_id=w.b, profile_id=w.pid, revision=seq,
                                 applied_cursor=seq, updated_at=now - timedelta(hours=2)))
        await seed.commit()
    return now


@pytest.mark.parametrize("intervening", ["renew", "claim", "logout_previous", "delete_account"])
@pytest.mark.parametrize("cached", [False, True])
async def test_worker_reselects_leased_authority_after_a_wait(world, intervening, cached):
    w = world
    now = await _lease(w)
    body = await _body(w, w.b)
    async with w.maker() as blocker, w.maker() as worker:
        account = await blocker.get(Account, w.account_id)
        retained = []
        if cached:
            retained = [await worker.get(Profile, w.pid), await worker.get(Device, w.a),
                        await worker.get(DeviceReadiness, (w.b, w.pid))]
        worker_pid = await _pid(worker)
        await lock_publication(blocker)
        taking_back = asyncio.create_task(alerts.hand_back_unconfirmed(worker, now, timedelta(hours=1)))
        try:
            assert await _waiting(taking_back, w.engine, worker_pid)
            if intervening == "renew":
                await pairing.report_ready(w.b, pairing.ReadyIn(**body), Caller(account, w.b), blocker)
            elif intervening == "claim":
                await pairing.set_reminder_authority(
                    w.pid, pairing.ReminderAuthorityIn(device_id=w.c, reports_ready=True),
                    Request({"type": "http"}), Caller(account, w.c), blocker,
                )
            elif intervening == "logout_previous":
                await auth.logout(auth.LogoutIn(reminders_stopped=True), Caller(account, w.a), blocker)
            else:
                await auth._erase_account(blocker, w.account_id)
        finally:
            await blocker.rollback()
            handed = await taking_back
            await worker.rollback()
        assert handed == 0, "worker acted on authority/readiness/eligibility read before the lock"
    profile, ready, queued = await _state(w)
    if intervening == "delete_account":
        assert profile is None and ready is None
    else:
        assert profile.owner_device_id == w.b
        assert profile.pending_owner_device_id == (w.c if intervening == "claim" else None)
    assert queued == []


async def test_ready_renewal_first_prevents_lease_handback(world, monkeypatch):
    w = world
    now = await _lease(w)
    body = await _body(w, w.b)
    entered, release = _pause_high_water(monkeypatch)
    renewing = asyncio.create_task(_ready(w, w.b, body))
    await entered.wait()
    async with w.maker() as worker:
        worker_pid = await _pid(worker)
        handback = asyncio.create_task(alerts.hand_back_unconfirmed(worker, now, timedelta(hours=1)))
        try:
            assert await _waiting(handback, w.engine, worker_pid)
        finally:
            release.set()
            response, handed = await asyncio.gather(renewing, handback)
            await worker.rollback()
    assert response.status_code == 204 and handed == 0
    profile, ready, queued = await _state(w)
    assert profile.owner_device_id == w.b and profile.authority_leased
    assert ready.updated_at >= now and queued == []


async def test_lease_handback_first_refuses_late_renewal(world):
    w = world
    now = await _lease(w)
    body = await _body(w, w.b)
    async with w.maker() as worker:
        assert await alerts.hand_back_unconfirmed(worker, now, timedelta(hours=1)) == 1
    response = await _ready(w, w.b, body)
    assert response.status_code == 409 and response.json()["error"]["code"] == "not_owner"
    profile, ready, queued = await _state(w)
    assert profile.owner_device_id == w.a and not profile.authority_leased
    assert queued == []


async def test_ready_timeout_then_superseding_claim_refuses_retry(world):
    w = world
    await _claim(w, w.b)
    body = await _body(w, w.b)
    async with w.maker() as blocker:
        account = await blocker.get(Account, w.account_id)
        await lock_publication(blocker)
        timing_out = asyncio.create_task(_ready(w, w.b, body))
        assert await _wait_request(w, timing_out, f"/v1/devices/{w.b}/ready")
        response = await asyncio.wait_for(timing_out, 7)
        assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
        await pairing.set_reminder_authority(
            w.pid, pairing.ReminderAuthorityIn(device_id=w.c, reports_ready=True),
            Request({"type": "http"}), Caller(account, w.c), blocker,
        )
    retry = await _ready(w, w.b, body)
    assert retry.status_code == 409 and retry.json()["error"]["code"] == "claim_superseded"
    profile, ready, queued = await _state(w)
    assert (profile.owner_device_id, profile.pending_owner_device_id) == (w.a, w.c)
    assert ready is None and queued == []


async def test_client_timeout_leaves_server_ready_ordered_before_the_next_claim(world, monkeypatch):
    w = world
    await _claim(w, w.b)
    body = await _body(w, w.b)
    entered, release = _pause_high_water(monkeypatch)
    server_ready = asyncio.create_task(_ready(w, w.b, body))
    await entered.wait()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(server_ready), 0.01)
    claim = asyncio.create_task(_claim(w, w.c))
    try:
        assert await _wait_request(w, claim, f"/v1/profiles/{w.pid}/reminder-authority", 1)
    finally:
        release.set()
        accepted, claimed = await asyncio.gather(server_ready, claim)
    assert accepted.status_code == 204 and claimed.status_code == 200
    retry = await _ready(w, w.b, body)
    assert retry.status_code == 409 and retry.json()["error"]["code"] == "stale_revision"
    profile, ready, queued = await _state(w)
    assert (profile.owner_device_id, profile.pending_owner_device_id) == (w.b, w.c)
    assert len(queued) == 1


@pytest.mark.parametrize("lifecycle", ["logout", "delete_account"])
async def test_ready_first_then_lifecycle_clears_the_committed_transition(world, monkeypatch, lifecycle):
    w = world
    await _claim(w, w.b)
    body = await _body(w, w.b)
    entered, release = _pause_high_water(monkeypatch)
    ready = asyncio.create_task(_ready(w, w.b, body))
    await entered.wait()
    if lifecycle == "logout":
        leaving = asyncio.create_task(w.api.post("/v1/auth/logout", headers=w.headers[w.b],
                                                json={"reminders_stopped": True}))
        path = "/v1/auth/logout"
    else:
        leaving = asyncio.create_task(w.api.delete("/v1/account", headers=w.headers[w.b]))
        path = "/v1/account"
    try:
        assert await _wait_request(w, leaving, path)
    finally:
        release.set()
        responses = await asyncio.gather(ready, leaving)
    assert [r.status_code for r in responses] == [204, 204]
    profile, readiness, queued = await _state(w)
    if lifecycle == "logout":
        assert profile.owner_device_id is None and profile.pending_owner_device_id is None
        assert not profile.authority_leased
        async with w.maker() as verify:
            delivery = await verify.get(AlertDelivery, queued[0].id)
            assert await alerts.resolve_nudge(verify, delivery, utcnow()) == alerts.NoNudge.moot
    else:
        assert profile is None and readiness is None and queued == []


async def test_claim_rechecks_a_cached_target_device_after_its_logout(world):
    w = world
    async with w.maker() as stale:
        account = await stale.get(Account, w.account_id)
        target = await stale.get(Device, w.b)
        assert target.revoked_at is None
        response = await w.api.post("/v1/auth/logout", headers=w.headers[w.b],
                                    json={"reminders_stopped": True})
        assert response.status_code == 204
        with pytest.raises(HTTPException) as refused:
            await pairing.set_reminder_authority(
                w.pid, pairing.ReminderAuthorityIn(device_id=w.b, reports_ready=True),
                Request({"type": "http"}), Caller(account, w.a), stale,
            )
        assert refused.value.status_code == 404 and refused.value.detail == "device_not_found"
        assert target.revoked_at is not None
        await stale.rollback()
    profile, ready, queued = await _state(w)
    assert profile.owner_device_id == w.a and profile.pending_owner_device_id is None
    assert ready is None and queued == []


async def test_ready_rechecks_a_cached_account_after_erasure(world):
    w = world
    await _claim(w, w.b)
    body = await _body(w, w.b)
    async with w.maker() as stale:
        account = await stale.get(Account, w.account_id)
        retained = [await stale.get(Profile, w.pid), await stale.get(Device, w.b)]
        assert (await w.api.delete("/v1/account", headers=w.headers[w.b])).status_code == 204
        with pytest.raises(HTTPException) as refused:
            await pairing.report_ready(w.b, pairing.ReadyIn(**body), Caller(account, w.b), stale)
        assert refused.value.status_code == 401
        await stale.rollback()
    assert (await _state(w)) == (None, None, [])


@pytest.mark.parametrize("operation", ["ready", "legacy_claim"])
async def test_failed_nudge_queue_rolls_back_the_entire_authority_transition(world, monkeypatch, operation):
    w = world
    await _claim(w, w.b)
    body = await _body(w, w.b)

    async def fail_queue(*args, **kwargs):
        raise RuntimeError("synthetic queue failure")

    monkeypatch.setattr(alerts, "claim", fail_queue)
    response = (await _ready(w, w.b, body) if operation == "ready"
                else await _claim(w, w.c, reports_ready=False))
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
    profile, ready, queued = await _state(w)
    assert (profile.owner_device_id, profile.pending_owner_device_id) == (w.a, w.b)
    assert profile.server_seq == int(body["revision"])
    assert ready is None and queued == []


async def test_an_empty_lease_pass_releases_publication_before_delivery(world):
    w = world
    async with w.maker() as worker, w.maker() as writer:
        assert await alerts.hand_back_unconfirmed(worker, utcnow(), timedelta(hours=1)) == 0
        writer_pid = await _pid(writer)

        async def writing():
            await writer.execute(update(Schedule).where(Schedule.id == w.sid)
                                 .values(server_seq=text(NEXT_SERVER_SEQ)))
            await writer.commit()

        task = asyncio.create_task(writing())
        try:
            assert not await _waiting(task, w.engine, writer_pid), "empty worker pass retained publication"
        finally:
            await worker.rollback()
            await task


async def test_a_watcher_cannot_claim_or_report_owner_readiness(world):
    from tests.conftest import sign_in
    w = world
    observer = await sign_in(w.api, f"observer-{uuid.uuid4()}")
    observer_device = uuid.UUID(read_access_token(w.api.app.state.settings.jwt_secret,
                                                observer["access_token"])["did"])
    issued = await w.api.post("/v1/pairing/codes", headers=w.headers[w.a],
                              json={"profile_id": str(w.pid), "role": "viewer"})
    redeemed = await w.api.post("/v1/pairing/redeem", headers=auth_header(observer),
                                json={"code": issued.json()["code"]})
    assert redeemed.status_code == 200
    claim = await w.api.post(f"/v1/profiles/{w.pid}/reminder-authority", headers=auth_header(observer),
                             json={"device_id": str(observer_device), "reports_ready": True})
    body = await _body(w, w.a)
    ready = await w.api.post(f"/v1/devices/{observer_device}/ready", headers=auth_header(observer), json=body)
    assert claim.status_code == 404 and ready.status_code == 404
    profile, readiness, queued = await _state(w)
    assert profile.owner_device_id == w.a and profile.pending_owner_device_id is None
    assert readiness is None and queued == []


async def test_same_owner_claim_releases_publication_with_its_session_still_open(world):
    w = world
    async with w.maker() as claiming, w.maker() as writer:
        account = await claiming.get(Account, w.account_id)
        receipt = await pairing.set_reminder_authority(
            w.pid, pairing.ReminderAuthorityIn(device_id=w.a, reports_ready=True),
            Request({"type": "http"}), Caller(account, w.a), claiming,
        )
        assert receipt.owner_device_id == w.a and receipt.pending_device_id is None
        writer_pid = await _pid(writer)

        async def writing():
            await writer.execute(update(Schedule).where(Schedule.id == w.sid)
                                 .values(server_seq=text(NEXT_SERVER_SEQ)))
            await writer.commit()

        task = asyncio.create_task(writing())
        try:
            assert not await _waiting(task, w.engine, writer_pid), "no-op claim retained publication"
        finally:
            await claiming.rollback()
            await task
    profile, ready, queued = await _state(w)
    assert profile.server_seq == receipt.revision and profile.owner_device_id == w.a
    assert ready is None and queued == []


async def test_configured_empty_lease_scan_does_not_hold_publication_across_fcm(world):
    from app.db.models import AlertKind
    from app.services.push import Delivery
    from app.worker import scan_once
    w = world
    now = utcnow()
    async with w.maker() as seed:
        seed.add(AlertDelivery(account_id=w.account_id, profile_id=w.pid,
                               kind=AlertKind.dose_missed, subject_id=str(uuid.uuid4()),
                               state="pending", attempts=0, next_attempt_at=now,
                               expires_at=now + timedelta(hours=1)))
        await seed.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedPush:
        async def send(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return Delivery.ok

    sending = asyncio.create_task(scan_once(w.maker, PausedPush(), now, timedelta(hours=1)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        page = await asyncio.wait_for(w.api.get("/v1/sync/pull", headers=w.headers[w.a]), 2)
        assert page.status_code == 200, page.text
    finally:
        release.set()
        assert await asyncio.wait_for(sending, 5) == 1
