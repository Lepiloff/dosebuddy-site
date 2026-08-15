"""Auth, with the failure paths weighted more heavily than the happy one.

Signing in working is the easy half. What protects someone else's health data is
what happens when a token is stolen, replayed, or presented after the device was
unlinked.
"""

import uuid
from datetime import datetime, timezone

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
    assert replay.json()["error"]["code"] == "refresh_token_reused"

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
    assert r.json()["error"]["code"] == "device_revoked"


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


async def test_sign_in_says_so_when_google_is_not_configured(api):
    """An unset GOOGLE_CLIENT_ID means there is no audience to verify against.
    Answer plainly instead of throwing: this is the state a fresh deployment is
    in until the OAuth client exists, and a 500 there sends someone hunting for
    a bug that is not one."""
    api.app.state.google_verifier = None

    r = await api.post(
        "/v1/auth/google",
        json={"id_token": "x", "device": {"id": str(uuid.uuid4()), "platform": "android"}},
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "google_sign_in_not_configured"


async def test_the_real_verifier_wraps_failures_rather_than_leaking_them(api):
    """Constructs the real verifier, not a stand-in.

    The stand-in cannot catch a missing dependency, and that is exactly what got
    through once: google.auth.transport.requests needs the `requests` package,
    google-auth does not pull it in, and the import sat inside verify() — so it
    surfaced as a 500 on the first real sign-in rather than at startup. This
    exercises the import and the exception wrapping without any network: a
    malformed token fails while being parsed.
    """
    from app.services.google import InvalidGoogleToken, RealGoogleVerifier

    verifier = RealGoogleVerifier("some-client-id.apps.googleusercontent.com")
    with pytest.raises(InvalidGoogleToken):
        verifier.verify("this-is-not-a-jwt")


async def test_a_rejected_token_is_explained_in_the_log_not_the_response(api, caplog):
    """The caller learns only that it was rejected — telling them whether it
    expired or had the wrong audience tells an attacker the same. The reason has
    to be somewhere, though, or the first integration failure is undebuggable
    from both ends at once."""
    import logging

    with caplog.at_level(logging.WARNING):
        r = await api.post(
            "/v1/auth/google",
            json={"id_token": "never-issued",
                  "device": {"id": str(uuid.uuid4()), "platform": "android"}},
        )

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_google_token"
    assert "expired" not in r.text and "audience" not in r.text

    assert any("auth.google_rejected" in rec.getMessage() for rec in caplog.records)


async def test_a_token_never_reaches_the_log():
    """An exception message that echoes the credential would put a live token in
    a file that outlives it."""
    from app.core.observability import redact

    jwt = "eyJhbGciOiJSUzI1NiIsImtpZCI6IngifQ.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlX2hlcmU"
    cleaned = redact(f"Token has wrong audience {jwt}, expected other")
    assert jwt not in cleaned
    assert "<token>" in cleaned


# ---------------------------------------------------------------------------
# The background sync token
# ---------------------------------------------------------------------------


async def _sync_token(api, session, pair) -> str:
    """Issue one the way the app does: from the foreground, with a full token."""
    from sqlalchemy import select as sa_select

    from app.db.models import Device

    device = (
        await session.execute(
            sa_select(Device).where(Device.account_id == uuid.UUID(pair["account_id"]))
        )
    ).scalars().first()
    r = await api.post(
        f"/v1/devices/{device.id}/sync-token", headers=auth_header(pair)
    )
    assert r.status_code == 200, r.text
    return r.json()["sync_token"]


async def test_a_sync_token_can_sync(api, session):
    """The whole reason it exists: the background half of the app can send.

    Confirmations from a notification and doses that lapsed while their owner
    slept used to sit on the phone until somebody opened the app, because the
    background worker had no credential it could safely use.
    """
    pair = await sign_in(api, f"owner-{uuid.uuid4()}")
    token = await _sync_token(api, session, pair)
    headers = {"Authorization": f"Bearer {token}"}

    assert (await api.get("/v1/sync/pull", headers=headers)).status_code == 200
    assert (
        await api.post("/v1/sync/push", headers=headers, json={"changes": {}})
    ).status_code == 200


async def test_a_sync_token_can_do_nothing_else(api, session):
    """The line the whole design rests on.

    It is held for ninety days and never rotates, so it has to be worth little
    if copied. Reaching any of these would make a leaked token into somebody
    else's phone ringing — or into nobody's.
    """
    pair = await sign_in(api, f"owner-{uuid.uuid4()}")
    token = await _sync_token(api, session, pair)
    headers = {"Authorization": f"Bearer {token}"}
    some_id = str(uuid.uuid4())

    forbidden = [
        ("post", "/v1/pairing/codes", {"profile_id": some_id, "role": "viewer"}),
        ("post", "/v1/pairing/redeem", {"code": "AAA-BBB"}),
        ("post", f"/v1/profiles/{some_id}/reminder-authority", {"device_id": some_id}),
        ("get", f"/v1/profiles/{some_id}/members", None),
        ("delete", f"/v1/profiles/{some_id}/members/{some_id}", None),
        ("put", f"/v1/devices/{some_id}/push-token", {"fcm_token": "x"}),
        ("post", f"/v1/devices/{some_id}/heartbeat", None),
        ("delete", "/v1/account", None),
        # Above all: it must not be able to mint another one.
        ("post", f"/v1/devices/{some_id}/sync-token", None),
    ]

    for method, path, body in forbidden:
        call = getattr(api, method)
        r = await call(path, headers=headers, json=body) if body else await call(
            path, headers=headers
        )
        assert r.status_code == 403, f"{method.upper()} {path} answered {r.status_code}"
        assert r.json()["error"]["code"] == "token_scope_insufficient"


async def test_unlinking_the_device_kills_the_sync_token_at_once(api, session):
    """Ninety days of life, revocable the moment the device is unlinked.

    This is what makes a long-lived signed token acceptable without storing it:
    the device row is re-read on every request, so revocation needs no list of
    outstanding tokens to hunt through.
    """
    from sqlalchemy import select as sa_select

    from app.db.models import Device

    pair = await sign_in(api, f"owner-{uuid.uuid4()}")
    token = await _sync_token(api, session, pair)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await api.get("/v1/sync/pull", headers=headers)).status_code == 200

    device = (
        await session.execute(
            sa_select(Device).where(Device.account_id == uuid.UUID(pair["account_id"]))
        )
    ).scalars().first()
    device.revoked_at = datetime.now(timezone.utc)
    await session.commit()

    r = await api.get("/v1/sync/pull", headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "device_revoked"


async def test_an_ordinary_token_still_syncs(api, session):
    """The foreground has no reason to hold a second credential to do what it
    was already doing."""
    pair = await sign_in(api, f"owner-{uuid.uuid4()}")
    assert (await api.get("/v1/sync/pull", headers=auth_header(pair))).status_code == 200


async def test_a_sync_token_is_refused_for_another_account_device(api, session):
    """Asking for a token against somebody else's device is a 404, not a token."""
    from sqlalchemy import select as sa_select

    from app.db.models import Device

    mine = await sign_in(api, f"owner-{uuid.uuid4()}")
    theirs = await sign_in(api, f"other-{uuid.uuid4()}")
    their_device = (
        await session.execute(
            sa_select(Device).where(Device.account_id == uuid.UUID(theirs["account_id"]))
        )
    ).scalars().first()

    r = await api.post(
        f"/v1/devices/{their_device.id}/sync-token", headers=auth_header(mine)
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# What signing out has to reach
# ---------------------------------------------------------------------------


async def test_signing_out_kills_the_sync_token(api, session):
    """Wiping the app's copy is not revocation.

    The sync token lives ninety days and does not rotate, so a copy taken before
    someone signed out kept full read access to their account for the rest of
    its life. The check that refuses it exists in `deps`; until this, nothing in
    the codebase ever set the flag it reads.
    """
    pair = await sign_in(api, f"out-{uuid.uuid4()}")
    token = await _sync_token(api, session, pair)
    header = {"Authorization": f"Bearer {token}"}

    assert (await api.get("/v1/sync/pull", headers=header)).status_code == 200

    assert (await api.post("/v1/auth/logout", headers=auth_header(pair))).status_code == 204

    r = await api.get("/v1/sync/pull", headers=header)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "device_revoked"


async def test_signing_out_stops_the_alerts(api, session):
    """A signed-out phone went on receiving alerts about someone's doses.

    Article 9 data arriving at a device whose user deliberately left the
    account. The app can decline to show it, and now does, but the server had no
    business sending it.
    """
    from sqlalchemy import select as sa_select

    from app.db.models import Device

    pair = await sign_in(api, f"push-{uuid.uuid4()}")
    device = (
        await session.execute(
            sa_select(Device).where(Device.account_id == uuid.UUID(pair["account_id"]))
        )
    ).scalars().first()
    await api.put(f"/v1/devices/{device.id}/push-token",
                  headers=auth_header(pair), json={"fcm_token": "a-live-token"})

    await api.post("/v1/auth/logout", headers=auth_header(pair))

    await session.refresh(device)
    assert device.push_token is None
    assert device.revoked_at is not None


async def test_signing_back_in_restores_the_device(api, session):
    """Revoking on sign-out must not lock anyone out of their own account."""
    from sqlalchemy import select as sa_select

    from app.db.models import Device

    subject = f"back-{uuid.uuid4()}"
    pair = await sign_in(api, subject)
    device_id = (
        await session.execute(
            sa_select(Device.id).where(Device.account_id == uuid.UUID(pair["account_id"]))
        )
    ).scalars().first()
    await api.post("/v1/auth/logout", headers=auth_header(pair))

    token = f"google-token-{subject}"
    api.app.state.google_verifier.add(token, subject, None)
    r = await api.post("/v1/auth/google", json={
        "id_token": token,
        "device": {"id": str(device_id), "platform": "android", "app_version": "1.1.0"},
    })
    assert r.status_code == 200, r.text
    assert (await api.get("/v1/sync/pull", headers=auth_header(r.json()))).status_code == 200
