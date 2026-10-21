"""Actual HTTP/auth/PostgreSQL reader, without FCM or app acceptance claims."""
import asyncio
import base64
import uuid
from dataclasses import replace
from datetime import timedelta

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import caregiver_events as reader
from app.core.caregiver_cursor import CursorCodec, TTL_MS
from app.core.security import mint_sync_token, read_access_token
from app.db.models import AlertDelivery, Device, utcnow
from app.services.caregiver_recovery import close_coverage, enable_coverage, set_reader
from tests.conftest import auth_header, sign_in
from tests.test_alerts import _with_watcher
from tests.test_alert_eligibility import _regrant

pytestmark = pytest.mark.asyncio
KEY = base64.urlsafe_b64encode(b"r" * 32).rstrip(b"=").decode()
OTHER_KEY = base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode()
PATH = "/v1/caregiver-events"
ENVELOPE = {"version", "account_id", "feed_epoch", "coverage_started_at", "as_of", "events", "cursor", "cursor_expires_at", "has_more"}
EVENT = {"event_id", "event_seq", "type", "account_id", "profile_id", "subject_id", "raised_at", "expires_at"}


def privacy(response):
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache" and response.headers["vary"] == "Authorization"
    assert "etag" not in response.headers and "last-modified" not in response.headers


def error(response, status, code):
    privacy(response)
    assert response.status_code == status, response.text
    assert response.json() == {"error": {"code": code, "message": code.replace("_", " ")}}


async def setup(api, session):
    owner, caregiver, pid, *_ = await _with_watcher(api, session)
    api.app.state.settings = api.app.state.settings.model_copy(update={"caregiver_cursor_signing_key": KEY})
    await enable_coverage(session, writers_verified=True)
    await set_reader(session, enabled=True, writers_verified=True)
    await session.commit()
    return owner, caregiver, pid


async def seed(session, caregiver, pid, start=1, stop=1, raised=None):
    raised = raised or utcnow() - timedelta(minutes=1)
    await session.execute(text("""INSERT INTO alert_deliveries
        (id,account_id,profile_id,kind,subject_id,state,attempts,next_attempt_at,expires_at)
        SELECT md5('b2-event-'||i)::uuid, :aid, :pid, 'dose_missed',
        (md5('b2-subject-'||i)::uuid)::text,
        (ARRAY['pending','sent','given_up','expired'])[1+(i%4)], 0, :raised, :expires
        FROM generate_series(CAST(:start AS integer),CAST(:stop AS integer)) AS i"""),
        {"aid": uuid.UUID(caregiver["account_id"]), "pid": uuid.UUID(pid), "start": start,
         "stop": stop, "raised": raised, "expires": raised + timedelta(hours=6)})
    await session.commit()


async def page(api, caregiver, cursor=None):
    response = await api.get(PATH, params={"cursor": cursor} if cursor else {}, headers=auth_header(caregiver))
    privacy(response)
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data) == ENVELOPE and data["version"] == 1 and type(data["version"]) is int
    assert data["account_id"] == caregiver["account_id"]
    assert data["coverage_started_at"] <= data["as_of"] < data["cursor_expires_at"] <= data["as_of"] + TTL_MS
    for event in data["events"]:
        assert set(event) == EVENT and event["account_id"] == data["account_id"]
        assert str(int(event["event_seq"])) == event["event_seq"]
        assert event["raised_at"] <= data["as_of"] < event["expires_at"]
        assert event["expires_at"] - event["raised_at"] == 6 * 60 * 60 * 1000
    return data


async def test_235_plus_5_snapshots_four_page_prefix_no_ack_or_ttl_change(api, session):
    _, caregiver, pid = await setup(api, session)
    await seed(session, caregiver, pid, stop=235)
    before = [tuple(row) for row in (await session.execute(select(AlertDelivery.id, AlertDelivery.state,
                                                                 AlertDelivery.expires_at, AlertDelivery.attempts))).all()]
    cursor, sequences, expiry = None, [], None
    for _ in range(4):
        data = await page(api, caregiver, cursor)
        assert data["has_more"] and len(data["events"]) == 50
        if expiry is None:
            expiry = data["cursor_expires_at"]
            await seed(session, caregiver, pid, 236, 240)
        assert data["cursor_expires_at"] == expiry
        sequences += [int(e["event_seq"]) for e in data["events"]]
        cursor = data["cursor"]
    assert sequences == list(range(1, 201))
    rest = await page(api, caregiver, cursor)
    assert [int(e["event_seq"]) for e in rest["events"]] == list(range(201, 236))
    assert not rest["has_more"]
    second = await page(api, caregiver, rest["cursor"])
    assert [int(e["event_seq"]) for e in second["events"]] == list(range(236, 241))
    assert not second["has_more"]
    assert (await page(api, caregiver, second["cursor"]))["events"] == []
    after = {row.id: (row.state, row.expires_at, row.attempts) for row in (await session.execute(select(AlertDelivery))).scalars()}
    assert all(after[identity] == (state, deadline, attempts) for identity, state, deadline, attempts in before)
    assert (await page(api, caregiver, cursor))["events"] == rest["events"]


