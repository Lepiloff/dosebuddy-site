"""Retention.

The privacy policy names a window for revoked access records. These tests are
what stops that sentence from becoming false: they check that a row past the
window goes, and — the half that actually matters — that a live membership and
a recently revoked one stay.
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import ProfileMembership, utcnow
from app.services import retention
from app.worker import sweep_if_due
from tests.conftest import auth_header, make_profile, sign_in

pytestmark = pytest.mark.asyncio


async def _shared_profile(api, session):
    """An owner, a caregiver, and a real membership between them."""
    owner = await sign_in(api, f"owner-{uuid.uuid4()}")
    caregiver = await sign_in(api, f"cg-{uuid.uuid4()}")
    profile_id = await make_profile(session, owner["account_id"], "Someone")

    issued = await api.post(
        "/v1/pairing/codes",
        headers=auth_header(owner),
        json={"profile_id": str(profile_id), "role": "with_alerts"},
    )
    assert issued.status_code == 200
    redeemed = await api.post(
        "/v1/pairing/redeem",
        headers=auth_header(caregiver),
        json={"code": issued.json()["code"]},
    )
    assert redeemed.status_code == 200
    return owner, caregiver, profile_id


async def _membership(session, profile_id) -> ProfileMembership:
    row = (
        await session.execute(
            select(ProfileMembership).where(ProfileMembership.profile_id == profile_id)
        )
    ).scalar_one()
    return row


async def test_a_membership_revoked_past_the_window_is_deleted(api, session):
    _, _, profile_id = await _shared_profile(api, session)
    membership = await _membership(session, profile_id)

    now = utcnow()
    membership.revoked_at = now - retention.REVOKED_MEMBERSHIP_TTL - timedelta(days=1)
    await session.commit()

    removed = await retention.sweep(session, now)
    await session.commit()

    assert removed == 1
    assert (
        await session.execute(
            select(ProfileMembership).where(ProfileMembership.profile_id == profile_id)
        )
    ).scalar_one_or_none() is None


async def test_a_recently_revoked_membership_is_kept(api, session):
    """The window exists to answer "who could see this, and when". Deleting
    early throws away the answer the record is kept for."""
    _, _, profile_id = await _shared_profile(api, session)
    membership = await _membership(session, profile_id)

    now = utcnow()
    membership.revoked_at = now - retention.REVOKED_MEMBERSHIP_TTL + timedelta(days=1)
    await session.commit()

    removed = await retention.sweep(session, now)
    await session.commit()

    assert removed == 0
    assert await _membership(session, profile_id) is not None


async def test_a_live_membership_is_never_swept(api, session):
    """The dangerous failure: a null `revoked_at` treated as "revoked long ago"
    would silently cut off a caregiver who still has access."""
    _, _, profile_id = await _shared_profile(api, session)
    membership = await _membership(session, profile_id)
    assert membership.revoked_at is None

    # Far enough into the future that any date comparison would fire.
    removed = await retention.sweep(session, utcnow() + timedelta(days=10_000))
    await session.commit()

    assert removed == 0
    assert await _membership(session, profile_id) is not None


class _CountingSessionmaker:
    """Counts how many sweeps actually reached a session."""

    def __init__(self):
        self.opened = 0

    def __call__(self):
        self.opened += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_args, **_kwargs):
        class _Result:
            rowcount = 0

        return _Result()

    async def commit(self):
        return None


async def test_the_sweep_runs_on_start_and_then_once_a_day():
    """A restart sweeping immediately is intended; a 60-second loop sweeping
    sixty times an hour is not."""
    sessionmaker = _CountingSessionmaker()
    start = utcnow()

    last = await sweep_if_due(sessionmaker, None, start)
    assert sessionmaker.opened == 1
    assert last == start

    last = await sweep_if_due(sessionmaker, last, start + timedelta(hours=23))
    assert sessionmaker.opened == 1
    assert last == start

    later = start + retention.SWEEP_INTERVAL
    last = await sweep_if_due(sessionmaker, last, later)
    assert sessionmaker.opened == 2
    assert last == later
