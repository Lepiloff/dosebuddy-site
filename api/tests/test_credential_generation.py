"""Durable credential revocation, through real HTTP and PostgreSQL paths."""

import uuid
from datetime import datetime, timezone

import pytest

from app.core import security
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


class FrozenDatetime(datetime):
    instant = datetime.now(timezone.utc).replace(microsecond=0)

    @classmethod
    def now(cls, tz=None):
        return cls.instant


async def login(api, subject, device_id):
    token = f"synthetic-{subject}"
    api.app.state.google_verifier.add(token, subject)
    response = await api.post("/v1/auth/google", json={
        "id_token": token, "device": {"id": str(device_id), "platform": "android"},
    })
    assert response.status_code == 200, response.text
    return response.json()


async def sync_token(api, device_id, pair):
    response = await api.post(f"/v1/devices/{device_id}/sync-token", headers=auth_header(pair))
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"sync_token", "expires_in"}
    return {"access_token": response.json()["sync_token"]}


@pytest.mark.parametrize("credential", ["access", "sync"])
async def test_logout_relogin_does_not_revive_old_bearer_in_same_second(api, monkeypatch, credential):
    monkeypatch.setattr(security, "datetime", FrozenDatetime)
    device_id = uuid.uuid4()
    old = await login(api, "same-second", device_id)
    bearer = old if credential == "access" else await sync_token(api, device_id, old)
    assert (await api.get("/v1/sync/pull", headers=auth_header(bearer))).status_code == 200
    assert (await api.post("/v1/auth/logout", headers=auth_header(old))).status_code == 204
    assert (await api.get("/v1/sync/pull", headers=auth_header(bearer))).status_code == 401
    new = await login(api, "same-second", device_id)
    assert (await api.get("/v1/sync/pull", headers=auth_header(new))).status_code == 200
    assert (await api.get("/v1/sync/pull", headers=auth_header(bearer))).status_code == 401


async def test_repeated_logout_login_advances_generation_without_revoking_other_devices(api, session, monkeypatch):
    from app.api import auth
    from app.db.models import Device

    monkeypatch.setattr(security, "datetime", FrozenDatetime)
    monkeypatch.setattr(auth, "utcnow", lambda: FrozenDatetime.instant)
    mine, sibling = uuid.uuid4(), uuid.uuid4()
    current = await login(api, "two-devices", mine)
    other = await login(api, "two-devices", sibling)
    other_sync = await sync_token(api, sibling, other)
    dead = []
    for generation in range(3):
        claims = security.read_access_token(api.app.state.settings.jwt_secret, current["access_token"])
        assert claims["dg"] == generation
        assert claims["iat"] == int(FrozenDatetime.instant.timestamp())
        dead += [current, await sync_token(api, mine, current)]
        assert (await api.post("/v1/auth/logout", headers=auth_header(current))).status_code == 204
        current = await login(api, "two-devices", mine)
        for old in dead:
            assert (await api.get("/v1/sync/pull", headers=auth_header(old))).status_code == 401
        for bearer in (other, other_sync, current):
            assert (await api.get("/v1/sync/pull", headers=auth_header(bearer))).status_code == 200
    await session.refresh(await session.get(Device, mine))
    assert (await session.get(Device, mine)).credential_generation == 3
    assert (await session.get(Device, sibling)).credential_generation == 0
    assert (await api.post("/v1/auth/refresh", json={"refresh_token": other["refresh_token"]})).status_code == 200


