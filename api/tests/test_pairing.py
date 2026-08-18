"""Pairing.

The interesting cases are the ones where someone gets access they should not:
a code redeemed twice, a role chosen by the receiver, a profile enumerated by
guessing ids.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.pairing import NUDGE_TTL
from app.db.models import PairingCode, ProfileMembership, utcnow
from tests.conftest import auth_header, make_profile, sign_in

pytestmark = pytest.mark.asyncio


async def _owner_with_profile(api, session):
    owner = await sign_in(api, f"owner-{uuid.uuid4()}")
    profile_id = await make_profile(session, owner["account_id"], "Someone")
    return owner, profile_id


async def test_owner_issues_a_code_and_a_caregiver_redeems_it(api, session):
    owner, profile_id = await _owner_with_profile(api, session)
    caregiver = await sign_in(api, f"cg-{uuid.uuid4()}")

    issued = await api.post(
        "/v1/pairing/codes",
        headers=auth_header(owner),
        json={"profile_id": str(profile_id), "role": "with_alerts"},
    )
    assert issued.status_code == 200
    code = issued.json()["code"]

    redeemed = await api.post(
        "/v1/pairing/redeem", headers=auth_header(caregiver), json={"code": code}
    )
    assert redeemed.status_code == 200
    assert redeemed.json()["role"] == "with_alerts"
    assert redeemed.json()["name"] == "Someone"


async def test_the_role_comes_from_the_code_not_the_redeemer(api, session):
    """Otherwise the receiving side decides how much it gets to see."""
    owner, profile_id = await _owner_with_profile(api, session)
    caregiver = await sign_in(api, f"cg-{uuid.uuid4()}")

    code = (
        await api.post(
            "/v1/pairing/codes",
            headers=auth_header(owner),
            json={"profile_id": str(profile_id), "role": "viewer"},
        )
    ).json()["code"]

    r = await api.post(
        "/v1/pairing/redeem",
        headers=auth_header(caregiver),
        json={"code": code, "role": "with_alerts"},
    )
    assert r.json()["role"] == "viewer"


async def test_a_code_works_once(api, session):
    owner, profile_id = await _owner_with_profile(api, session)
    first = await sign_in(api, f"cg1-{uuid.uuid4()}")
    second = await sign_in(api, f"cg2-{uuid.uuid4()}")

    code = (
        await api.post(
            "/v1/pairing/codes",
            headers=auth_header(owner),
            json={"profile_id": str(profile_id), "role": "viewer"},
        )
    ).json()["code"]

    assert (
        await api.post("/v1/pairing/redeem", headers=auth_header(first), json={"code": code})
    ).status_code == 200
    assert (
        await api.post("/v1/pairing/redeem", headers=auth_header(second), json={"code": code})
    ).status_code == 400


async def test_an_expired_code_is_refused(api, session):
    owner, profile_id = await _owner_with_profile(api, session)
    caregiver = await sign_in(api, f"cg-{uuid.uuid4()}")

    code = (
        await api.post(
            "/v1/pairing/codes",
            headers=auth_header(owner),
            json={"profile_id": str(profile_id), "role": "viewer"},
        )
    ).json()["code"]

    row = (await session.execute(select(PairingCode))).scalars().first()
    row.expires_at = utcnow() - timedelta(seconds=1)
    await session.commit()

    r = await api.post("/v1/pairing/redeem", headers=auth_header(caregiver), json={"code": code})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_code"


async def test_wrong_used_and_expired_are_indistinguishable(api, session):
    """Different answers would turn the endpoint into an oracle for which codes
    exist."""
    caregiver = await sign_in(api, f"cg-{uuid.uuid4()}")
    r = await api.post(
        "/v1/pairing/redeem", headers=auth_header(caregiver), json={"code": "ZZZ-999"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_code"


async def test_ownership_cannot_be_handed_out(api, session):
    owner, profile_id = await _owner_with_profile(api, session)
    r = await api.post(
        "/v1/pairing/codes",
        headers=auth_header(owner),
        json={"profile_id": str(profile_id), "role": "owner"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "role_not_grantable"


async def test_someone_elses_profile_is_a_404_not_a_403(api, session):
    """A 403 confirms the id names a real profile, which is enough to enumerate
    other people's."""
    _, profile_id = await _owner_with_profile(api, session)
    stranger = await sign_in(api, f"stranger-{uuid.uuid4()}")

    r = await api.post(
        "/v1/pairing/codes",
        headers=auth_header(stranger),
        json={"profile_id": str(profile_id), "role": "viewer"},
    )
    assert r.status_code == 404


async def test_redeem_attempts_are_throttled(api, session):
    caregiver = await sign_in(api, f"cg-{uuid.uuid4()}")
    for _ in range(10):
        await api.post(
            "/v1/pairing/redeem", headers=auth_header(caregiver), json={"code": "AAA-111"}
        )

    r = await api.post(
        "/v1/pairing/redeem", headers=auth_header(caregiver), json={"code": "AAA-111"}
    )
    assert r.status_code == 429


