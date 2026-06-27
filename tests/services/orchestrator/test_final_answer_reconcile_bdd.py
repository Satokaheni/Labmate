# tests/services/orchestrator/test_final_answer_reconcile_bdd.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.main import OrchestratorProcess
from services.orchestrator import events as events_mod
from tests.conftest import run_async

scenarios("features/final_answer_reconcile.feature")


@pytest.fixture
def ctx(monkeypatch):
    async def _fake_emit(_type, **fields):
        pass
    monkeypatch.setattr(events_mod, "emit", _fake_emit)

    proc = OrchestratorProcess()
    proc._redis = AsyncMock()

    storage = AsyncMock()
    storage.workspaces = AsyncMock()
    storage.workspaces.record_session = AsyncMock()
    storage.workspaces.complete_session = AsyncMock()
    storage.workspaces.upsert_workspace = AsyncMock()

    orch = AsyncMock()
    orch.stream_final_answer = AsyncMock(return_value="")
    return {"proc": proc, "storage": storage, "orch": orch}


@given("an orchestrator handler with a mocked redis and storage")
def _handler(ctx):
    assert ctx["proc"]._redis is not None


@given("run_task returns a state with error None and final_answer \"internal\"")
def _state(ctx):
    ctx["orch"].run_task.return_value = {
        "session_id": "s1",
        "goal_tree": {},
        "final_answer": "internal",
        "error": None,
    }


@given(parsers.parse('stream_final_answer renders "{rendered}"'))
def _render(ctx, rendered):
    ctx["orch"].stream_final_answer = AsyncMock(return_value=rendered)


@when(parsers.parse('the handler processes task "{task_id}"'))
def _process(ctx, task_id):
    payload = json.dumps({"task_id": task_id, "task": "do the thing", "session_id": "s1"})
    run_async(ctx["proc"]._handle(f"{task_id}-msg", {"payload": payload}, ctx["orch"], ctx["storage"]))


@then(parsers.parse("the stored result ok is {value}"))
def _ok_is(ctx, value):
    set_args = ctx["proc"]._redis.set.call_args[0]
    stored = json.loads(set_args[1])
    assert stored["ok"] is (value == "True")


@then(parsers.parse('the stored final answer contains "{needle}"'))
def _answer_contains(ctx, needle):
    set_args = ctx["proc"]._redis.set.call_args[0]
    stored = json.loads(set_args[1])
    assert needle.lower() in stored["state"]["final_answer"].lower()