@pytest.mark.parametrize("rotated", [False, True])
async def test_old_refresh_cannot_revoke_new_login_generation(api, rotated):
    device_id = uuid.uuid4()
    old = await login(api, "old-refresh", device_id)
    working = old
    if rotated:
        response = await api.post("/v1/auth/refresh", json={"refresh_token": old["refresh_token"]})
        assert response.status_code == 200
        working = response.json()
    assert (await api.post("/v1/auth/logout", headers=auth_header(working))).status_code == 204
    new = await login(api, "old-refresh", device_id)
    for pair in (old, working):
        assert (await api.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})).status_code == 401
    refreshed = await api.post("/v1/auth/refresh", json={"refresh_token": new["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    assert security.read_access_token(api.app.state.settings.jwt_secret, refreshed.json()["access_token"])["dg"] == 1


async def test_refresh_links_exact_successor_when_created_at_ties(api, session, monkeypatch):
    from sqlalchemy import event, select
    from app.db.models import RefreshToken

    monkeypatch.setattr(security, "datetime", FrozenDatetime)

    def freeze_created_at(mapper, connection, target):
        target.created_at = FrozenDatetime.instant

    event.listen(RefreshToken, "before_insert", freeze_created_at)
    try:
        old = await login(api, "refresh-tie", uuid.uuid4())
        fresh = await api.post("/v1/auth/refresh", json={"refresh_token": old["refresh_token"]})
        assert fresh.status_code == 200
        newer = await api.post("/v1/auth/refresh", json={"refresh_token": fresh.json()["refresh_token"]})
        assert newer.status_code == 200
        rows = (await session.execute(select(RefreshToken))).scalars().all()
        assert {row.created_at for row in rows} == {FrozenDatetime.instant}
        by_hash = {row.token_hash: row for row in rows}
        first = by_hash[security.hash_refresh_token(old["refresh_token"])]
        second = by_hash[security.hash_refresh_token(fresh.json()["refresh_token"])]
        third = by_hash[security.hash_refresh_token(newer.json()["refresh_token"])]
        assert first.replaced_by_id == second.id
        assert second.replaced_by_id == third.id
        assert third.replaced_by_id is None
    finally:
        event.remove(RefreshToken, "before_insert", freeze_created_at)


@pytest.mark.parametrize("scope", [None, "sync"])
async def test_legacy_jwt_without_generation_only_works_at_zero(api, scope):
    import jwt

    device_id = uuid.uuid4()
    pair = await login(api, "legacy", device_id)
    claims = security.read_access_token(api.app.state.settings.jwt_secret, pair["access_token"])
    del claims["dg"]
    if scope:
        claims["scope"] = scope
    legacy = {"access_token": jwt.encode(claims, api.app.state.settings.jwt_secret, algorithm="HS256")}
    assert (await api.get("/v1/sync/pull", headers=auth_header(legacy))).status_code == 200
    assert (await api.post("/v1/auth/logout", headers=auth_header(pair))).status_code == 204
    fresh = await login(api, "legacy", device_id)
    assert (await api.get("/v1/sync/pull", headers=auth_header(legacy))).status_code == 401
    assert (await api.get("/v1/sync/pull", headers=auth_header(fresh))).status_code == 200


@pytest.mark.parametrize("generation", [-1, None, True, 0.0, "0", 2**63])
async def test_malformed_generation_is_401(api, generation):
    import jwt

    pair = await login(api, "malformed", uuid.uuid4())
    claims = security.read_access_token(api.app.state.settings.jwt_secret, pair["access_token"])
    claims["dg"] = generation
    bearer = {"access_token": jwt.encode(claims, api.app.state.settings.jwt_secret, algorithm="HS256")}
    response = await api.get("/v1/sync/pull", headers=auth_header(bearer))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_new_background_tokens_keep_scope_and_do_not_rotate_refresh(api, session):
    from sqlalchemy import select
    from app.db.models import RefreshToken
    from tests.conftest import make_profile

    device = uuid.uuid4()
    old = await login(api, "background", device)
    await api.post("/v1/auth/logout", headers=auth_header(old))
    pair = await login(api, "background", device)
    pid = await make_profile(session, pair["account_id"])
    claimed = await api.post(f"/v1/profiles/{pid}/reminder-authority",
                             headers=auth_header(pair), json={"device_id": str(device)})
    assert claimed.status_code == 200
    revision = claimed.json()["revision"]
    first = await sync_token(api, device, pair)
    second = await sync_token(api, device, pair)
    before = [(r.id, r.revoked_at, r.replaced_by_id) for r in (
        await session.execute(select(RefreshToken))).scalars()]
    for bearer in (first, second):
        claims = security.read_access_token(api.app.state.settings.jwt_secret, bearer["access_token"])
        assert claims["scope"] == "sync" and claims["dg"] == 1
        assert claims["exp"] - claims["iat"] == 90 * 86400
        for path in ("/v1/sync/pull", "/v1/sync/preview", f"/v1/sync/records/profiles/{pid}"):
            assert (await api.get(path, headers=auth_header(bearer))).status_code == 200
        assert (await api.post("/v1/sync/push", headers=auth_header(bearer), json={})).status_code == 200
        page = await api.get("/v1/sync/pull?ready=1", headers=auth_header(bearer))
        assert (await api.post(f"/v1/devices/{device}/ready", headers=auth_header(bearer), json={
            "profile_id": str(pid), "revision": str(revision),
            "applied_cursor": page.json()["cursor"],
        })).status_code == 204
        for method, path, body in (
            ("POST", f"/v1/profiles/{pid}/reminder-authority", {"device_id": str(device)}),
            ("POST", "/v1/pairing/codes", {"profile_id": str(pid), "role": "viewer"}),
            ("POST", "/v1/pairing/redeem", {"code": "AAAAAA"}),
            ("DELETE", "/v1/account", None),
            ("POST", f"/v1/devices/{device}/sync-token", None),
        ):
            response = await api.request(method, path, headers=auth_header(bearer), json=body)
            assert response.status_code == 403
    await session.rollback()
    after = [(r.id, r.revoked_at, r.replaced_by_id) for r in (
        await session.execute(select(RefreshToken))).scalars()]
    assert after == before
    assert (await api.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})).status_code == 200


async def test_account_erasure_invalidates_all_credential_types_after_recreation(api):
    device = uuid.uuid4()
    old = await login(api, "erasure", device)
    old_sync = await sync_token(api, device, old)
    assert (await api.delete("/v1/account", headers=auth_header(old))).status_code == 204
    new = await login(api, "erasure", device)
    assert new["account_id"] != old["account_id"]
    for bearer in (old, old_sync):
        assert (await api.get("/v1/sync/pull", headers=auth_header(bearer))).status_code == 401
    assert (await api.post("/v1/auth/refresh", json={"refresh_token": old["refresh_token"]})).status_code == 401
    assert (await api.get("/v1/sync/pull", headers=auth_header(new))).status_code == 200


@pytest.mark.parametrize("endpoint", [
    "pull", "preview", "push", "record", "sync-token", "claim", "ready", "logout", "delete",
    "issue-code", "redeem", "members", "revoke-member", "push-token", "heartbeat",
])
async def test_authenticated_request_waiting_for_fence_cannot_cross_logout_login(api, session, monkeypatch, endpoint):
    import asyncio
    from app.api import auth, pairing, sync
    from app.db.models import Device, Profile
    from tests.conftest import make_profile

    device = uuid.uuid4()
    old = await login(api, "waiting-bearer", device)
    pid = await make_profile(session, old["account_id"])
    routes = {
        "pull": (sync, "lock_publication", "GET", "/v1/sync/pull", None),
        "preview": (sync, "lock_publication", "GET", "/v1/sync/preview", None),
        "push": (sync, "lock_publication", "POST", "/v1/sync/push", {}),
        "record": (sync, "lock_caller", "GET", f"/v1/sync/records/profiles/{pid}", None),
        "sync-token": (pairing, "lock_caller", "POST", f"/v1/devices/{device}/sync-token", None),
        "claim": (pairing, "lock_publication", "POST", f"/v1/profiles/{pid}/reminder-authority", {"device_id": str(device)}),
        "ready": (pairing, "lock_publication", "POST", f"/v1/devices/{device}/ready", {"profile_id": str(pid), "revision": "1", "applied_cursor": sync.encode_cursor(1)}),
        "logout": (auth, "lock_caller", "POST", "/v1/auth/logout", None),
        "delete": (auth, "lock_caller", "DELETE", "/v1/account", None),
        "issue-code": (pairing, "lock_caller", "POST", "/v1/pairing/codes", {"profile_id": str(pid), "role": "viewer"}),
        "redeem": (pairing, "lock_caller", "POST", "/v1/pairing/redeem", {"code": "AAAAAA"}),
        "members": (pairing, "lock_caller", "GET", f"/v1/profiles/{pid}/members", None),
        "revoke-member": (pairing, "lock_caller", "DELETE", f"/v1/profiles/{pid}/members/{uuid.uuid4()}", None),
        "push-token": (pairing, "lock_caller", "PUT", f"/v1/devices/{device}/push-token", {"fcm_token": "stale"}),
        "heartbeat": (pairing, "lock_caller", "POST", f"/v1/devices/{device}/heartbeat", None),
    }
    module, helper, method, path, body = routes[endpoint]
    original = getattr(module, helper)
    arrived, release = asyncio.Event(), asyncio.Event()
    paused = False

    async def pause(*args, **kwargs):
        nonlocal paused
        if not paused:
            paused = True
            arrived.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, helper, pause)
    pending = asyncio.create_task(api.request(method, path, headers=auth_header(old), json=body))
    try:
        await asyncio.wait_for(arrived.wait(), 2)
        assert (await api.post("/v1/auth/logout", headers=auth_header(old))).status_code == 204
        new = await login(api, "waiting-bearer", device)
        release.set()
        response = await asyncio.wait_for(pending, 2)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "device_revoked"
        assert (await api.get("/v1/sync/pull", headers=auth_header(new))).status_code == 200
        await session.rollback()
        live_device = await session.get(Device, device, populate_existing=True)
        profile = await session.get(Profile, pid, populate_existing=True)
        assert live_device.credential_generation == 1 and live_device.revoked_at is None
        assert live_device.push_token is None
        assert profile.owner_device_id is None and profile.pending_owner_device_id is None
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("issuance", ["sign-in", "refresh", "sync-token"])
async def test_logout_waits_for_issuance_and_revokes_every_credential_issued_before_it(api, db_engine, monkeypatch, issuance):
    import asyncio
    from app.api import auth, deps, pairing
    from app.db.publication import lock_publication
    from tests.test_sync_publication import _pid, _waiting

    device = uuid.uuid4()
    old = await login(api, "issuance-first", device)
    arrived, release = asyncio.Event(), asyncio.Event()
    if issuance in ("sign-in", "refresh"):
        original = auth._issue

        async def hold_issue(*args):
            result = await original(*args)
            arrived.set()
            await release.wait()
            return result

        monkeypatch.setattr(auth, "_issue", hold_issue)
        operation = (login(api, "issuance-first", device) if issuance == "sign-in" else
                     api.post("/v1/auth/refresh", json={"refresh_token": old["refresh_token"]}))
    else:
        original = pairing.lock_caller

        async def hold_target(*args):
            result = await original(*args)
            arrived.set()
            await release.wait()
            return result

        monkeypatch.setattr(pairing, "lock_caller", hold_target)
        operation = sync_token(api, device, old)
    pending = asyncio.create_task(operation)
    logout_task = None
    pids = []
    try:
        await asyncio.wait_for(arrived.wait(), 2)

        async def trace_lock(session):
            pids.append(await _pid(session))
            await lock_publication(session)

        monkeypatch.setattr(deps, "lock_publication", trace_lock)
        logout_task = asyncio.create_task(api.post("/v1/auth/logout", headers=auth_header(old)))
        for _ in range(200):
            if pids:
                break
            await asyncio.sleep(0.01)
        assert pids and await _waiting(logout_task, db_engine, pids[0])
        release.set()
        issued = await asyncio.wait_for(pending, 2)
        pair = issued.json() if issuance == "refresh" else issued
        if issuance == "refresh":
            assert issued.status_code == 200
        assert (await asyncio.wait_for(logout_task, 2)).status_code == 204
        assert (await api.get("/v1/sync/pull", headers=auth_header(pair))).status_code == 401
        if issuance != "sync-token":
            assert (await api.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})).status_code == 401
    finally:
        release.set()
        for task in (pending, logout_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (pending, logout_task) if t), return_exceptions=True)


