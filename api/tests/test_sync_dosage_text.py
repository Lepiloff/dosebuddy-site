"""The agreed empty-label contract, through authenticated HTTP and PostgreSQL."""

import json
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text, update

from app.api import sync
from app.core.crypto import decrypt
from app.db.models import Medication, ProfileMembership, Role
from tests.conftest import auth_header, sign_in
from tests.test_sync import medication, ms, profile, pull, push, schedule

pytestmark = pytest.mark.asyncio

VARIANTS = [
    pytest.param({}, "", id="omitted"),
    pytest.param({"dosage_text": None}, "", id="null"),
    pytest.param({"dosage_text": ""}, "", id="empty"),
    pytest.param({"dosage_text": "Synthetic label"}, "Synthetic label", id="text"),
    pytest.param({"dosage_text": "  Synthetic label  "}, "  Synthetic label  ", id="spaces"),
    pytest.param({"dosage_text": "x" * 200}, "x" * 200, id="length200"),
]


def record(mid, pid, fields, at):
    row = medication(mid, pid, at=at)
    row.pop("dosage_text")
    return {**row, **fields}


async def owned(api):
    owner = await sign_in(api, f"dosage-owner-{uuid.uuid4()}")
    pid = str(uuid.uuid4())
    await push(api, owner, profiles=[profile(pid)])
    return owner, pid


async def stored(api, mid):
    # A new session observes committed storage, not the handler identity map.
    async with api.app.state.sessionmaker() as session:
        row = await session.get(Medication, uuid.UUID(mid))
        if row is None:
            return None
        raw = (await session.execute(text(
            "SELECT dosage_text FROM medications WHERE id=:id"
        ), {"id": uuid.UUID(mid)})).scalar_one()
        return {
            "label": row.dosage_text, "raw": raw, "seq": row.server_seq,
            "updated": row.updated_at_ms, "created": row.created_at_ms,
            "op": row.op_seq, "parent": str(row.profile_id), "name": row.name,
        }


@pytest.mark.parametrize("fields,expected", VARIANTS)
async def test_insert_and_winning_full_row_update_canonicalize_label(api, fields, expected):
    owner, pid = await owned(api)
    mid = str(uuid.uuid4())
    at = ms() - 1000
    await push(api, owner, medications=[record(mid, pid, fields, at)])
    inserted = await stored(api, mid)
    assert inserted["label"] == expected
    assert inserted["raw"] is not None
    assert decrypt(bytes(inserted["raw"])) == expected

    await push(api, owner, medications=[record(mid, pid, {"dosage_text": "Previous"}, at + 1)])
    winning = record(mid, pid, fields, at + 2)
    await push(api, owner, medications=[winning])
    updated = await stored(api, mid)
    assert updated["label"] == expected, "omission is a full-row clear, not a patch"
    assert decrypt(bytes(updated["raw"])) == expected
    assert updated["seq"] > inserted["seq"]
    assert updated["updated"] == at + 2

    lookup = await api.get(f"/v1/sync/records/medications/{mid}", headers=auth_header(owner))
    assert lookup.status_code == 200
    assert lookup.json()["record"]["dosage_text"] == expected
    await push(api, owner, medications=[winning])
    assert await stored(api, mid) == updated, "exact resend must not republish"


@pytest.mark.parametrize("invalid", [7, 1.5, False, [], {}, "x" * 201],
                         ids=["integer", "float", "boolean", "list", "object", "length201"])
async def test_invalid_labels_reject_whole_batch_before_insert_or_update(api, invalid):
    owner, pid = await owned(api)
    mid = str(uuid.uuid4())
    at = ms() - 1000
    await push(api, owner, medications=[record(mid, pid, {"dosage_text": "Original"}, at)])
    before = await stored(api, mid)
    for invalid_update in (False, True):
        fresh = str(uuid.uuid4())
        existing = record(mid, pid, {"dosage_text": invalid if invalid_update else "Changed"}, at + 1)
        inserted = record(fresh, pid, {"dosage_text": "Valid" if invalid_update else invalid}, at + 1)
        response = await api.post("/v1/sync/push", headers=auth_header(owner),
                                  json={"changes": {"medications": [existing, inserted]}})
        assert response.status_code == 422, response.text
        assert await stored(api, mid) == before
        assert await stored(api, fresh) is None


