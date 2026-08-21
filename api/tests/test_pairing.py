"""Pairing.

The interesting cases are the ones where someone gets access they should not:
a code redeemed twice, a role chosen by the receiver, a profile enumerated by
guessing ids.
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import (
    AlertDelivery,
    AlertKind,
    AlertState,
    PairingCode,
    Profile,
    ProfileMembership,
    utcnow,
)
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


async def test_the_handover_queues_a_nudge_for_the_losing_device(api, session):
    """The API queues; it does not send.

    It has no FCM credentials and must not be given any — the compose file hands
    them to the worker alone, because that process does not answer requests from
    the internet. Sending from here is what made every nudge for months land in
    a log file instead of a phone.
    """
    owner, profile_id, losing, winning = await _two_devices(api, session)

    await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                   headers=auth_header(owner), json={"device_id": str(losing.id)})
    r = await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                       headers=auth_header(owner), json={"device_id": str(winning.id)})
    assert r.status_code == 200

    row = (await session.execute(
        select(AlertDelivery).where(AlertDelivery.kind == AlertKind.reminder_authority_lost)
    )).scalars().one()

    assert row.device_id == losing.id, "the nudge concerns exactly one device"
    assert row.profile_id == profile_id
    assert row.state == AlertState.pending.value
    assert row.attempts == 0

    profile = await session.get(Profile, profile_id)
    assert row.subject_id == str(profile.server_seq), (
        "the revision is the subject, so two handovers are two rows rather than "
        "one deduplicated away"
    )
    assert timedelta(minutes=55) < row.expires_at - utcnow() < timedelta(minutes=65)


async def test_claiming_authority_for_the_first_time_queues_nothing(api, session):
    """There is no previous holder to tell."""
    owner, profile_id, losing, _winning = await _two_devices(api, session)

    r = await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                       headers=auth_header(owner), json={"device_id": str(losing.id)})
    assert r.status_code == 200
    assert (await session.execute(select(AlertDelivery))).scalars().all() == []


async def test_the_claim_answers_with_the_number_it_wrote(api, session):
    """204 threw away the one thing the caller most needed.

    The revision is what a device compares late nudges against. Told nothing, it
    records nothing, and its fence stays open at whatever number it last saw — so
    a nudge issued *before* this handover can still clear it and disarm the very
    device the server has just made the owner.
    """
    owner, profile_id, first, second = await _two_devices(api, session)

    r = await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                       headers=auth_header(owner), json={"device_id": str(second.id)})
    assert r.status_code == 200

    body = r.json()
    assert body["owner_device_id"] == str(second.id)

    session.expire_all()
    profile = await session.get(Profile, profile_id)
    assert body["revision"] == profile.server_seq, "the receipt names the row that was written"


async def test_the_revision_in_the_receipt_is_a_number(api, session):
    """A string here would throw on the build that is already in the store.

    It casts this field with `as num?` — inside the sign-in path, where a
    failure of exactly this kind once hid for three days. Pull sends the same
    quantity as a string because that value also travels through FCM's
    `map<string,string>`, which has no numbers. The two disagree deliberately;
    making them agree would break a client that can no longer be changed.
    """
    owner, profile_id, first, second = await _two_devices(api, session)

    r = await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                       headers=auth_header(owner), json={"device_id": str(second.id)})
    assert isinstance(r.json()["revision"], int)


async def test_the_api_has_no_push_sender_at_all(api):
    """Structural, not behavioural, and that is the point.

    Asserting that a recorder saw nothing would pass just as well if the sender
    were still wired up and merely happened not to fire on this path. There is
    no sender: the FCM credential belongs to the worker, and an API that cannot
    send cannot quietly start sending again.
    """
    assert not hasattr(api.app.state, "push")


async def test_a_losing_device_with_no_token_is_still_queued(api, session):
    """It used to be skipped outright, which threw away the nudge for a phone
    that registers a token a minute later. The worker resolves the token when it
    sends, so the hour the row lives is an hour the device can turn up in."""
    owner, profile_id, losing, winning = await _two_devices(api, session)
    losing.push_token = None
    await session.commit()

    await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                   headers=auth_header(owner), json={"device_id": str(losing.id)})
    await api.post(f"/v1/profiles/{profile_id}/reminder-authority",
                   headers=auth_header(owner), json={"device_id": str(winning.id)})

    row = (await session.execute(
        select(AlertDelivery).where(AlertDelivery.kind == AlertKind.reminder_authority_lost)
    )).scalars().one()
    assert row.device_id == losing.id