async def test_empty_continuation_expiry_checkpoint_and_epoch_reset(api, session, monkeypatch):
    _, caregiver, pid = await setup(api, session)
    await seed(session, caregiver, pid, stop=51)
    first, now = await page(api, caregiver), utcnow()
    monkeypatch.setattr(reader, "utcnow", lambda: now + timedelta(hours=7))
    empty = await page(api, caregiver, first["cursor"])
    assert empty["events"] == [] and not empty["has_more"]
    assert empty["cursor_expires_at"] == first["cursor_expires_at"]
    assert (await page(api, caregiver, empty["cursor"]))["cursor_expires_at"] > empty["cursor_expires_at"]
    monkeypatch.setattr(reader, "utcnow", lambda: now + timedelta(hours=24, seconds=1))
    error(await api.get(PATH, params={"cursor": first["cursor"]}, headers=auth_header(caregiver)), 410, "caregiver_cursor_expired")
    assert (await page(api, caregiver))["events"] == []
    monkeypatch.setattr(reader, "utcnow", utcnow)
    await close_coverage(session)
    await session.commit()
    error(await api.get(PATH, headers=auth_header(caregiver)), 503, "caregiver_events_unavailable")
    await enable_coverage(session, writers_verified=True)
    await set_reader(session, enabled=True, writers_verified=True)
    await session.commit()
    error(await api.get(PATH, params={"cursor": first["cursor"]}, headers=auth_header(caregiver)), 410, "caregiver_feed_reset")
    assert (await page(api, caregiver))["events"] == []


@pytest.mark.parametrize("query", ["limit=0", "limit=51", "limit=-1", "limit=1.0", "limit=01", "limit=true",
    "limit=", "limit=1&limit=2", "cursor=", "cursor=x&cursor=y", "cursor="+"a"*2049, "cursor=%D1%8F"])
async def test_query_shape_is_422_no_echo(api, session, query):
    _, caregiver, _ = await setup(api, session)
    error(await api.get(PATH+"?"+query, headers=auth_header(caregiver)), 422, "invalid_request")


async def test_unknown_query_limit_one_conditionals_no_redirect_and_no_membership(api, session):
    _, caregiver, pid = await setup(api, session)
    await seed(session, caregiver, pid, stop=2)
    response = await api.get(PATH+"?limit=1&unknown=private&account_id=foreign", headers={**auth_header(caregiver),
        "If-None-Match": "*", "If-Modified-Since": "Wed, 21 Oct 2099 07:28:00 GMT"})
    assert response.status_code == 200 and len(response.json()["events"]) == 1 and response.json()["has_more"]
    privacy(response)
    nobody = await sign_in(api, "nobody")
    assert (await page(api, nobody))["events"] == []
    trailing = await api.get(PATH+"/", headers=auth_header(caregiver))
    assert trailing.status_code == 404 and "location" not in trailing.headers
    privacy(trailing)


async def test_cursor_binding_forgery_impossible_positions_and_rotation(api, session):
    _, caregiver, pid = await setup(api, session)
    await seed(session, caregiver, pid)
    first = await page(api, caregiver)
    token = first["cursor"]
    other = await sign_in(api, "foreign-reader")
    error(await api.get(PATH, params={"cursor": token}, headers=auth_header(other)), 400, "caregiver_cursor_invalid")
    for forged in ("garbage", token[:-8]+"abcdefgh"):
        error(await api.get(PATH, params={"cursor": forged}, headers=auth_header(caregiver)), 400, "caregiver_cursor_invalid")
    codec = CursorCodec(api.app.state.settings)
    position = codec.decode(token, first["account_id"], first["feed_epoch"], first["as_of"])
    for bad in (replace(position, p=-1), replace(position, h=0), replace(position, p=2, h=1),
                replace(position, p=True), replace(position, p=999, h=999)):
        error(await api.get(PATH, params={"cursor": codec.encode(bad)}, headers=auth_header(caregiver)), 400, "caregiver_cursor_invalid")
    api.app.state.settings = api.app.state.settings.model_copy(update={
        "caregiver_cursor_signing_key": OTHER_KEY, "caregiver_cursor_verifier_keys": [KEY]})
    assert (await page(api, caregiver, token))["events"] == []
    api.app.state.settings = api.app.state.settings.model_copy(update={"caregiver_cursor_verifier_keys": []})
    error(await api.get(PATH, params={"cursor": token}, headers=auth_header(caregiver)), 400, "caregiver_cursor_invalid")