async def test_legacy_null_reads_do_not_mutate_or_republish_below_cursor(api):
    owner, pid = await owned(api)
    mid = str(uuid.uuid4())
    await push(api, owner, medications=[record(mid, pid, {"dosage_text": "Before"}, ms())])
    async with api.app.state.sessionmaker() as session:
        await session.execute(update(Medication).where(Medication.id == uuid.UUID(mid))
                              .values(dosage_text=None))
        await session.commit()
    before = await stored(api, mid)
    assert before["raw"] is None and before["label"] is None
    cursor = (await pull(api, owner))["cursor"]
    for path in ("/v1/sync/pull", "/v1/sync/preview", f"/v1/sync/records/medications/{mid}"):
        response = await api.get(path, headers=auth_header(owner))
        assert response.status_code == 200, response.text
        body = response.json()
        wire = body["record"] if "record" in body else body["changes"]["medications"][0]
        assert wire["dosage_text"] == ""
        assert await stored(api, mid) == before, "read compatibility must not rewrite encrypted NULL"
    assert (await pull(api, owner, cursor))["changes"] == {}


async def test_stale_tie_and_parent_rejection_cannot_clear_or_partially_mutate(api):
    owner, pid = await owned(api)
    other = str(uuid.uuid4())
    await push(api, owner, profiles=[profile(other)])
    mid = str(uuid.uuid4())
    at = ms() - 1000
    original = record(mid, pid, {"dosage_text": "Retained"}, at)
    await push(api, owner, medications=[original])
    before = await stored(api, mid)
    # Exact operation identity with changed content still loses the LWW tie.
    for candidate in ({**original, "dosage_text": None, "name": "Tie"},
                      record(mid, pid, {}, at - 1)):
        await push(api, owner, medications=[candidate])
        assert await stored(api, mid) == before
    moved = record(mid, other, {"dosage_text": None, "name": "Moved"}, at + 1)
    reply = await push(api, owner, medications=[moved])
    assert reply["rejected"] == [{"entity": "medications", "id": mid, "code": "immutable_parent"}]
    assert await stored(api, mid) == before


async def test_owner_and_watcher_actual_wire_fixture(api, monkeypatch):
    """Capture real responses optionally; no committed test requires /tmp data."""
    owner, pid = await owned(api)
    watcher = await sign_in(api, f"dosage-watcher-{uuid.uuid4()}")
    at = ms() - 1000
    variants = {}
    for variant in VARIANTS:
        fields, expected = variant.values
        mid, sid = str(uuid.uuid4()), str(uuid.uuid4())
        await push(api, owner, medications=[record(mid, pid, fields, at)],
                   schedules=[schedule(sid, mid, at=at)])
        variants[variant.id] = {"id": mid, "schedule_id": sid, "expected_api": expected}
    async with api.app.state.sessionmaker() as session:
        # Old encrypted SQL NULL is projected too, without any rewrite.
        await session.execute(update(Medication).where(
            Medication.id == uuid.UUID(variants["null"]["id"])).values(dosage_text=None))
        session.add(ProfileMembership(profile_id=uuid.UUID(pid),
                                      account_id=uuid.UUID(watcher["account_id"]), role=Role.viewer))
        await session.commit()
    fixture = {"profile_id": pid, "variants": variants, "transport": "authenticated ASGI/private PostgreSQL"}
    for label, tokens in (("owner", owner), ("watcher", watcher)):
        for mode in ("pull", "preview"):
            response = await api.get(f"/v1/sync/{mode}", headers=auth_header(tokens))
            assert response.status_code == 200, response.text
            page = response.json()
            assert len(page["changes"]["medications"]) == 6
            assert not page["has_more"]
            assert page["roles"] == {pid: "owner" if label == "owner" else "viewer"}
            assert len(page["changes"].get("schedules", [])) == (6 if label == "owner" else 0)
            for name, variant in variants.items():
                wire = next(row for row in page["changes"]["medications"] if row["id"] == variant["id"])
                if label == "owner":
                    assert wire["dosage_text"] == variant["expected_api"], name
                else:
                    assert "dosage_text" not in wire
            fixture[f"{label}_{mode}"] = page
        lookups = {}
        for name, variant in variants.items():
            response = await api.get(f"/v1/sync/records/medications/{variant['id']}", headers=auth_header(tokens))
            assert response.status_code == 200, response.text
            body = response.json()
            if label == "owner":
                assert body["record"]["dosage_text"] == variant["expected_api"]
            else:
                assert "dosage_text" not in body["record"]
            lookups[name] = body
        fixture[f"{label}_lookups"] = lookups
    monkeypatch.setattr(sync, "PAGE_SIZE", 3)
    pages, cursor = [], None
    while True:
        page = await pull(api, owner, cursor)
        pages.append(page)
        cursor = page["cursor"]
        if not page["has_more"]:
            break
        assert len(pages) < 10
    assert sum(len(page["changes"].get("schedules", [])) for page in pages) == 6
    fixture["owner_pages"] = pages
    if output := os.environ.get("H02B_FIXTURE_PATH"):
        destination = Path(output)
        assert destination.parent == Path("/tmp") and not destination.exists()
        destination.write_text(json.dumps(fixture, sort_keys=True, indent=2) + "\n")
