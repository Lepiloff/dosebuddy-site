"""Actual worker.main cleanup; no database, OAuth or external HTTP."""

import asyncio
from types import SimpleNamespace

import pytest

from app import worker
from tests.test_push_lifecycle import fcm  # noqa: F401

pytestmark = pytest.mark.asyncio


def wire_main(monkeypatch, fcm):
    sender = fcm.sender()
    events = []

    async def dispose():
        events.append("dispose")

    original_close = sender.aclose

    async def close():
        events.append("close")
        await original_close()

    sender.aclose = close
    settings = SimpleNamespace(authority_lease=None, authority_lease_minutes=0)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "setup_logging", lambda _: None)
    monkeypatch.setattr(worker, "create_engine", lambda _: SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(worker, "create_sessionmaker", lambda _: "synthetic-sessionmaker")
    monkeypatch.setattr(worker, "build_push", lambda _: sender)

    async def sweep(*args):
        events.append("sweep")
        return None

    async def sleep(_):
        events.append("sleep")
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "sweep_if_due", sweep)
    monkeypatch.setattr(worker, "asyncio", SimpleNamespace(sleep=sleep))
    return sender, events


@pytest.mark.parametrize("unused,scan_error,close_error", [
    (False, False, False), (True, False, False), (False, True, False), (False, False, True),
])
async def test_main_finally_closes_sender_and_disposes_engine(
    monkeypatch, fcm, unused, scan_error, close_error,
):
    sender, events = wire_main(monkeypatch, fcm)
    fcm.close_error = close_error

    async def scan(sessionmaker, push, *, lease):
        assert sessionmaker == "synthetic-sessionmaker" and push is sender and lease is None
        events.append("scan")
        if not unused:
            await sender.send("recipient", {}, None, 60, clock=fcm.clock)
        if scan_error:
            raise ValueError("synthetic scan failure")
        return 0

    monkeypatch.setattr(worker, "scan_once", scan)
    with pytest.raises(RuntimeError if close_error else asyncio.CancelledError):
        await worker.main()
    assert events == ["scan", "sweep", "sleep", "close", "dispose"]
    assert len(fcm.clients) == (0 if unused else 1)
    if fcm.clients:
        assert fcm.clients[0].is_closed and fcm.clients[0].close_calls == 1


async def test_main_cancellation_during_real_http_closes_sender_and_engine(monkeypatch, fcm):
    sender, events = wire_main(monkeypatch, fcm)
    fcm.block = True

    async def scan(*args, **kwargs):
        await sender.send("recipient", {}, None, 60, clock=fcm.clock)

    monkeypatch.setattr(worker, "scan_once", scan)
    task = asyncio.create_task(worker.main())
    await asyncio.wait_for(fcm.entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["close", "dispose"]
    assert fcm.clients[0].is_closed and fcm.clients[0].close_calls == 1


async def test_main_keeps_one_client_between_failed_and_successful_scans(monkeypatch, fcm):
    sender, events = wire_main(monkeypatch, fcm)
    calls = []

    async def scan(sessionmaker, push, *, lease):
        assert push is sender
        fcm.response = 503 if not calls else 200
        outcome = await push.send("recipient", {}, None, 60, clock=fcm.clock)
        calls.append(outcome.value)
        return int(outcome.value == "ok")

    async def sleep(_):
        if len(calls) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "scan_once", scan)
    monkeypatch.setattr(worker, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        await worker.main()
    assert calls == ["retry", "ok"]
    assert len(fcm.clients) == 1 and fcm.clients[0].close_calls == 1
    assert fcm.refreshes == fcm.loads == 2
    assert events == ["sweep", "sweep", "close", "dispose"]


async def test_main_unexpected_escape_still_closes_sender_and_engine(monkeypatch, fcm):
    class StopWorker(BaseException):
        pass

    sender, events = wire_main(monkeypatch, fcm)
    fcm.response = StopWorker()

    async def scan(*args, **kwargs):
        await sender.send("recipient", {}, None, 60, clock=fcm.clock)

    monkeypatch.setattr(worker, "scan_once", scan)
    with pytest.raises(StopWorker):
        await worker.main()
    assert events == ["close", "dispose"]
    assert fcm.clients[0].is_closed and fcm.clients[0].close_calls == 1
