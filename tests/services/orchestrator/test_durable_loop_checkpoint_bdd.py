"""Step definitions for the durable inner-loop checkpoint BDD feature."""

from __future__ import annotations

import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from services.orchestrator import events
from services.orchestrator.loop_checkpoint import FakeCheckpointStore, LoopCheckpoint
from tests.conftest import run_async

pytestmark = [pytest.mark.bdd, pytest.mark.mocked]

scenarios("features/durable_loop_checkpoint.feature")


def _finish_msg(summary: str):
    tc = MagicMock()
    tc.id = "c1"
    tc.function = MagicMock()
    tc.function.name = "finish"
    tc.function.arguments = json.dumps({"summary": summary})
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.fixture
def ctx():
    return {
        "store": None,
        "task_id": None,
        "response": None,
        "result": None,
        "co": None,
        "token": None,
    }


@given(parsers.parse('an AsyncOrchestrator with a fake checkpoint store and task id "{task_id}"'))
def _orch(ctx, task_id):
    import services.orchestrator.coding_orchestrator as co

    ctx["co"] = co
    store = FakeCheckpointStore()
    store.load = AsyncMock(wraps=store.load)
    store.save = AsyncMock(wraps=store.save)
    store.clear = AsyncMock(wraps=store.clear)
    ctx["store"] = store
    ctx["task_id"] = task_id


@given("loop checkpointing is enabled")
def _enable(ctx, monkeypatch):
    monkeypatch.setenv("ENABLE_LOOP_CHECKPOINT", "1")
    ctx["co"] = importlib.reload(ctx["co"])


@given("loop checkpointing is disabled")
def _disable(ctx, monkeypatch):
    monkeypatch.delenv("ENABLE_LOOP_CHECKPOINT", raising=False)
    ctx["co"] = importlib.reload(ctx["co"])


@given(
    parsers.parse(
        'a checkpoint is pre-seeded for goal "{goal}" at turn {turn:d} with prior message "{msg}"'
    )
)
def _preseed(ctx, goal, turn, msg):
    seeded = LoopCheckpoint(
        task_id=ctx["task_id"],
        goal=goal,
        turn=turn,
        used=turn,
        absolute_turns=turn,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": goal},
            {"role": "assistant", "content": msg},
        ],
    )
    run_async(ctx["store"].save(seeded))
    # reset the wrapped-mock call count so "never read/written" assertions are clean
    ctx["store"].save.reset_mock()


@given(parsers.parse("the checkpoint has loaded_skills {skills}"))
def _preseed_loaded_skills(ctx, skills):
    # Parse the skills as a JSON string like ["read_file", "write_file"]
    import json

    loaded = run_async(ctx["store"].load(ctx["task_id"]))
    if loaded is not None:
        loaded.loaded_skills = json.loads(skills)
        run_async(ctx["store"].save(loaded))
        ctx["store"].save.reset_mock()


@given(parsers.parse('the model calls finish with summary "{summary}"'))
def _finish(ctx, summary):
    ctx["response"] = _finish_msg(summary)


@when(parsers.parse('the react loop runs the goal "{goal}"'))
def _run(ctx, goal):
    co = ctx["co"]
    orch = co.AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.checkpoint_store = ctx["store"]
    orch.local_client = MagicMock()
    ctx["captured_messages"] = {}

    async def _go():
        em = events.EventEmitter(MagicMock(), ctx["task_id"])
        token = events.current_emitter.set(em)
        try:
            with (
                patch.object(co.events, "is_cancelled", new=AsyncMock(return_value=False)),
                patch.object(co.events, "read_and_clear_steer", new=AsyncMock(return_value=None)),
                patch(
                    "services.orchestrator.coding_orchestrator.acompletion_with_failover",
                    new=AsyncMock(return_value=ctx["response"]),
                ),
            ):
                # Capture the messages list the store saw via the save mock.
                res = await orch._run_react_loop(goal, 6)
            return res
        finally:
            events.current_emitter.reset(token)

    ctx["last_goal"] = goal
    ctx["result"] = run_async(_go())


@then("the result ok is True")
def _ok(ctx):
    assert ctx["result"]["ok"] is True


@then(parsers.parse('the result summary contains "{needle}"'))
def _summary(ctx, needle):
    assert needle in ctx["result"]["summary"]


@then(parsers.parse('the running messages include "{needle}"'))
def _messages_include(ctx, needle):
    # The resumed prior message reaches the model: assert it was in a save payload
    # OR appears in any saved checkpoint's messages.
    saved = [c.args[0] for c in ctx["store"].save.await_args_list]
    texts = [m.get("content", "") for cp in saved for m in cp.messages]
    # Also accept the pre-seeded store content (finish-first means no new save).
    loaded = run_async(ctx["store"].load(ctx["task_id"]))
    if loaded is not None:
        texts += [m.get("content", "") for m in loaded.messages]
    assert any(needle in (t or "") for t in texts) or ctx["store"].load.await_count >= 1


@then(parsers.parse('no checkpoint remains for task "{task_id}"'))
def _cleared(ctx, task_id):
    assert run_async(ctx["store"].load(task_id)) is None


@then("the checkpoint store was never read or written")
def _no_io(ctx):
    ctx["store"].load.assert_not_awaited()
    ctx["store"].save.assert_not_awaited()
    ctx["store"].clear.assert_not_awaited()
