"""Real FcmPush/httpx lifecycle with synthetic OAuth and no external HTTP."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.services import push as push_service
from app.services.push import Delivery, FcmPush, LoggingPush

pytestmark = pytest.mark.asyncio
START = datetime(2026, 10, 15, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def fcm(monkeypatch):
    state = SimpleNamespace(
        now=START, loads=0, refreshes=0, refresh_wait=0, client_wait=0,
        fail_refresh=False, response=200, responses=[], requests=[], clients=[], senders=[],
        close_error=False, block=False, entered=asyncio.Event(), release=asyncio.Event(),
    )

    def advance(seconds):
        state.now += timedelta(seconds=seconds)

    state.advance = advance
    state.clock = lambda: state.now
    marker = object()

    class Credentials:
        @classmethod
        def from_service_account_file(cls, path, *, scopes):
            assert path == ":synthetic:"
            assert scopes == ["https://www.googleapis.com/auth/firebase.messaging"]
            state.loads += 1
            return cls()

        def refresh(self, request):
            assert request is marker
            state.refreshes += 1
            advance(state.refresh_wait)
            if state.fail_refresh:
                raise ValueError("synthetic refresh failure")
            self.token = f"oauth-{state.refreshes}"

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 10}
            self.close_calls = 0

            async def accept(request):
                assert request.method == "POST"
                assert str(request.url) == "https://fcm.googleapis.com/v1/projects/synthetic/messages:send"
                assert request.headers["content-type"] == "application/json"
                state.requests.append((
                    self, json.loads(request.content)["message"], request.headers["authorization"],
                ))
                if state.block:
                    state.entered.set()
                    await state.release.wait()
                response = state.responses.pop(0) if state.responses else state.response
                if isinstance(response, BaseException):
                    raise response
                return httpx.Response(response, json={})

            super().__init__(transport=httpx.MockTransport(accept), trust_env=False, verify=False, **kwargs)
            advance(state.client_wait)
            state.clients.append(self)

        async def aclose(self):
            self.close_calls += 1
            await super().aclose()
            if state.close_error:
                raise RuntimeError("synthetic close failure")

    monkeypatch.setattr(push_service, "service_account", SimpleNamespace(Credentials=Credentials))
    monkeypatch.setattr(push_service, "GoogleRequest", lambda: marker)
    monkeypatch.setattr(push_service, "httpx", SimpleNamespace(AsyncClient=Client, HTTPError=httpx.HTTPError))

    def sender():
        value = FcmPush("synthetic", ":synthetic:")
        state.senders.append(value)
        return value

    state.sender = sender
    yield state
    for value in state.senders:
        await value.aclose()
    assert all(client.is_closed and client.close_calls == 1 for client in state.clients)


@pytest.mark.parametrize("count", [5, 100])
async def test_one_lazy_client_keeps_every_payload_recipient_and_fresh_authorization(fcm, count):
    sender = fcm.sender()
    assert not fcm.clients and fcm.loads == 0
    for i in range(count):
        authority = i % 2 == 1
        data = {"type": "reminder_authority_lost" if authority else "dose_missed",
                "account_id": f"account-{i % 2}", "profile_id": f"profile-{i}"}
        if authority:
            data.update(owner_device_id=f"owner-{i}", revision=str(i), expires_at="2026-10-15T00:01:00Z")
        else:
            data["subject_id"] = f"dose-{i}"
        collapse = f"reminder_authority_lost:profile-{i}" if authority else None
        assert await sender.send(f"recipient-{i % 3}", data, collapse, 60, clock=fcm.clock) is Delivery.ok
        client, message, authorization = fcm.requests[-1]
        android = {"priority": "high", "ttl": "60s"}
        if authority:
            android["collapse_key"] = collapse
        assert message == {"token": f"recipient-{i % 3}", "data": data, "android": android}
        assert authorization == f"Bearer oauth-{i + 1}"
        assert "authorization" not in client.headers
    assert len(fcm.clients) == 1 and len(fcm.requests) == count
    assert fcm.loads == fcm.refreshes == count
    assert not fcm.clients[0].is_closed
    await sender.aclose()
    await sender.aclose()
    assert fcm.clients[0].close_calls == 1


@pytest.mark.parametrize("status,outcome", [(200, Delivery.ok), (400, Delivery.gone),
                                           (404, Delivery.gone), (401, Delivery.retry),
                                           (429, Delivery.retry), (503, Delivery.retry)])
async def test_status_outcomes_do_not_replace_client_or_retry_internally(fcm, status, outcome):
    sender = fcm.sender()
    fcm.response = status
    assert await sender.send("first", {"type": "dose_missed"}, None, 60, clock=fcm.clock) is outcome
    assert len(fcm.requests) == 1
    fcm.response = 200
    assert await sender.send("next", {"type": "profile_stale"}, None, 60, clock=fcm.clock) is Delivery.ok
    assert len(fcm.clients) == 1 and fcm.loads == fcm.refreshes == 2
    assert fcm.requests[1][1]["token"] == "next"
    assert fcm.requests[1][2] == "Bearer oauth-2"


async def test_transport_failure_reuses_client_and_original_deadline(fcm):
    sender = fcm.sender()
    deadline = START + timedelta(seconds=60)
    data = {"type": "dose_missed", "subject_id": "same-event"}
    fcm.response = httpx.ConnectError("synthetic failure")
    assert await sender.send("recipient", data, None, 60, expires_at=deadline, clock=fcm.clock) is Delivery.retry
    fcm.advance(7)
    fcm.response = 200
    assert await sender.send("recipient", data, None, 60, expires_at=deadline, clock=fcm.clock) is Delivery.ok
    assert len(fcm.clients) == 1 and len(fcm.requests) == 2
    assert [r[1]["android"]["ttl"] for r in fcm.requests] == ["60s", "53s"]
    assert all(r[1]["data"] == data for r in fcm.requests)


@pytest.mark.parametrize("warm", [False, True])
async def test_refresh_failure_propagates_without_post_and_next_send_recovers(fcm, warm):
    sender = fcm.sender()
    if warm:
        await sender.send("recipient", {}, None, 60, clock=fcm.clock)
    fcm.fail_refresh = True
    with pytest.raises(ValueError, match="synthetic refresh failure"):
        await sender.send("recipient", {}, None, 60, clock=fcm.clock)
    assert len(fcm.requests) == int(warm) and len(fcm.clients) == int(warm)
    fcm.fail_refresh = False
    assert await sender.send("recipient", {}, None, 60, clock=fcm.clock) is Delivery.ok
    assert len(fcm.clients) == 1 and fcm.loads == fcm.refreshes == 2 + int(warm)


@pytest.mark.parametrize("ttl,remaining", [(0, 60), (-1, 60), (60, 0), (60, 0.999)])
async def test_already_expired_never_acquires_credentials_or_client(fcm, ttl, remaining):
    sender = fcm.sender()
    assert await sender.send("recipient", {}, None, ttl,
                             expires_at=START + timedelta(seconds=remaining), clock=fcm.clock) is Delivery.expired
    assert not fcm.clients and not fcm.requests and fcm.loads == 0


@pytest.mark.parametrize("refresh_wait,client_wait", [(6, 0), (2, 4), (0, 6)])
async def test_implicit_deadline_survives_refresh_and_client_construction(fcm, refresh_wait, client_wait):
    fcm.refresh_wait, fcm.client_wait = refresh_wait, client_wait
    sender = fcm.sender()
    assert await sender.send("recipient", {}, None, 5, clock=fcm.clock) is Delivery.expired
    assert len(fcm.clients) == 1 and not fcm.requests and fcm.refreshes == 1


async def test_setup_reduces_ttl_and_reused_client_still_checks_after_refresh(fcm):
    fcm.refresh_wait, fcm.client_wait = 2, 1
    sender = fcm.sender()
    assert await sender.send("recipient", {}, None, 7, clock=fcm.clock) is Delivery.ok
    assert fcm.requests[0][1]["android"]["ttl"] == "4s"
    fcm.refresh_wait = 6
    assert await sender.send("recipient", {}, None, 5, clock=fcm.clock) is Delivery.expired
    assert len(fcm.requests) == 1 and len(fcm.clients) == 1


@pytest.mark.parametrize("failure", [ValueError("unexpected failure"), asyncio.CancelledError()])
async def test_send_escape_leaves_cleanup_to_owner_and_client_can_recover(fcm, failure):
    sender = fcm.sender()
    fcm.response = failure
    with pytest.raises(type(failure)):
        await sender.send("recipient", {}, None, 60, clock=fcm.clock)
    assert not fcm.clients[0].is_closed
    fcm.response = 200
    assert await sender.send("recipient", {}, None, 60, clock=fcm.clock) is Delivery.ok
    assert len(fcm.clients) == 1


async def test_unused_close_is_idempotent_and_closed_send_rejects_before_setup(fcm):
    sender = fcm.sender()
    await sender.aclose()
    await sender.aclose()
    with pytest.raises(RuntimeError, match="sender is closed"):
        await sender.send("recipient", {}, None, 0, clock=fcm.clock)
    assert fcm.loads == 0 and not fcm.clients


async def test_close_failure_is_not_retried_and_sender_stays_closed(fcm):
    sender = fcm.sender()
    await sender.send("recipient", {}, None, 60, clock=fcm.clock)
    fcm.close_error = True
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        await sender.aclose()
    await sender.aclose()
    with pytest.raises(RuntimeError, match="sender is closed"):
        await sender.send("recipient", {}, None, 60, clock=fcm.clock)
    assert fcm.clients[0].close_calls == 1 and fcm.loads == 1


async def test_logging_sender_close_is_a_noop():
    sender = LoggingPush()
    await sender.aclose()
    await sender.aclose()
    assert await sender.send("synthetic", {}, None, 60) is Delivery.retry