async def test_members_are_listed_and_revoked_by_the_owner(api, session):
    owner, profile_id = await _owner_with_profile(api, session)
    caregiver = await sign_in(api, f"cg-{uuid.uuid4()}")

    code = (
        await api.post(
            "/v1/pairing/codes",
            headers=auth_header(owner),
            json={"profile_id": str(profile_id), "role": "viewer"},
        )
    ).json()["code"]
    await api.post("/v1/pairing/redeem", headers=auth_header(caregiver), json={"code": code})

    members = await api.get(f"/v1/profiles/{profile_id}/members", headers=auth_header(owner))
    assert len(members.json()) == 1

    revoked = await api.delete(
        f"/v1/profiles/{profile_id}/members/{caregiver['account_id']}",
        headers=auth_header(owner),
    )
    assert revoked.status_code == 204

    after = await api.get(f"/v1/profiles/{profile_id}/members", headers=auth_header(owner))
    assert after.json() == []


async def test_revocation_keeps_the_history(api, session):
    """Who could see what, and when, has to stay answerable."""
    owner, profile_id = await _owner_with_profile(api, session)
    caregiver = await sign_in(api, f"cg-{uuid.uuid4()}")
    code = (
        await api.post(
            "/v1/pairing/codes",
            headers=auth_header(owner),
            json={"profile_id": str(profile_id), "role": "viewer"},
        )
    ).json()["code"]
    await api.post("/v1/pairing/redeem", headers=auth_header(caregiver), json={"code": code})
    await api.delete(
        f"/v1/profiles/{profile_id}/members/{caregiver['account_id']}",
        headers=auth_header(owner),
    )

    rows = (await session.execute(select(ProfileMembership))).scalars().all()
    assert len(rows) == 1
    assert rows[0].revoked_at is not None


async def test_the_profile_name_is_not_stored_in_the_clear(api, session):
    owner, profile_id = await _owner_with_profile(api, session)
    raw = (
        await session.execute(
            select(ProfileMembership).where(ProfileMembership.profile_id == profile_id)
        )
    ).scalars().all()
    del raw, owner

    row = (
        await session.execute(
            __import__("sqlalchemy").text(
                "SELECT name FROM profiles WHERE id = :id"
            ).bindparams(id=profile_id)
        )
    ).scalar_one()
    assert b"Someone" not in bytes(row)


async def _two_devices(api, session):
    """One account, two handsets — the situation the handover exists for."""
    from app.db.models import Device

    subject = f"owner-{uuid.uuid4()}"
    owner = await sign_in(api, subject)
    await sign_in(api, subject)

    profile_id = await make_profile(session, owner["account_id"], "Someone")
    devices = (await session.execute(
        select(Device).where(Device.account_id == uuid.UUID(owner["account_id"]))
        .order_by(Device.created_at)
    )).scalars().all()
    devices[0].push_token = "token-losing-device"
    await session.commit()
    return owner, profile_id, devices[0], devices[1]


async def test_the_authority_nudge_carries_what_the_receiver_checks(api, session):
    """Every field the receiving device reads has to actually be sent.

    Its guard drops a nudge that names the receiver itself as the new owner, one
    whose revision is not strictly newer, and one that has expired. All three
    were written and covered by tests on the device, and all three were dead:
    the payload was `type` and `profile_id` and nothing else, so the checks read
    absent fields on the rare occasions a nudge was sent at all.
    """
    from tests.test_alerts import RecordingPush

    owner, profile_id, losing, winning = await _two_devices(api, session)

    await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                   headers=auth_header(owner), json={"device_id": str(losing.id)})

    pusher = RecordingPush()
    api.app.state.push = pusher

    r = await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                       headers=auth_header(owner), json={"device_id": str(winning.id)})
    assert r.status_code == 204

    assert len(pusher.sent) == 1
    token, data = pusher.sent[0]
    assert token == "token-losing-device"
    assert data["type"] == "reminder_authority_lost"
    assert data["profile_id"] == str(profile_id)
    assert data["owner_device_id"] == str(winning.id)
    assert int(data["revision"]) > 0

    # FCM's `data` is map<string,string>: a value sent as an int arrives as a
    # string anyway. Asserting it here means the sending side says what the
    # receiving side will see, rather than what Python happened to hold.
    assert all(isinstance(v, str) for v in data.values()), data

    expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    assert expires.tzinfo is not None, "a naive timestamp is not a moment in time"
    # Level with the TTL handed to FCM, because they answer the same question
    # from opposite ends.
    assert pusher.ttl == [NUDGE_TTL]
    assert abs((expires - utcnow()).total_seconds() - NUDGE_TTL) < 60


async def test_the_nudge_names_the_winner_not_the_loser(api, session):
    """The receiver drops a nudge that names the receiver as the new owner —
    that shape means "someone else lost it", not "you did". Sending the losing
    device's own id would have every nudge correctly discarded."""
    from tests.test_alerts import RecordingPush

    owner, profile_id, losing, winning = await _two_devices(api, session)
    await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                   headers=auth_header(owner), json={"device_id": str(losing.id)})

    pusher = RecordingPush()
    api.app.state.push = pusher
    await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                   headers=auth_header(owner), json={"device_id": str(winning.id)})

    _token, data = pusher.sent[0]
    assert data["owner_device_id"] != str(losing.id)


async def test_a_handover_to_a_device_with_no_token_sends_nothing(api, session):
    """Not an error. The device simply cannot be reached, and the pull still
    tells it the truth."""
    from tests.test_alerts import RecordingPush

    owner, profile_id, losing, winning = await _two_devices(api, session)
    losing.push_token = None
    await session.commit()

    await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                   headers=auth_header(owner), json={"device_id": str(losing.id)})

    pusher = RecordingPush()
    api.app.state.push = pusher
    r = await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                       headers=auth_header(owner), json={"device_id": str(winning.id)})

    assert r.status_code == 204
    assert pusher.sent == []