@pytest.mark.parametrize("waiting_operation", ["sign-in", "refresh"])
async def test_auth_issuance_waiting_for_logout_reads_committed_generation(api, db_engine, monkeypatch, waiting_operation):
    import asyncio
    from app.api import auth
    from tests.test_sync_publication import _pid, _waiting

    device = uuid.uuid4()
    old = await login(api, "logout-first", device)
    arrived, release = asyncio.Event(), asyncio.Event()
    original_logout_lock = auth.lock_caller

    async def hold_logout(*args):
        result = await original_logout_lock(*args)
        arrived.set()
        await release.wait()
        return result

    monkeypatch.setattr(auth, "lock_caller", hold_logout)
    logging_out = asyncio.create_task(api.post("/v1/auth/logout", headers=auth_header(old)))
    pending = None
    try:
        await asyncio.wait_for(arrived.wait(), 2)
        original_lock = auth.lock_publication
        pids = []

        async def trace_lock(session):
            pids.append(await _pid(session))
            await original_lock(session)

        monkeypatch.setattr(auth, "lock_publication", trace_lock)
        operation = (login(api, "logout-first", device) if waiting_operation == "sign-in" else
                     api.post("/v1/auth/refresh", json={"refresh_token": old["refresh_token"]}))
        pending = asyncio.create_task(operation)
        for _ in range(200):
            if pids:
                break
            await asyncio.sleep(0.01)
        assert pids and await _waiting(pending, db_engine, pids[0])
        release.set()
        assert (await asyncio.wait_for(logging_out, 2)).status_code == 204
        result = await asyncio.wait_for(pending, 2)
        if waiting_operation == "refresh":
            assert result.status_code == 401
            result = await login(api, "logout-first", device)
        assert security.read_access_token(api.app.state.settings.jwt_secret, result["access_token"])["dg"] == 1
        assert (await api.get("/v1/sync/pull", headers=auth_header(result))).status_code == 200
        assert (await api.get("/v1/sync/pull", headers=auth_header(old))).status_code == 401
    finally:
        release.set()
        for task in (logging_out, pending):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (logging_out, pending) if t), return_exceptions=True)


