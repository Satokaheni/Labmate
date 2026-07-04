"""Step definitions for the skill-usage-telemetry BDD contract.

Consumes: skill_telemetry (new_entry/record_use/compute_state/apply_transitions/
          load/save/record_use_best_effort) from skill_telemetry.py
          SkillRouter from skill_router.py
          run_async from tests.conftest
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from services.orchestrator import skill_telemetry as st
from services.orchestrator.skill_router import SkillRouter
from services.skill_runner.skill_runner import SkillRunner
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/skill_usage_telemetry.feature")

NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def ctx(tmp_path):
    """Shared mutable context for the scenario."""
    return {"path": tmp_path / "tele.json", "router_result": None}


@given("an empty telemetry store")
def _empty_store(ctx):
    st.save({"version": 1, "skills": {}}, ctx["path"])


@when(parsers.parse('the skill "{name}" is dispatched with result ok'))
def _dispatch_ok(ctx, name):
    st.record_use_best_effort(name, True, path=ctx["path"], now=NOW)


@when(parsers.parse('the skill "{name}" is dispatched with result fail'))
def _dispatch_fail(ctx, name):
    st.record_use_best_effort(name, False, path=ctx["path"], now=NOW)


@given(parsers.parse('the skill "{name}" was last used {days:d} days ago'))
def _seed_last_used(ctx, name, days):
    store = st.load(ctx["path"])
    entry = st.new_entry(NOW - timedelta(days=days))
    entry["last_used_at"] = (NOW - timedelta(days=days)).isoformat()
    store["skills"][name] = entry
    st.save(store, ctx["path"])


@given(parsers.parse('the skill "{name}" was last used {days:d} days ago and is pinned'))
def _seed_pinned(ctx, name, days):
    store = st.load(ctx["path"])
    entry = st.new_entry(NOW - timedelta(days=days))
    entry["last_used_at"] = (NOW - timedelta(days=days)).isoformat()
    entry["pinned"] = True
    store["skills"][name] = entry
    st.save(store, ctx["path"])


@when("the telemetry states are recomputed")
def _recompute(ctx):
    store = st.load(ctx["path"])
    store = st.apply_transitions(store, NOW)
    st.save(store, ctx["path"])


@given("telemetry persistence is broken")
def _break_persistence(ctx):
    ctx["_patch"] = patch.object(st, "save", side_effect=OSError("disk full"))
    ctx["_patch"].start()


@when(parsers.parse('the skill "{name}" is dispatched through the router with result ok'))
def _dispatch_via_router(ctx, name):
    runner = MagicMock(spec=SkillRunner)
    runner.catalog = {name: MagicMock()}
    router = SkillRouter(
        runner=runner,
        registry=AsyncMock(),
        gemma_api_base="http://localhost:8000/v1",
        telemetry_path=ctx["path"],
    )
    router.select = AsyncMock(return_value=name)
    router.plan_tool_call = AsyncMock(return_value={"tool": "t", "arguments": {}})
    router.execute = AsyncMock(return_value={"ok": True, "result": "done"})
    try:
        ctx["router_result"] = run_async(router.run("do it"))
    finally:
        if "_patch" in ctx:
            ctx["_patch"].stop()


@then(parsers.parse('the telemetry use_count for "{name}" is {n:d}'))
def _check_use(ctx, name, n):
    assert st.load(ctx["path"])["skills"][name]["use_count"] == n


@then(parsers.parse('the telemetry success_count for "{name}" is {n:d}'))
def _check_success(ctx, name, n):
    assert st.load(ctx["path"])["skills"][name]["success_count"] == n


@then(parsers.parse('the telemetry fail_count for "{name}" is {n:d}'))
def _check_fail(ctx, name, n):
    assert st.load(ctx["path"])["skills"][name]["fail_count"] == n


@then(parsers.parse('the telemetry last_used_at for "{name}" is set'))
def _check_last_used(ctx, name):
    assert st.load(ctx["path"])["skills"][name]["last_used_at"] is not None


@then(parsers.parse('the telemetry state for "{name}" is "{state}"'))
def _check_state(ctx, name, state):
    assert st.load(ctx["path"])["skills"][name]["state"] == state


@then("the router dispatch result is ok")
def _check_router_ok(ctx):
    assert ctx["router_result"] is not None
    assert ctx["router_result"]["ok"] is True
