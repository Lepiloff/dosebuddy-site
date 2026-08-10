"""Pairing.

The interesting cases are the ones where someone gets access they should not:
a code redeemed twice, a role chosen by the receiver, a profile enumerated by
guessing ids.
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

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
    assert r.json()["detail"] == "invalid_code"


async def test_wrong_used_and_expired_are_indistinguishable(api, session):
    """Different answers would turn the endpoint into an oracle for which codes
    exist."""
    caregiver = await sign_in(api, f"cg-{uuid.uuid4()}")
    r = await api.post(
        "/v1/pairing/redeem", headers=auth_header(caregiver), json={"code": "ZZZ-999"}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_code"


async def test_ownership_cannot_be_handed_out(api, session):
    owner, profile_id = await _owner_with_profile(api, session)
    r = await api.post(
        "/v1/pairing/codes",
        headers=auth_header(owner),
        json={"profile_id": str(profile_id), "role": "owner"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "role_not_grantable"


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