async def test_concurrent_refresh_serializes_rotation_and_preserves_replay_semantics(api, db_engine, monkeypatch):
    import asyncio
    from app.api import auth
    from tests.test_sync_publication import _pid, _waiting

    pair = await login(api, "concurrent-refresh", uuid.uuid4())
    arrived, release = asyncio.Event(), asyncio.Event()
    original_issue, original_lock = auth._issue, auth.lock_publication

    async def hold_issue(*args):
        result = await original_issue(*args)
        arrived.set()
        await release.wait()
        return result

    monkeypatch.setattr(auth, "_issue", hold_issue)
    first = asyncio.create_task(api.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}))
    second = None
    try:
        await asyncio.wait_for(arrived.wait(), 2)
        pids = []

        async def trace_lock(session):
            pids.append(await _pid(session))
            await original_lock(session)

        monkeypatch.setattr(auth, "lock_publication", trace_lock)
        second = asyncio.create_task(api.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}))
        for _ in range(200):
            if pids:
                break
            await asyncio.sleep(0.01)
        assert pids and await _waiting(second, db_engine, pids[0])
        release.set()
        fresh = await asyncio.wait_for(first, 2)
        replay = await asyncio.wait_for(second, 2)
        assert fresh.status_code == 200
        assert replay.status_code == 401 and replay.json()["error"]["code"] == "refresh_token_reused"
        assert (await api.post("/v1/auth/refresh", json={"refresh_token": fresh.json()["refresh_token"]})).status_code == 401
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (first, second) if t), return_exceptions=True)


