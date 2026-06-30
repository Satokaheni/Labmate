"""assess_ambiguity must not ask "which codebase?" when a local workspace client
is attached. Surfaced by a live test: "find where WebSocket auth is handled" was
gated for clarification because the triage prompt had no notion of an attached
workspace. The fix injects a workspace clause into the prompt only when a manifest
is present (no-client prompt unchanged).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator import client_context, events
from services.orchestrator.tool_manifest import parse_manifest


class _FakeEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, type: str, **fields):
        self.events.append((type, fields))


@pytest.fixture
def fake_emitter():
    emitter = _FakeEmitter()
    token = events.current_emitter.set(emitter)
    yield emitter
    events.current_emitter.reset(token)


def _make_state(root_goal: str) -> dict:
    from services.orchestrator.types import create_goal

    tree: dict = {}
    create_goal(tree, "root", None, root_goal)
    return {
        "session_id": "ws-aware-001",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": root_goal,
    }


def _assess_node():
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator, CodingOrchestrator
    from services.orchestrator.graph import make_nodes

    mock_orch = MagicMock(spec=CodingOrchestrator)
    mock_orch.architect = AsyncMock(
        return_value='{"assumptions": [], "ambiguity": 0.1, "blocking_question": ""}'
    )
    mock_async_orch = MagicMock(spec=AsyncOrchestrator)
    nodes = make_nodes(mock_orch, mock_async_orch)
    return nodes[5], mock_orch


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_workspace_attached_injects_clause_forbidding_which_codebase(fake_emitter):
    assess, mock_orch = _assess_node()
    manifest = parse_manifest({"tools": [{"name": "search_files", "source": "builtin"}]})
    token = client_context.set_manifest(manifest)
    try:
        await assess(_make_state("find where websocket auth is handled"))
    finally:
        client_context.reset_manifest(token)

    prompt = mock_orch.architect.call_args.args[0]
    # The triage prompt tells the model the workspace is the target and forbids
    # asking which codebase/repo to use.
    assert "local workspace IS attached" in prompt
    assert "MUST NEVER set blocking_question to ask which codebase" in prompt


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_no_client_prompt_has_no_workspace_clause(fake_emitter):
    # Ensure no leaked manifest from another test.
    assert client_context.get_manifest() is None
    assess, mock_orch = _assess_node()
    await assess(_make_state("find where websocket auth is handled"))

    prompt = mock_orch.architect.call_args.args[0]
    assert "local workspace IS attached" not in prompt
