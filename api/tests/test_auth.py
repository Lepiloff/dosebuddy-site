"""Auth, with the failure paths weighted more heavily than the happy one.

Signing in working is the easy half. What protects someone else's health data is
what happens when a token is stolen, replayed, or presented after the device was
unlinked.
"""

import uuid

import pytest
from sqlalchemy import select, text

from app.db.models import Account, Device, RefreshToken
from tests.conftest import auth_header, sign_in

pytestmark = pytest.mark.asyncio


async def test_first_sign_in_creates_the_account(api, session):
    pair = await sign_in(api, "google-sub-1", "a@example.com")

    account = (
        await session.execute(select(Account).where(Account.google_sub == "google-sub-1"))
    ).scalar_one()
    assert str(account.id) == pair["account_id"]


async def test_identity_is_the_subject_not_the_email(api, session):
    """Emails change. Keying on one would read, eventually, as losing access to
    your own data."""
    first = await sign_in(api, "stable-sub", "old@example.com")

    api.app.state.google_verifier.add("second", "stable-sub", "new@example.com")
    r = await api.post(
        "/v1/auth/google",
        json={
            "id_token": "second",
            "device": {"id": str(uuid.uuid4()), "platform": "android"},
        },
    )

    assert r.status_code == 200
    assert r.json()["account_id"] == first["account_id"]


async def test_email_is_not_stored_in_the_clear(api, session):
    await sign_in(api, "sub-enc", "secret@example.com")

    # Through the ORM the column decrypts, so reading it that way proves
    # nothing about what is on disk. Raw SQL bypasses the type decorator, which
    # is the only way to see the bytes a leaked dump would contain.
    raw = (
        await session.execute(
            text("SELECT email FROM accounts WHERE google_sub = 'sub-enc'")
        )
    ).scalar_one()
    assert b"secret@example.com" not in bytes(raw)
    assert bytes(raw)[0] == 1  # scheme version, so a later scheme is tellable apart

    through_orm = (
        await session.execute(select(Account).where(Account.google_sub == "sub-enc"))
    ).scalar_one()
    assert through_orm.email == "secret@example.com"


async def test_a_token_google_does_not_recognise_is_rejected(api):
    r = await api.post(
        "/v1/auth/google",
        json={
            "id_token": "never-issued",
            "device": {"id": str(uuid.uuid4()), "platform": "android"},
        },
    )
    assert r.status_code == 401


async def test_refresh_rotates_the_token(api):
    pair = await sign_in(api, "sub-rotate")

    r = await api.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 200
    assert r.json()["refresh_token"] != pair["refresh_token"]


async def test_replaying_a_rotated_token_kills_the_whole_device_session(api, session):
    """A rotated token turning up again means two copies exist and one of them
    is not the user's. Revoking only the replayed one would leave the thief with
    the newer token."""
    pair = await sign_in(api, "sub-replay")
    fresh = (
        await api.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    ).json()

    replay = await api.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert replay.status_code == 401
    assert replay.json()["detail"] == "refresh_token_reused"

    # The token handed out a moment ago is dead too.
    after = await api.post("/v1/auth/refresh", json={"refresh_token": fresh["refresh_token"]})
    assert after.status_code == 401


async def test_logout_revokes_refresh_but_leaves_the_access_token_alone(api):
    pair = await sign_in(api, "sub-logout")

    assert (await api.post("/v1/auth/logout", headers=auth_header(pair))).status_code == 204

    r = await api.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 401


async def test_a_revoked_device_is_refused_on_every_request(api, session):
    """Checked per request rather than trusted from the token: fifteen minutes
    of continued access to someone else's health data is fifteen too many."""
    pair = await sign_in(api, "sub-revoked")

    device = (await session.execute(select(Device))).scalars().first()
    from app.db.models import utcnow

    device.revoked_at = utcnow()
    await session.commit()

    r = await api.post("/v1/auth/logout", headers=auth_header(pair))
    assert r.status_code == 401
    assert r.json()["detail"] == "device_revoked"


async def test_the_same_device_id_under_another_account_is_refused(api):
    first = await sign_in(api, "sub-a")
    device_id = str(uuid.uuid4())
    api.app.state.google_verifier.add("t-a", "sub-a")
    await api.post(
        "/v1/auth/google",
        json={"id_token": "t-a", "device": {"id": device_id, "platform": "android"}},
    )

    api.app.state.google_verifier.add("t-b", "sub-b")
    r = await api.post(
        "/v1/auth/google",
        json={"id_token": "t-b", "device": {"id": device_id, "platform": "android"}},
    )
    assert r.status_code == 409
    del first


async def test_no_token_is_rejected(api):
    assert (await api.post("/v1/auth/logout")).status_code == 401


async def test_account_deletion_removes_the_account(api, session):
    pair = await sign_in(api, "sub-delete")

    assert (await api.delete("/v1/account", headers=auth_header(pair))).status_code == 204

    remaining = (
        await session.execute(select(Account).where(Account.google_sub == "sub-delete"))
    ).scalar_one_or_none()
    assert remaining is None


async def test_tokens_die_with_the_account(api, session):
    pair = await sign_in(api, "sub-delete-tokens")
    await api.delete("/v1/account", headers=auth_header(pair))

    left = (await session.execute(select(RefreshToken))).scalars().all()
    assert left == []
