"""Sender lifetime key/token policy using the installed token_state implementation."""

import asyncio
import gc
from datetime import timedelta

import pytest
from google.auth.credentials import TokenState
from google.auth.exceptions import RefreshError

from app.services.push import Delivery
from tests.test_push_lifecycle import fcm  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("count", [5, 100])
async def test_fresh_cache_loads_refreshes_and_owns_oauth_transport_once(fcm, count):
    sender = fcm.sender()
    for index in range(count):
        assert await sender.send(f"recipient-{index}", {"type": "dose_missed"}, None, 60,
                                 clock=fcm.clock) is Delivery.ok
    assert fcm.loads == fcm.refreshes == 1
    assert len(fcm.oauth_requests) == len(fcm.oauth_sessions) == len(fcm.clients) == 1
    assert all(row[2] == "Bearer oauth-1" for row in fcm.requests)
    assert fcm.credentials[0].token_state is TokenState.FRESH
    await sender.aclose()
    await sender.aclose()
    assert fcm.oauth_sessions[0].close_calls == 1
    assert fcm.oauth_requests[0].session is None
    assert fcm.clients[0].close_calls == 1


@pytest.mark.parametrize("remaining,refresh", [(226, False), (225, True), (0.000001, True), (0, True)])
async def test_real_token_state_boundary_controls_refresh(fcm, remaining, refresh):
    sender = fcm.sender()
    await sender.send("first", {}, None, 60, clock=fcm.clock)
    creds = fcm.credentials[0]
    creds.expiry = fcm.now.replace(tzinfo=None) + timedelta(seconds=remaining)
    expected = TokenState.FRESH if remaining == 226 else (TokenState.INVALID if remaining == 0 else TokenState.STALE)
    assert creds.token_state is expected
    await sender.send("next", {}, None, 60, clock=fcm.clock)
    assert fcm.loads == 1 and fcm.refreshes == 1 + int(refresh)
    assert fcm.requests[-1][2] == f"Bearer oauth-{1 + int(refresh)}"


@pytest.mark.parametrize("token", [None, "", 7])
async def test_missing_or_malformed_cached_token_refreshes_before_post(fcm, token):
    sender = fcm.sender()
    await sender.send("first", {}, None, 60, clock=fcm.clock)
    fcm.credentials[0].token = token
    await sender.send("next", {}, None, 60, clock=fcm.clock)
    assert fcm.loads == 1 and fcm.refreshes == 2
    assert fcm.requests[-1][2] == "Bearer oauth-2"


@pytest.mark.parametrize("token", [None, "", 7])
async def test_malformed_refreshed_token_never_posts_and_next_send_can_recover(fcm, token):
    sender = fcm.sender()
    fcm.grant_token = token
    with pytest.raises(RefreshError):
        await sender.send("first", {}, None, 60, clock=fcm.clock)
    assert not fcm.requests and not fcm.clients
    assert fcm.credentials[0].token is None
    fcm.grant_token = ...
    assert await sender.send("next", {}, None, 60, clock=fcm.clock) is Delivery.ok
    assert fcm.loads == 1 and fcm.refreshes == 2


@pytest.mark.parametrize("expiry", [None, 100])
async def test_unknown_or_stale_refreshed_expiry_is_one_use_without_refresh_loop(fcm, expiry):
    sender = fcm.sender()
    fcm.grant_expiry = expiry
    for _ in range(2):
        assert await sender.send("recipient", {}, None, 60, clock=fcm.clock) is Delivery.ok
    assert fcm.loads == 1 and fcm.refreshes == 2 and len(fcm.requests) == 2
    assert len(fcm.oauth_requests) == 1
    assert [row[2] for row in fcm.requests] == ["Bearer oauth-1", "Bearer oauth-2"]


async def test_expired_refreshed_token_does_not_post(fcm):
    sender = fcm.sender()
    fcm.grant_expiry = 0
    with pytest.raises(RefreshError):
        await sender.send("recipient", {}, None, 60, clock=fcm.clock)
    assert not fcm.requests


@pytest.mark.parametrize("failure", [ValueError("refresh failed"), asyncio.CancelledError()])
async def test_refresh_escape_clears_old_token_and_keeps_key_and_request(fcm, failure):
    sender = fcm.sender()
    await sender.send("first", {}, None, 60, clock=fcm.clock)
    creds = fcm.credentials[0]
    creds.expiry = fcm.now.replace(tzinfo=None) + timedelta(seconds=225)
    fcm.refresh_error = failure
    with pytest.raises(type(failure)):
        await sender.send("next", {}, None, 60, clock=fcm.clock)
    assert creds.token is None and len(fcm.requests) == 1
    fcm.refresh_error = None
    await sender.send("recovered", {}, None, 60, clock=fcm.clock)
    assert fcm.loads == 1 and fcm.refreshes == 3 and len(fcm.oauth_requests) == 1
    assert fcm.requests[-1][2] == "Bearer oauth-3"


async def test_401_invalidates_immediately_for_next_send_without_inline_resend(fcm):
    sender = fcm.sender()
    fcm.response = 401
    assert await sender.send("first", {}, None, 60, clock=fcm.clock) is Delivery.retry
    assert len(fcm.requests) == 1 and fcm.credentials[0].token is None
    fcm.fail_refresh = True
    with pytest.raises(ValueError):
        await sender.send("second", {}, None, 60, clock=fcm.clock)
    assert len(fcm.requests) == 1 and fcm.credentials[0].token is None
    fcm.fail_refresh = False
    fcm.response = 200
    assert await sender.send("third", {}, None, 60, clock=fcm.clock) is Delivery.ok
    assert fcm.loads == 1 and fcm.refreshes == 3


