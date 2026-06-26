from __future__ import annotations

import time

import pytest

from services.orchestrator.coding_orchestrator import AsyncOrchestrator


@pytest.mark.mocked
def test_now_defaults_to_time_monotonic():
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    assert orch._now is time.monotonic


@pytest.mark.mocked
def test_now_is_injectable():
    calls = {"n": 0}

    def fake_clock() -> float:
        calls["n"] += 1
        return float(calls["n"])

    orch = AsyncOrchestrator(
        skill_router=None, mcp=None, workspace="/tmp", now=fake_clock
    )
    assert orch._now is fake_clock
    assert orch._now() == 1.0
    assert orch._now() == 2.0