async def test_sign_in_refresh_use_fresh_orm_generation(api, db_engine):
    from fastapi import Request
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.api import auth
    from app.db.models import Account, Device, RefreshToken
    from sqlalchemy import select

    device = uuid.uuid4()
    old = await login(api, "stale-orm", device)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    request = Request({"type": "http", "app": api.app})
    async with maker() as stale_login, maker() as stale_refresh:
        retained = await stale_login.get(Device, device)
        account = await stale_login.get(Account, uuid.UUID(old["account_id"]))
        token = (await stale_refresh.execute(select(RefreshToken).where(
            RefreshToken.token_hash == security.hash_refresh_token(old["refresh_token"])
        ))).scalar_one()
        retained_refresh_device = await stale_refresh.get(Device, device)
        await stale_login.commit()
        await stale_refresh.commit()
        assert retained.credential_generation == 0
        assert (await api.post("/v1/auth/logout", headers=auth_header(old))).status_code == 204
        fresh = await auth.sign_in_with_google(auth.GoogleSignIn(
            id_token="synthetic-stale-orm", device=auth.DeviceIn(id=device, platform="android")
        ), request, stale_login)
        assert retained.credential_generation == 1
        assert security.read_access_token(api.app.state.settings.jwt_secret, fresh.access_token)["dg"] == 1
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as rejected:
            await auth.refresh(auth.RefreshIn(refresh_token=old["refresh_token"]), request, stale_refresh)
        assert rejected.value.status_code == 401
        assert retained_refresh_device.credential_generation == 1
        assert token.credential_generation == 0
        assert account.id == fresh.account_id


