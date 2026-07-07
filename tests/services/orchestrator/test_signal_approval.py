from __future__ import annotations

import asyncio

import pytest

from services.orchestrator.inproc_bus import SignalRegistry


@pytest.mark.asyncio
async def test_await_approval_returns_decision():
    sig = SignalRegistry()

    async def decide():
        await asyncio.sleep(0.05)
        sig.set_approval("t1", "approve")

    asyncio.create_task(decide())
    assert await sig.await_approval("t1", poll_s=0.01, timeout_s=2.0) == "approve"


@pytest.mark.asyncio
async def test_get_approval_is_consume_once():
    sig = SignalRegistry()
    sig.set_approval("t1", "reject")
    assert sig.get_approval("t1") == "reject"
    assert sig.get_approval("t1") is None


@pytest.mark.asyncio
async def test_await_approval_returns_reject_on_cancel():
    sig = SignalRegistry()
    sig.request_cancel("t1")
    assert await sig.await_approval("t1", poll_s=0.01, timeout_s=2.0) == "reject"


@pytest.mark.asyncio
async def test_await_approval_times_out():
    sig = SignalRegistry()
    with pytest.raises(TimeoutError):
        await sig.await_approval("t1", poll_s=0.01, timeout_s=0.05)