async def test_auth_before_cursor_facts_and_sync_scope(api, session):
    _, caregiver, _ = await setup(api, session)
    error(await api.get(PATH+"?cursor=forged"), 401, "missing_token")
    error(await api.get(PATH, headers={"Authorization": "Bearer invalid"}), 401, "invalid_token")
    settings = api.app.state.settings
    claims = read_access_token(settings.jwt_secret, caregiver["access_token"])
    token, _ = mint_sync_token(settings.jwt_secret, uuid.UUID(claims["sub"]), uuid.UUID(claims["did"]), claims["dg"])
    assert (await page(api, {"account_id": caregiver["account_id"], "access_token": token}))["events"] == []
    claims["scope"] = "pairing"
    error(await api.get(PATH+"?cursor=forged", headers={"Authorization": "Bearer "+jwt.encode(claims, settings.jwt_secret, algorithm="HS256")}), 403, "token_scope_insufficient")


@pytest.mark.parametrize("state", ["pending", "sent", "given_up"])
async def test_retired_events_do_not_reappear_after_same_id_regrant(api, session, state):
    owner, caregiver, pid = await setup(api, session)
    await seed(session, caregiver, pid)
    await session.execute(update(AlertDelivery).values(state=state))
    await session.commit()
    assert len((await page(api, caregiver))["events"]) == 1
    await _regrant(api, owner, caregiver, pid, "viewer")
    assert (await page(api, caregiver))["events"] == []
    await _regrant(api, owner, caregiver, pid, "with_alerts")
    assert (await page(api, caregiver))["events"] == []


async def test_second_auth_read_after_initial_auth_rejects_generation_change(api, session, monkeypatch, db_engine):
    _, caregiver, _ = await setup(api, session)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = reader.lock_caller
    async def delayed(session, caller):
        entered.set()
        await resume.wait()
        return await original(session, caller)
    monkeypatch.setattr(reader, "lock_caller", delayed)
    pending = asyncio.create_task(api.get(PATH, headers=auth_header(caregiver)))
    await asyncio.wait_for(entered.wait(), 3)
    claims = read_access_token(api.app.state.settings.jwt_secret, caregiver["access_token"])
    async with async_sessionmaker(db_engine)() as lifecycle:
        await lifecycle.execute(update(Device).where(Device.id == uuid.UUID(claims["did"])).values(credential_generation=Device.credential_generation + 1))
        await lifecycle.commit()
    resume.set()
    error(await asyncio.wait_for(pending, 3), 401, "device_revoked")


async def test_missing_key_and_unexpected_500_are_private(api, session, monkeypatch):
    _, caregiver, _ = await setup(api, session)
    api.app.state.settings = api.app.state.settings.model_copy(update={"caregiver_cursor_signing_key": ""})
    result = await api.get(PATH, headers=auth_header(caregiver))
    error(result, 503, "caregiver_events_unavailable")
    assert result.headers["retry-after"] == "60"
    async def fail(*args):
        raise RuntimeError("private-exception-payload")
    monkeypatch.setattr(reader, "lock_caller", fail)
    async with AsyncClient(transport=ASGITransport(app=api.app, raise_app_exceptions=False), base_url="http://test") as client:
        result = await client.get(PATH+"?cursor=private-query", headers=auth_header(caregiver))
    error(result, 500, "internal_error")
    assert "private" not in result.text


async def test_wire_millisecond_expiry_boundary_does_not_invalidate_entire_page(api, session, monkeypatch):
    _, caregiver, pid = await setup(api, session)
    now = (utcnow() + timedelta(seconds=1)).replace(microsecond=123500)
    await seed(session, caregiver, pid, 1, 1, raised=now.replace(microsecond=123900) - timedelta(hours=6))
    await seed(session, caregiver, pid, 2, 2, raised=now.replace(microsecond=124000) - timedelta(hours=6))
    monkeypatch.setattr(reader, "utcnow", lambda: now)
    data = await page(api, caregiver)
    assert [e["event_seq"] for e in data["events"]] == ["2"]
    assert data["events"][0]["expires_at"] == data["as_of"] + 1
    assert not data["has_more"]
    assert (await page(api, caregiver, data["cursor"]))["events"] == []


