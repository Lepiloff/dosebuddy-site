"""Bounded sync work keeps row outcomes and the existing retry envelope."""

import uuid

import pytest
from sqlalchemy import event, select, text

from app.db.models import Medication, Schedule, StockEvent
from tests.conftest import auth_header, sign_in
from tests.test_sync import dose, medication, profile, pull, push, schedule, stock

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("size,budget", [(1, 18), (10, 18), (200, 24), (1000, 48)])
async def test_medication_push_has_bounded_round_trips_and_paginates_all_rows(api, db_engine, size, budget):
    owner = await sign_in(api, f"batch-{uuid.uuid4()}")
    pid = str(uuid.uuid4())
    await push(api, owner, profiles=[profile(pid)])
    before = (await pull(api, owner))["cursor"]
    rows = [medication(str(uuid.uuid4()), pid, name=f"Synthetic {i}", at=100) for i in range(size)]
    statements = []

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", observe)
    try:
        result = await push(api, owner, medications=rows)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", observe)
    assert result["rejected"] == result["retry"] == []
    assert len(statements) <= budget, f"{size} rows used {len(statements)} SQL statements"
    found = {}
    cursor = before
    while True:
        page = await pull(api, owner, cursor)
        found.update({row["id"]: row for row in page["changes"].get("medications", [])})
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    assert set(found) == {row["id"] for row in rows}
    assert {row["name"] for row in found.values()} == {row["name"] for row in rows}


async def test_a_failed_chunk_falls_back_without_poisoning_valid_rows_or_outcome_order(api, session):
    owner = await sign_in(api, f"fallback-{uuid.uuid4()}")
    pid, mid = str(uuid.uuid4()), str(uuid.uuid4())
    await push(api, owner, profiles=[profile(pid)], medications=[medication(mid, pid)])
    good_a, bad_fk, moved, good_b = (str(uuid.uuid4()) for _ in range(4))
    other_mid = str(uuid.uuid4())
    await push(api, owner, medications=[medication(other_mid, pid)],
               stock_events=[stock(moved, mid)])
    missing_dose = str(uuid.uuid4())
    result = await push(api, owner, stock_events=[
        stock(good_a, mid), {**stock(bad_fk, mid), "dose_event_id": missing_dose},
        stock(moved, other_mid, at=9000000000000), stock(good_b, mid),
    ])
    assert result["retry"] == [{"entity": "stock_events", "id": bad_fk, "code": "missing_parent"}]
    assert result["rejected"] == [{"entity": "stock_events", "id": moved, "code": "immutable_parent"}]
    saved = (await session.execute(select(StockEvent))).scalars().all()
    assert {str(row.id) for row in saved} == {good_a, moved, good_b}
    assert next(row for row in saved if str(row.id) == moved).medication_id == uuid.UUID(mid)
    # The identical retry lands once its actual FK parent is present.
    await push(api, owner, dose_events=[dose(missing_dose, mid, pid)])
    retried = await push(api, owner, stock_events=[{**stock(bad_fk, mid), "dose_event_id": missing_dose}])
    assert retried["retry"] == retried["rejected"] == []


@pytest.mark.parametrize("gap", [0, 99, 100])
async def test_duplicate_ids_keep_lww_and_parent_move_semantics_across_chunks(api, session, gap):
    owner = await sign_in(api, f"duplicates-{uuid.uuid4()}")
    p1, p2, mid = (str(uuid.uuid4()) for _ in range(3))
    await push(api, owner, profiles=[profile(p1), profile(p2)])
    first = medication(mid, p1, name="First", at=100)
    newer = {**first, "name": "Newer", "op_seq": first["op_seq"] + 1}
    moved = {**newer, "profile_id": p2, "updated_at": 101, "op_seq": newer["op_seq"] + 1}
    rows = [first] + [medication(str(uuid.uuid4()), p1, at=100) for _ in range(gap)] + [newer, moved, first]
    result = await push(api, owner, medications=rows)
    assert result["retry"] == []
    assert result["rejected"] == [{"entity": "medications", "id": mid, "code": "immutable_parent"}]
    saved = await session.get(Medication, uuid.UUID(mid))
    assert saved.name == "Newer" and saved.profile_id == uuid.UUID(p1)
    assert saved.op_seq == newer["op_seq"]
    assert len((await session.execute(select(Medication.id))).scalars().all()) == gap + 1


async def test_parent_batches_are_settled_before_children_and_foreign_rows_stay_private(api, session):
    owner, stranger = [await sign_in(api, f"parents-{uuid.uuid4()}") for _ in range(2)]
    foreign_pid = str(uuid.uuid4())
    await push(api, stranger, profiles=[profile(foreign_pid)])
    pid, mid, sid, eid = (str(uuid.uuid4()) for _ in range(4))
    forbidden_mid = str(uuid.uuid4())
    result = await push(api, owner, profiles=[profile(pid)],
        medications=[medication(mid, pid), medication(forbidden_mid, foreign_pid)],
        schedules=[schedule(sid, mid)], stock_events=[stock(eid, mid)])
    assert result["retry"] == []
    assert result["rejected"] == [{"entity": "medications", "id": forbidden_mid, "code": "forbidden_role"}]
    assert await session.get(Schedule, uuid.UUID(sid)) is not None
    assert await session.get(StockEvent, uuid.UUID(eid)) is not None
    assert await session.get(Medication, uuid.UUID(forbidden_mid)) is None
    assert not (await pull(api, stranger))["changes"].get("medications")


