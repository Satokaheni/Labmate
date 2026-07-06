from __future__ import annotations

import pytest

from services.orchestrator import lite_persistence as lp
from services.orchestrator.lite_state import build_initial_state


class _FakeStore:
    def __init__(self):
        self._cp = {}

    async def checkpoint_put(self, task_id, payload):
        self._cp[task_id] = payload

    async def checkpoint_get(self, task_id):
        return self._cp.get(task_id)

    async def checkpoint_delete(self, task_id):
        self._cp.pop(task_id, None)


@pytest.mark.asyncio
async def test_save_then_load_round_trips_state_and_phase():
    store = _FakeStore()
    s = build_initial_state("do it", "sess")
    await lp.save_suspend(store, "t1", s, phase="await_approval")
    loaded = await lp.load_resume(store, "t1")
    assert loaded is not None
    state, phase = loaded
    assert phase == "await_approval"
    assert state["root_goal"] == "do it"


@pytest.mark.asyncio
async def test_load_missing_returns_none_and_clear_removes():
    store = _FakeStore()
    assert await lp.load_resume(store, "nope") is None
    s = build_initial_state("x", "sess")
    await lp.save_suspend(store, "t1", s, phase="assess")
    await lp.clear(store, "t1")
    assert await lp.load_resume(store, "t1") is None


@pytest.mark.asyncio
async def test_save_swallows_store_error():
    class _BadStore:
        async def checkpoint_put(self, task_id, payload):
            raise RuntimeError("db down")

    # must NOT raise — persistence is best-effort
    await lp.save_suspend(_BadStore(), "t1", build_initial_state("x", "s"), phase="assess")
