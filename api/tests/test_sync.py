"""Sync.

The tests that matter here are the ones about what a caregiver must *not*
receive, and about what happens when two devices disagree. A push that works and
a pull that returns rows is the easy half.
"""

import time
import uuid

import pytest

from tests.conftest import auth_header, sign_in

pytestmark = pytest.mark.asyncio


def ms() -> int:
    return int(time.time() * 1000)


def profile(pid: str, name: str = "Someone", at: int | None = None) -> dict:
    t = at or ms()
    return {"id": pid, "created_at": t, "updated_at": t, "deleted_at": None,
            # A real Android colour: unsigned ARGB, past the top of int32.
            "name": name, "color": 4283215696, "sort_order": 0}


def medication(mid: str, pid: str, name: str = "Aspirin", at: int | None = None, **kw) -> dict:
    t = at or ms()
    return {"id": mid, "created_at": t, "updated_at": t, "deleted_at": None,
            "profile_id": pid, "name": name, "form": "tablet", "dose_amount": 1.0,
            "notes": None, "dosage_text": None, "pack_size": None,
            "refill_threshold_days": 3, "is_active": True, "photo_key": None, **kw}


def schedule(sid: str, mid: str, at: int | None = None) -> dict:
    t = at or ms()
    return {"id": sid, "created_at": t, "updated_at": t, "deleted_at": None,
            "medication_id": mid, "type": "fixed_times", "times": '["09:00","21:00"]',
            "days_of_week": None, "interval_days": None, "start_date": "2026-08-01",
            "end_date": None}


def dose(did: str, mid: str, pid: str, sid: str | None = None,
         status: str = "pending", at: int | None = None,
         planned: int | None = None) -> dict:
    """`at` is when the row was last edited, `planned` is when the dose was due.

    Separate on purpose. Conflating them means a dose planned an hour ago also
    claims to have been edited an hour ago — which last-write-wins correctly
    refuses, since the row on the server is newer.
    """
    t = at or ms()
    return {"id": did, "created_at": t, "updated_at": t, "deleted_at": None,
            "schedule_id": sid, "medication_id": mid, "profile_id": pid,
            "planned_at": planned if planned is not None else t,
            "status": status, "action_at": None,
            "snooze_count": 0, "snoozed_until": None, "dose_amount": 1.0}


async def push(api, tokens, **entities):
    r = await api.post("/v1/sync/push", headers=auth_header(tokens),
                       json={"changes": entities})
    assert r.status_code == 200, r.text
    return r.json()


async def pull(api, tokens, cursor=None):
    url = "/v1/sync/pull" + (f"?cursor={cursor}" if cursor else "")
    r = await api.get(url, headers=auth_header(tokens))
    assert r.status_code == 200, r.text
    return r.json()


async def _owner_with_data(api):
    owner = await sign_in(api, f"owner-{uuid.uuid4()}")
    pid, mid, sid, did = (str(uuid.uuid4()) for _ in range(4))
    await push(api, owner, profiles=[profile(pid)])
    await push(api, owner,
               medications=[medication(mid, pid)],
               schedules=[schedule(sid, mid)],
               dose_events=[dose(did, mid, pid, sid)])
    return owner, pid, mid, sid, did


async def _pair(api, owner, pid, role="with_alerts"):
    caregiver = await sign_in(api, f"cg-{uuid.uuid4()}")
    code = (await api.post("/v1/pairing/codes", headers=auth_header(owner),
                           json={"profile_id": pid, "role": role})).json()["code"]
    await api.post("/v1/pairing/redeem", headers=auth_header(caregiver), json={"code": code})
    return caregiver


async def test_push_then_pull_returns_what_was_sent(api):
    owner, pid, mid, sid, did = await _owner_with_data(api)

    got = await pull(api, owner)
    assert {e["id"] for e in got["changes"]["profiles"]} == {pid}
    assert {e["id"] for e in got["changes"]["medications"]} == {mid}
    assert {e["id"] for e in got["changes"]["schedules"]} == {sid}
    assert {e["id"] for e in got["changes"]["dose_events"]} == {did}