async def test_batched_children_invalidate_readiness_until_the_full_high_water_is_applied(api, session):
    from sqlalchemy import update
    from app.core.security import read_access_token
    from app.db.models import Profile
    owner = await sign_in(api, f"ready-batch-{uuid.uuid4()}")
    dev = read_access_token(api.app.state.settings.jwt_secret, owner["access_token"])["did"]
    pid = str(uuid.uuid4())
    await push(api, owner, profiles=[profile(pid)])
    await session.execute(update(Profile).where(Profile.id == uuid.UUID(pid)).values(owner_device_id=uuid.UUID(dev)))
    await session.commit()
    before = await pull(api, owner)
    revision = str((await session.get(Profile, uuid.UUID(pid))).server_seq)
    ready = {"profile_id": pid, "revision": revision, "applied_cursor": before["cursor"]}
    response = await api.post(f"/v1/devices/{dev}/ready", headers=auth_header(owner), json=ready)
    assert response.status_code == 204
    assert (await pull(api, owner))["authority"][pid]["owner_ready"]
    rows = [medication(str(uuid.uuid4()), pid, at=100) for _ in range(200)]
    await push(api, owner, medications=rows)
    after = await pull(api, owner, before["cursor"])
    assert len(after["changes"]["medications"]) == 200
    assert not after["authority"][pid]["owner_ready"]
    stale = await api.post(f"/v1/devices/{dev}/ready", headers=auth_header(owner), json=ready)
    assert stale.status_code == 409
    ready["applied_cursor"] = after["cursor"]
    renewed = await api.post(f"/v1/devices/{dev}/ready", headers=auth_header(owner), json=ready)
    assert renewed.status_code == 204
    assert (await pull(api, owner, after["cursor"]))["authority"][pid]["owner_ready"]


async def test_batched_parents_keep_canonical_dose_supersede_and_set_once_key(api, session):
    from app.db.models import DoseEvent
    from tests.test_sync import _new_client_push
    subject = f"canonical-batch-{uuid.uuid4()}"
    modern, legacy = await sign_in(api, subject), await sign_in(api, subject)
    pid, mid, sid, first, winner, eid = (str(uuid.uuid4()) for _ in range(6))
    medications = [medication(mid, pid)] + [medication(str(uuid.uuid4()), pid) for _ in range(100)]
    await _new_client_push(api, modern, profiles=[profile(pid)], medications=medications,
        schedules=[schedule(sid, mid)], dose_events=[dose(first, mid, pid, sid, planned=123)])
    # The legacy twin still wins over a holder that can adopt; batching parents
    # must not defer the dose write past the stock FK or the supersede effect.
    await push(api, legacy, medications=[medication(str(uuid.uuid4()), pid) for _ in range(100)],
        dose_events=[dose(winner, mid, pid, sid, planned=123)],
        stock_events=[{**stock(eid, mid), "dose_event_id": winner}])
    saved_first = await session.get(DoseEvent, uuid.UUID(first))
    assert saved_first.superseded_by == uuid.UUID(winner)
    assert saved_first.dose_key == f"{sid}:123"
    refused = await _new_client_push(api, modern,
        dose_events=[dose(str(uuid.uuid4()), mid, pid, sid, planned=123)])
    assert refused["rejected"][0]["code"] == "duplicate_dose"
    assert refused["rejected"][0]["canonical_id"] == winner
    assert (await session.get(StockEvent, uuid.UUID(eid))).dose_event_id == uuid.UUID(winner)
    # Deleting the schedule removes its FK, while a later dose edit still keeps
    # the server-derived key and the original supersede destination.
    await session.delete(await session.get(Schedule, uuid.UUID(sid)))
    await session.commit()
    await push(api, legacy, dose_events=[dose(winner, mid, pid, None, status="taken", at=9000000000000, planned=123)])
    saved_winner = await session.get(DoseEvent, uuid.UUID(winner), populate_existing=True)
    assert saved_winner.dose_key == f"{sid}:123" and saved_winner.status == "taken"


@pytest.mark.parametrize("sqlstate,expected", [("55P03", 429), ("40001", 500), (None, 500)])
async def test_request_database_errors_only_map_lock_timeout_and_rollback_first(api, db_engine, monkeypatch, sqlstate, expected):
    from sqlalchemy.exc import DBAPIError
    from app.api import sync
    from app.db.session import create_sessionmaker
    owner = await sign_in(api, f"db-error-{uuid.uuid4()}")
    pid = str(uuid.uuid4())
    await push(api, owner, profiles=[profile(pid)])
    # Use an actual write before the wrapped driver error. The error handler
    # must never acknowledge it, and request cleanup must finish before return.
    async def fail(session, _caller):
        await session.execute(text("UPDATE profiles SET sort_order = 999 WHERE id = :id"),
                              {"id": uuid.UUID(pid)})
        cause = RuntimeError("synthetic private driver message")
        cause.sqlstate = sqlstate
        wrapped = RuntimeError("wrapped driver")
        wrapped.__cause__ = cause
        raise DBAPIError("private statement", {}, wrapped, False)
    monkeypatch.setattr(sync, "recheck_caller", fail)
    api._transport.raise_app_exceptions = False
    response = await api.post("/v1/sync/push", headers=auth_header(owner), json={"changes": {}})
    assert response.status_code == expected
    assert response.json()["error"]["code"] == ("rate_limited" if expected == 429 else "internal_error")
    assert response.headers.get("Retry-After") == ("5" if expected == 429 else None)
    assert "private" not in response.text
    async with create_sessionmaker(db_engine)() as check:
        from app.db.models import Profile
        assert (await check.get(Profile, uuid.UUID(pid))).sort_order == 0


async def test_http_error_envelope_preserves_retry_after(api):
    from fastapi import HTTPException
    @api.app.get("/synthetic-retry")
    async def retry():
        raise HTTPException(429, "rate_limited", headers={"Retry-After": "5"})
    response = await api.get("/synthetic-retry")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert response.headers["Retry-After"] == "5"