@pytest.mark.parametrize("error", [FileNotFoundError("synthetic"), ValueError("synthetic parse failure")])
async def test_failed_load_is_not_cached_and_creates_no_transports(fcm, error):
    sender = fcm.sender()
    fcm.load_error = error
    with pytest.raises(type(error)):
        await sender.send("first", {}, None, 60, clock=fcm.clock)
    assert not fcm.oauth_requests and not fcm.clients and not fcm.requests
    fcm.load_error = None
    await sender.send("next", {}, None, 60, clock=fcm.clock)
    assert fcm.loads == 2 and fcm.refreshes == 1


async def test_successful_key_load_survives_file_replacement_until_new_sender(fcm):
    # This checks the mocked loader policy, not a real keyfile/rotation operation.
    first = fcm.sender()
    fcm.fail_refresh = True
    with pytest.raises(ValueError):
        await first.send("recipient", {}, None, 60, clock=fcm.clock)
    fcm.key_version = "key-b"
    fcm.load_error = FileNotFoundError("file replaced or removed")
    fcm.fail_refresh = False
    await first.send("recipient", {}, None, 60, clock=fcm.clock)
    assert fcm.loads == 1 and fcm.credentials[0].key_version == "key-a"
    await first.aclose()
    second = fcm.sender()
    with pytest.raises(FileNotFoundError):
        await second.send("recipient", {}, None, 60, clock=fcm.clock)
    fcm.load_error = None
    await second.send("recipient", {}, None, 60, clock=fcm.clock)
    assert [creds.key_version for creds in fcm.credentials] == ["key-a", "key-b"]


@pytest.mark.parametrize("where,failure", [("oauth", RuntimeError("OAuth close")),
                                          ("oauth", asyncio.CancelledError()),
                                          ("http", RuntimeError("HTTP close")),
                                          ("http", asyncio.CancelledError())])
async def test_both_transports_are_closed_once_even_when_one_close_escapes(fcm, where, failure):
    sender = fcm.sender()
    await sender.send("recipient", {}, None, 60, clock=fcm.clock)
    if where == "oauth":
        fcm.oauth_close_error = failure
    else:
        fcm.close_error = failure
    with pytest.raises(type(failure)):
        await sender.aclose()
    await sender.aclose()
    assert fcm.oauth_sessions[0].close_calls == 1 and fcm.oauth_requests[0].session is None
    assert fcm.clients[0].close_calls == 1
    fcm.oauth_requests.clear()
    gc.collect()
    assert fcm.oauth_sessions[0].close_calls == 1


async def test_unused_close_and_refresh_failure_cleanup_create_no_extra_resources(fcm):
    unused = fcm.sender()
    await unused.aclose()
    assert not fcm.oauth_requests and not fcm.clients and not fcm.credentials
    sender = fcm.sender()
    fcm.fail_refresh = True
    with pytest.raises(ValueError):
        await sender.send("recipient", {}, None, 60, clock=fcm.clock)
    await sender.aclose()
    assert len(fcm.oauth_sessions) == 1 and fcm.oauth_sessions[0].close_calls == 1
    assert fcm.oauth_requests[0].session is None and not fcm.clients


async def test_oauth_request_construction_failure_keeps_loaded_key_and_recovers(fcm):
    sender = fcm.sender()
    fcm.request_error = ValueError("synthetic Request failure")
    with pytest.raises(ValueError, match="Request failure"):
        await sender.send("recipient", {}, None, 60, clock=fcm.clock)
    assert fcm.loads == 1 and fcm.refreshes == 0 and not fcm.oauth_sessions and not fcm.clients
    fcm.request_error = None
    await sender.send("recipient", {}, None, 60, clock=fcm.clock)
    assert fcm.loads == fcm.refreshes == len(fcm.oauth_sessions) == 1


async def test_token_expiring_during_client_setup_is_rejected_at_post_boundary(fcm):
    sender = fcm.sender()
    fcm.grant_expiry, fcm.client_wait = 1, 2
    with pytest.raises(RefreshError):
        await sender.send("recipient", {}, None, 60, clock=fcm.clock)
    assert len(fcm.clients) == 1 and not fcm.requests
    assert fcm.credentials[0].token is None


async def test_cached_token_does_not_renew_delivery_window_or_spend_skipped_refresh_wait(fcm):
    sender = fcm.sender()
    deadline = fcm.now + timedelta(seconds=60)
    await sender.send("recipient", {}, None, 60, expires_at=deadline, clock=fcm.clock)
    fcm.advance(58)
    fcm.refresh_wait = 100
    assert await sender.send("recipient", {}, None, 60, expires_at=deadline, clock=fcm.clock) is Delivery.ok
    assert fcm.requests[-1][1]["android"]["ttl"] == "2s" and fcm.refreshes == 1
    fcm.advance(1.5)
    assert await sender.send("recipient", {}, None, 60, expires_at=deadline, clock=fcm.clock) is Delivery.expired
    assert len(fcm.requests) == 2 and fcm.refreshes == fcm.loads == 1