async def test_the_cursor_only_returns_what_is_new(api):
    owner, pid, mid, _, _ = await _owner_with_data(api)

    first = await pull(api, owner)
    again = await pull(api, owner, first["cursor"])
    assert again["changes"] == {}

    await push(api, owner, medications=[medication(str(uuid.uuid4()), pid, "Second")])
    after = await pull(api, owner, first["cursor"])
    assert len(after["changes"]["medications"]) == 1


async def test_a_watcher_never_receives_schedules(api):
    """The load-bearing test in this file.

    Without schedules the caregiver's device has nothing to materialise an alarm
    from, so the one-reminder-owner invariant holds by construction. If this ever
    fails, the wrong phone can ring for someone else's dose.
    """
    owner, pid, mid, sid, did = await _owner_with_data(api)
    caregiver = await _pair(api, owner, pid)

    got = await pull(api, caregiver)
    assert "schedules" not in got["changes"]
    assert "stock_events" not in got["changes"]


async def test_a_watcher_sees_status_and_the_medication_name(api):
    """Names do cross: an alert that cannot say which medication was missed is
    not worth sending. That is the deliberate minimum, not an oversight."""
    owner, pid, mid, sid, did = await _owner_with_data(api)
    caregiver = await _pair(api, owner, pid)

    got = await pull(api, caregiver)
    med = got["changes"]["medications"][0]
    assert med["name"] == "Aspirin"
    assert med["form"] == "tablet"

    ev = got["changes"]["dose_events"][0]
    assert ev["status"] == "pending"


async def test_a_watcher_gets_no_stock_or_notes(api):
    owner, pid, mid, _, _ = await _owner_with_data(api)
    await push(api, owner, medications=[
        medication(mid, pid, notes="secret note", pack_size=30.0, at=ms() + 1000)
    ])
    caregiver = await _pair(api, owner, pid)

    med = (await pull(api, caregiver))["changes"]["medications"][0]
    assert "notes" not in med
    assert "pack_size" not in med
    assert "refill_threshold_days" not in med


async def test_a_watcher_cannot_push_to_the_profile_it_watches(api):
    owner, pid, mid, _, _ = await _owner_with_data(api)
    caregiver = await _pair(api, owner, pid)

    out = await push(api, caregiver, medications=[
        medication(str(uuid.uuid4()), pid, "Injected")
    ])
    assert len(out["rejected"]) == 1
    assert out["rejected"][0]["code"] == "forbidden_role"


async def test_one_bad_row_does_not_throw_away_the_batch(api):
    """A rejection is per record. Failing the whole push would mean one row the
    caller had no business sending costs them everything else they did offline."""
    owner, pid, mid, _, _ = await _owner_with_data(api)
    caregiver = await _pair(api, owner, pid)

    own_pid = str(uuid.uuid4())
    await push(api, caregiver, profiles=[profile(own_pid, "Mine")])
    mine = str(uuid.uuid4())

    out = await push(api, caregiver, medications=[
        medication(mine, own_pid, "Legitimate"),
        medication(str(uuid.uuid4()), pid, "Not mine"),
    ])
    assert len(out["rejected"]) == 1
    got = await pull(api, caregiver)
    assert mine in {m["id"] for m in got["changes"]["medications"]}


async def test_last_write_wins_and_an_older_write_loses(api):
    owner, pid, mid, _, _ = await _owner_with_data(api)
    base = ms()

    await push(api, owner, medications=[medication(mid, pid, "Newer", at=base + 5000)])
    await push(api, owner, medications=[medication(mid, pid, "Older", at=base + 1000)])

    got = await pull(api, owner)
    med = [m for m in got["changes"]["medications"] if m["id"] == mid][0]
    assert med["name"] == "Newer"