async def test_profile_stale_original_day_lifetime_and_minimal_fields(api, session):
    from app.services.alerts import Alert, claim
    from app.db.models import AlertKind
    _, caregiver, pid = await setup(api, session)
    now = utcnow() - timedelta(minutes=1)
    assert await claim(session, Alert(uuid.UUID(caregiver["account_id"]), uuid.UUID(pid),
        AlertKind.profile_stale, now.date().isoformat()), now)
    await session.commit()
    response = await api.get(PATH, headers=auth_header(caregiver))
    assert response.status_code == 200
    event = response.json()["events"][0]
    assert set(event) == EVENT and event["type"] == "profile_stale"
    assert event["subject_id"] == now.date().isoformat()
    assert event["expires_at"] - event["raised_at"] == 86400000


@pytest.mark.parametrize("state", ["55P03", "08006"])
async def test_db_failure_or_lock_timeout_has_private_headers(api, session, monkeypatch, state):
    from sqlalchemy.exc import DBAPIError
    _, caregiver, _ = await setup(api, session)
    class DriverFailure(Exception):
        sqlstate = state
    async def fail(*args):
        raise DBAPIError("private-statement", {}, DriverFailure("private-driver-error"), False)
    monkeypatch.setattr(reader, "lock_caller", fail)
    response = await api.get(PATH+"?cursor=private-query", headers=auth_header(caregiver))
    error(response, 429 if state == "55P03" else 500, "rate_limited" if state == "55P03" else "internal_error")
    if state == "55P03":
        assert response.headers["retry-after"] == "5"


async def test_actual_postgresql_error_gets_private_500_without_sql_parameters(api, session, monkeypatch):
    _, caregiver, _ = await setup(api, session)
    async def database_failure(session, caller):
        await session.execute(text("SELECT 1/0"))
    monkeypatch.setattr(reader, "lock_caller", database_failure)
    result = await api.get(PATH+"?cursor=private-query", headers=auth_header(caregiver))
    error(result, 500, "internal_error")


async def test_fresh_eligibility_filters_even_nonretired_child_from_alternative_writer(api, session):
    from app.db.models import CaregiverRecoveryEvent
    _, caregiver, pid, *_ = await _with_watcher(api, session, role="viewer")
    api.app.state.settings = api.app.state.settings.model_copy(update={"caregiver_cursor_signing_key": KEY})
    await enable_coverage(session, writers_verified=True)
    await set_reader(session, enabled=True, writers_verified=True)
    await session.commit()
    await seed(session, caregiver, pid)
    assert (await session.execute(select(CaregiverRecoveryEvent.retired_at))).scalar_one() is None
    assert (await page(api, caregiver))["events"] == []


async def test_reader_committed_watermark_ignores_rolled_back_sequence_and_survives_purge(api, session):
    from app.db.models import CaregiverRecoveryEvent
    from sqlalchemy import delete
    _, caregiver, pid = await setup(api, session)
    await session.execute(text("SELECT next_caregiver_event_seq()"))
    await session.rollback()
    first = await page(api, caregiver)
    codec = CursorCodec(api.app.state.settings)
    assert codec.decode(first["cursor"], first["account_id"], first["feed_epoch"], first["as_of"]).h == 0
    await seed(session, caregiver, pid)
    next_page = await page(api, caregiver, first["cursor"])
    assert [e["event_seq"] for e in next_page["events"]] == ["2"]
    await session.execute(delete(CaregiverRecoveryEvent))
    await session.commit()
    empty = await page(api, caregiver, next_page["cursor"])
    assert empty["events"] == []
    assert codec.decode(empty["cursor"], empty["account_id"], empty["feed_epoch"], empty["as_of"]).h == 2


async def test_reader_waits_for_parent_child_commit_and_samples_as_of_after_fence(api, session, db_engine, monkeypatch):
    from app.services.alerts import Alert, claim
    from app.db.models import AlertKind
    from tests.test_sync_publication import _waiting
    _, caregiver, pid = await setup(api, session)
    entered, resume = asyncio.Event(), asyncio.Event()
    reader_pid, clock = [], [utcnow()]
    original = reader.lock_caller
    async def delayed(session, caller):
        reader_pid.append((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        entered.set()
        await resume.wait()
        return await original(session, caller)
    monkeypatch.setattr(reader, "lock_caller", delayed)
    monkeypatch.setattr(reader, "utcnow", lambda: clock[0])
    task = asyncio.create_task(page(api, caregiver))
    await asyncio.wait_for(entered.wait(), 3)
    async with async_sessionmaker(db_engine)() as writer:
        assert await claim(writer, Alert(uuid.UUID(caregiver["account_id"]), uuid.UUID(pid),
            AlertKind.dose_missed, str(uuid.uuid4())), clock[0])
        resume.set()
        assert await _waiting(task, db_engine, reader_pid[0])
        clock[0] += timedelta(seconds=1)
        await writer.commit()
    result = await asyncio.wait_for(task, 3)
    assert result["as_of"] == reader.milliseconds(clock[0])
    assert len(result["events"]) == 1 and result["events"][0]["event_seq"] == "1"
