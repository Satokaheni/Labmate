"""pytest-bdd step defs for revise-before-deliver.

@mocked: the revise node's single model call is the orchestrator's
orch.architect, which we replace with an AsyncMock (the established graph-test
idiom). No HTTP, no GPU.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from pytest_bdd import scenarios, given, when, then, parsers

import services.orchestrator.graph as graph_mod
from services.orchestrator.graph import make_nodes
from tests.conftest import run_async

scenarios("features/revise_before_deliver.feature")


@pytest.fixture
def ctx(monkeypatch):
    """Mutable scenario context; defaults the feature ON + emit patched."""
    monkeypatch.setattr(graph_mod, "ENABLE_FINALIZE_REVISION", True, raising=False)
    monkeypatch.setattr(graph_mod, "MAX_FINALIZE_REVISIONS", 1, raising=False)
    monkeypatch.setattr(graph_mod.events, "emit", AsyncMock())
    architect = AsyncMock(return_value="UNSET")
    return {
        "architect": architect,
        "state": {
            "last_artifact": {"type": "other", "payload": ""},
            "finalize_revisions": 0,
        },
        "out": None,
    }


# ── Background ────────────────────────────────────────────────────────────────
@given("the finalize-revision feature is enabled")
def _enabled(ctx, monkeypatch):
    monkeypatch.setattr(graph_mod, "ENABLE_FINALIZE_REVISION", True, raising=False)


@given("the finalize-revision feature is disabled")
def _disabled(ctx, monkeypatch):
    monkeypatch.setattr(graph_mod, "ENABLE_FINALIZE_REVISION", False, raising=False)


@given(parsers.parse("the maximum finalize revisions is {n:d}"))
def _max(ctx, monkeypatch, n):
    monkeypatch.setattr(graph_mod, "MAX_FINALIZE_REVISIONS", n, raising=False)


# ── State setup ───────────────────────────────────────────────────────────────
@given(parsers.parse('a finalized state for task "{task}"'))
def _task(ctx, task):
    ctx["state"]["root_goal"] = task
    ctx["state"]["goal_tree"] = {
        "root": {"status": "COMPLETED", "attempts": 0, "result": "", "children": []}
    }


@given(parsers.parse('the finalized answer is "{answer}"'))
def _answer(ctx, answer):
    ctx["state"]["final_answer"] = answer


@given(parsers.parse('the finalized answer is ""'))
def _answer_blank(ctx):
    ctx["state"]["final_answer"] = ""


@given("no side-effecting tools ran during the task")
def _no_side_effects(ctx):
    ctx["state"]["last_artifact"] = {"type": "other", "payload": ""}


@given("side-effecting tools ran during the task")
def _side_effects(ctx):
    ctx["state"]["last_artifact"] = {"type": "code", "payload": "wrote a file"}


@given("the run did not error")
def _no_error(ctx):
    ctx["state"].pop("error", None)


@given(parsers.parse('the run errored with "{msg}"'))
def _errored(ctx, msg):
    ctx["state"]["error"] = msg


@given(parsers.parse("the finalize revision count is already {n:d}"))
def _count_already(ctx, n):
    ctx["state"]["finalize_revisions"] = n


@given(parsers.parse('the revision model will return "{text}"'))
def _model_returns(ctx, text):
    ctx["architect"] = AsyncMock(return_value=text)


# ── Action ────────────────────────────────────────────────────────────────────
@when("the revise node runs")
def _run(ctx):
    orch = MagicMock()
    orch.architect = ctx["architect"]
    nodes = make_nodes(orch, MagicMock())
    revise = nodes[7]
    ctx["out"] = run_async(revise(ctx["state"]))


# ── Assertions ────────────────────────────────────────────────────────────────
@then(parsers.parse('the delivered final answer is "{expected}"'))
def _delivered(ctx, expected):
    out = ctx["out"] or {}
    # When the node passes through (out == {}), the delivered answer is the
    # original final_answer in state; otherwise it's the node's final_answer.
    delivered = out.get("final_answer", ctx["state"].get("final_answer"))
    assert delivered == expected


@then(parsers.parse("the revision model was called exactly {n:d} time"))
@then(parsers.parse("the revision model was called exactly {n:d} times"))
def _called(ctx, n):
    assert ctx["architect"].await_count == n


@then(parsers.parse("the finalize revision count is {n:d}"))
def _count(ctx, n):
    out = ctx["out"] or {}
    assert out.get("finalize_revisions", ctx["state"].get("finalize_revisions")) == n