async def test_a_status_that_goes_backwards_is_accepted(api):
    """missed can return to pending when the intake window widens, and taken can
    be undone from the calendar. Nothing may assume forward-only movement."""
    owner, pid, mid, sid, did = await _owner_with_data(api)
    base = ms()

    await push(api, owner, dose_events=[dose(did, mid, pid, sid, "missed", at=base + 1000)])
    await push(api, owner, dose_events=[dose(did, mid, pid, sid, "pending", at=base + 2000)])

    ev = [e for e in (await pull(api, owner))["changes"]["dose_events"] if e["id"] == did][0]
    assert ev["status"] == "pending"


async def test_a_soft_delete_reaches_the_other_side(api):
    owner, pid, mid, _, _ = await _owner_with_data(api)
    t = ms() + 5000

    await push(api, owner, medications=[
        {**medication(mid, pid, at=t), "deleted_at": t}
    ])

    med = [m for m in (await pull(api, owner))["changes"]["medications"] if m["id"] == mid][0]
    assert med["deleted_at"] == t


async def test_nan_is_refused_at_the_boundary(api):
    """This server is the relay. A NaN accepted from one buggy client is a NaN
    handed to every other device on the account, where SQLite stores it as NULL
    and NULL means "stock tracking off" — so it disables a feature in silence.

    Sent as raw bytes because Python's json emits a bare NaN token that a naive
    client library will happily produce.
    """
    owner, pid, _, _, _ = await _owner_with_data(api)
    body = (
        '{"changes":{"medications":[{"id":"%s","created_at":1,"updated_at":1,'
        '"profile_id":"%s","name":"X","form":"tablet","dose_amount":NaN}]}}'
        % (uuid.uuid4(), pid)
    )

    r = await api.post(
        "/v1/sync/push",
        headers={**auth_header(owner), "Content-Type": "application/json"},
        content=body.encode(),
    )
    assert r.status_code == 422
    # And the rejection itself has to be serialisable, which is the part that
    # broke: FastAPI's default response echoes the offending value, and a NaN
    # cannot be encoded — so a bad float produced a 500 rather than this.
    assert r.json()["error"]["code"] == "invalid_request"


async def test_an_unknown_enum_value_is_relayed_not_rejected(api):
    """The server mirrors; it does not adjudicate vocabulary. A value a newer
    client invented has to reach older ones, which quarantine it themselves."""
    owner, pid, mid, _, _ = await _owner_with_data(api)

    await push(api, owner, medications=[
        medication(mid, pid, at=ms() + 1000, form="inhaler_from_a_future_release")
    ])

    med = [m for m in (await pull(api, owner))["changes"]["medications"] if m["id"] == mid][0]
    assert med["form"] == "inhaler_from_a_future_release"


async def test_a_nonsense_cursor_restarts_rather_than_failing(api):
    """A device that cannot sync is worse than one that syncs too much: the
    extra rows are idempotent, a hard error is a dead end."""
    owner, pid, _, _, _ = await _owner_with_data(api)

    got = await pull(api, owner, "not-a-cursor")
    assert pid in {p["id"] for p in got["changes"]["profiles"]}


async def test_revoking_a_membership_stops_the_data(api):
    owner, pid, mid, _, _ = await _owner_with_data(api)
    caregiver = await _pair(api, owner, pid)
    assert (await pull(api, caregiver))["changes"] != {}

    await api.delete(f"/v1/profiles/{pid}/members/{caregiver['account_id']}",
                     headers=auth_header(owner))

    assert (await pull(api, caregiver))["changes"] == {}


async def test_reminder_authority_moves_and_is_visible_over_sync(api, session):
    """The load-bearing part is that it crosses in the *data*.

    A device learns it lost authority by pulling and seeing an id that is not
    its own. If that only arrived by push, a lost push would leave two phones
    ringing for the same dose — which is the invariant, not a nicety."""
    owner, pid, mid, sid, did = await _owner_with_data(api)

    from sqlalchemy import select

    from app.db.models import Device

    mine = (await session.execute(
        select(Device).where(Device.account_id == uuid.UUID(owner["account_id"]))
    )).scalars().first()

    r = await api.post(
        f"/v1/profiles/{pid}/reminder-authority",
        headers=auth_header(owner),
        json={"device_id": str(mine.id)},
    )
    assert r.status_code == 204

    got = await pull(api, owner)
    prof = [p for p in got["changes"]["profiles"] if p["id"] == pid][0]
    assert prof["owner_device_id"] == str(mine.id)