async def test_revoked_target_cannot_receive_sync_token_from_other_device(api):
    target, sibling = uuid.uuid4(), uuid.uuid4()
    first = await login(api, "target-revoked", target)
    second = await login(api, "target-revoked", sibling)
    assert (await api.post("/v1/auth/logout", headers=auth_header(first))).status_code == 204
    response = await api.post(f"/v1/devices/{target}/sync-token", headers=auth_header(second))
    assert response.status_code == 401
    fresh = await login(api, "target-revoked", target)
    issued = await sync_token(api, target, second)
    assert security.read_access_token(api.app.state.settings.jwt_secret, issued["access_token"])["dg"] == 1
    assert (await api.get("/v1/sync/pull", headers=auth_header(fresh))).status_code == 200


async def test_google_verification_occurs_before_publication_wait(api, db_engine, monkeypatch):
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.api import auth
    from app.db.publication import lock_publication
    from tests.test_sync_publication import _pid, _waiting

    device = uuid.uuid4()
    await login(api, "google-before-fence", device)
    verified, pids = asyncio.Event(), []
    original_verify = api.app.state.google_verifier.verify
    original_lock = auth.lock_publication

    def verify(token):
        verified.set()
        return original_verify(token)

    async def trace_lock(session):
        pids.append(await _pid(session))
        await original_lock(session)

    monkeypatch.setattr(api.app.state.google_verifier, "verify", verify)
    monkeypatch.setattr(auth, "lock_publication", trace_lock)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as blocker:
        await lock_publication(blocker)
        pending = asyncio.create_task(login(api, "google-before-fence", device))
        try:
            await asyncio.wait_for(verified.wait(), 2)
            for _ in range(200):
                if pids:
                    break
                await asyncio.sleep(0.01)
            assert pids and await _waiting(pending, db_engine, pids[0])
            await blocker.commit()
            assert (await asyncio.wait_for(pending, 2))["account_id"]
        finally:
            await blocker.rollback()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_redeem_redis_wait_does_not_hold_publication(api, db_engine, monkeypatch):
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.publication import lock_publication

    pair = await login(api, "redis-before-fence", uuid.uuid4())
    arrived, release = asyncio.Event(), asyncio.Event()
    original_incr = api.app.state.redis.incr

    async def paused_redis(key):
        arrived.set()
        await release.wait()
        return await original_incr(key)

    monkeypatch.setattr(api.app.state.redis, "incr", paused_redis)
    pending = asyncio.create_task(api.post("/v1/pairing/redeem", headers=auth_header(pair), json={"code": "AAAAAA"}))
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    try:
        await asyncio.wait_for(arrived.wait(), 2)
        async with maker() as writer:
            await asyncio.wait_for(lock_publication(writer), 1)
            await writer.commit()
        release.set()
        assert (await asyncio.wait_for(pending, 2)).status_code == 400
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("endpoint", ["record", "members", "sync-token", "record-404", "target-404"])
async def test_read_only_or_refused_request_releases_fence_before_http_delivery(api, db_engine, session, endpoint):
    import asyncio
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.publication import lock_publication
    from tests.conftest import make_profile

    device = uuid.uuid4()
    pair = await login(api, "slow-send", device)
    pid = await make_profile(session, pair["account_id"])
    method, path, expected = {
        "record": ("GET", f"/v1/sync/records/profiles/{pid}", 200),
        "members": ("GET", f"/v1/profiles/{pid}/members", 200),
        "sync-token": ("POST", f"/v1/devices/{device}/sync-token", 200),
        "record-404": ("GET", f"/v1/sync/records/profiles/{uuid.uuid4()}", 404),
        "target-404": ("POST", f"/v1/devices/{uuid.uuid4()}/sync-token", 404),
    }[endpoint]
    arrived, release = asyncio.Event(), asyncio.Event()

    async def paused_send_app(scope, receive, send):
        async def paused_send(message):
            if message["type"] == "http.response.start":
                arrived.set()
                await release.wait()
            await send(message)
        await api.app(scope, receive, paused_send)

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with AsyncClient(transport=ASGITransport(app=paused_send_app), base_url="http://test") as client:
        pending = asyncio.create_task(client.request(method, path, headers=auth_header(pair)))
        try:
            await asyncio.wait_for(arrived.wait(), 2)
            async with maker() as writer:
                await asyncio.wait_for(lock_publication(writer), 1)
                await writer.commit()
            release.set()
            assert (await asyncio.wait_for(pending, 2)).status_code == expected
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