async def test_a_watcher_is_not_told_which_device_rings(api, session):
    owner, pid, mid, sid, did = await _owner_with_data(api)
    caregiver = await _pair(api, owner, pid)

    from sqlalchemy import select

    from app.db.models import Device

    mine = (await session.execute(
        select(Device).where(Device.account_id == uuid.UUID(owner["account_id"]))
    )).scalars().first()
    await api.post(f"/v1/profiles/{pid}/reminder-authority",
                   headers=auth_header(owner), json={"device_id": str(mine.id)})

    prof = (await pull(api, caregiver))["changes"]["profiles"][0]
    assert "owner_device_id" not in prof


async def test_authority_cannot_be_taken_by_someone_else(api, session):
    owner, pid, mid, sid, did = await _owner_with_data(api)
    stranger = await sign_in(api, f"stranger-{uuid.uuid4()}")

    from sqlalchemy import select

    from app.db.models import Device

    theirs = (await session.execute(
        select(Device).where(Device.account_id == uuid.UUID(stranger["account_id"]))
    )).scalars().first()

    r = await api.post(f"/v1/profiles/{pid}/reminder-authority",
                       headers=auth_header(stranger), json={"device_id": str(theirs.id)})
    assert r.status_code == 404


async def test_authority_cannot_be_set_through_sync_push(api, session):
    """Through the change stream it would be resolved by last-write-wins, and
    two devices each believing they hold it is the state the invariant exists to
    prevent."""
    owner, pid, mid, sid, did = await _owner_with_data(api)

    p = profile(pid, at=ms() + 5000)
    p["owner_device_id"] = str(uuid.uuid4())
    await push(api, owner, profiles=[p])

    prof = [x for x in (await pull(api, owner))["changes"]["profiles"] if x["id"] == pid][0]
    assert prof["owner_device_id"] is None


async def test_a_real_android_colour_survives(api):
    """0xFF2A9D8F is 4283215696 — past int32, and perfectly ordinary on a phone.
    The column was INTEGER until a generated example used a real one."""
    owner, pid, _, _, _ = await _owner_with_data(api)

    prof = [p for p in (await pull(api, owner))["changes"]["profiles"] if p["id"] == pid][0]
    assert prof["color"] == 4283215696


async def test_resending_the_same_record_changes_nothing(api):
    """The app track builds at-least-once delivery, so the same rows arrive
    again after any interruption. A resend that rewrote identical values would
    take a new server_seq and push the row out to every other device."""
    owner, pid, mid, _, _ = await _owner_with_data(api)

    first = await pull(api, owner)
    med = [m for m in first["changes"]["medications"] if m["id"] == mid][0]

    await push(api, owner, medications=[medication(mid, pid, med["name"], at=med["updated_at"])])

    assert (await pull(api, owner, first["cursor"]))["changes"] == {}


async def test_an_oversized_batch_is_refused_whole(api):
    """Truncating would leave the client believing it sent everything, and the
    missing rows are only noticed later as data that never arrived."""
    owner, pid, _, _, _ = await _owner_with_data(api)

    many = [medication(str(uuid.uuid4()), pid, f"m{i}") for i in range(1001)]
    r = await api.post("/v1/sync/push", headers=auth_header(owner),
                       json={"changes": {"medications": many}})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "batch_too_large"


async def test_the_role_tells_a_device_whether_to_ring(api):
    """Without it a watcher cannot tell a shared profile from one of its own
    that nobody has claimed, and would arm alarms for someone else's doses."""
    owner, pid, _, _, _ = await _owner_with_data(api)
    caregiver = await _pair(api, owner, pid)

    mine = [p for p in (await pull(api, owner))["changes"]["profiles"] if p["id"] == pid][0]
    theirs = [p for p in (await pull(api, caregiver))["changes"]["profiles"] if p["id"] == pid][0]

    assert mine["role"] == "owner"
    assert theirs["role"] == "with_alerts"
