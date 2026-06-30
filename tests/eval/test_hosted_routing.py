"""
Unit tests for hosted MCP-tool routing evaluation.

Tests the hosted-tool scoring path with stubbed model calls (no network).
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

# We'll import the eval modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "eval"))

from extend_eval import (
    covered_hosted_tools,
    templated_hosted_positive,
)
from extend_eval import (
    load_hosted_tools as extend_load_hosted_tools,
)
from run_routing_eval import (
    HOSTED_SYSTEM,
    is_correct_hosted,
    route_hosted_one,
    summarize,
)


# =========================================================================== #
# Test fixtures
# =========================================================================== #
@pytest.fixture
def sample_hosted_tools():
    """Sample hosted tool schemas."""
    return [
        {
            "type": "function",
            "function": {
                "name": "mcp__ast-ts-refactor__find_references",
                "description": "Find all usages of a symbol in TypeScript code",
                "parameters": {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mcp__ast-ts-refactor__rename_symbol",
                "description": "Rename a symbol across the project",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "old_name": {"type": "string"},
                        "new_name": {"type": "string"},
                    },
                    "required": ["old_name", "new_name"],
                },
            },
        },
    ]


@pytest.fixture
def sample_cases():
    """Sample mixed (skill + hosted) eval cases."""
    return [
        {
            "id": "skill_1",
            "task": "Map the repository structure",
            "expected": "ast-repo-map",
            "cluster": "nav",
        },
        {
            "id": "hosted_1",
            "task": "Where is the parseConfig function used",
            "expected": "mcp__ast-ts-refactor__find_references",
            "kind": "hosted",
            "cluster": "ts_refactor",
        },
        {
            "id": "hosted_2",
            "task": "Rename oldName to newName everywhere",
            "expected": "mcp__ast-ts-refactor__rename_symbol",
            "kind": "hosted",
            "cluster": "ts_refactor",
        },
    ]


# =========================================================================== #
# Scoring tests
# =========================================================================== #


class TestHostedScoring:
    def test_is_correct_hosted_exact_match(self):
        """Test scoring when prediction matches expected."""
        case = {
            "id": "test_1",
            "expected": "mcp__ast-ts-refactor__find_references",
        }
        assert is_correct_hosted(case, "mcp__ast-ts-refactor__find_references") is True

    def test_is_correct_hosted_wrong_prediction(self):
        """Test scoring when prediction differs."""
        case = {"id": "test_1", "expected": "mcp__ast-ts-refactor__find_references"}
        assert is_correct_hosted(case, "mcp__ast-ts-refactor__rename_symbol") is False

    def test_is_correct_hosted_no_prediction(self):
        """Test scoring when model declined."""
        case = {"id": "test_1", "expected": "mcp__ast-ts-refactor__find_references"}
        assert is_correct_hosted(case, None) is False

    def test_is_correct_hosted_acceptable_match(self):
        """Test scoring with acceptable alternatives."""
        case = {
            "id": "test_1",
            "expected": "mcp__ast-ts-refactor__find_references",
            "acceptable": ["mcp__ast-ts-refactor__find_definitions"],
        }
        assert is_correct_hosted(case, "mcp__ast-ts-refactor__find_definitions") is True

    def test_is_correct_hosted_none_decline(self):
        """Test scoring when expecting 'none' (no tool needed)."""
        case = {"id": "test_1", "expected": "none"}
        assert is_correct_hosted(case, None) is True
        assert is_correct_hosted(case, "mcp__some-tool__foo") is False


@pytest.mark.asyncio
async def test_route_hosted_one_with_stub():
    """Test route_hosted_one with a stubbed model call."""

    async def stub_call_model(client, model, task, hosted_tools, system_prompt, temperature):
        """Stub that returns a tool call for find_references."""

        # Simulate the API response structure
        class FakeToolCall:
            def __init__(self, name):
                self.function = type("obj", (), {"name": name})()

        class FakeMessage:
            def __init__(self, tool_calls):
                self.tool_calls = tool_calls

        class FakeChoice:
            def __init__(self):
                self.message = FakeMessage([FakeToolCall("mcp__ast-ts-refactor__find_references")])

        class FakeResponse:
            def __init__(self):
                self.choices = [FakeChoice()]

        return FakeResponse()

    result = await route_hosted_one(
        client=None,
        model="test",
        task="Where is foo used?",
        hosted_tools=[],
        system_prompt=HOSTED_SYSTEM,
        attempts=1,
        temperature=0.7,
        call_model=stub_call_model,
    )

    assert result == "mcp__ast-ts-refactor__find_references"


@pytest.mark.asyncio
async def test_route_hosted_one_decline():
    """Test route_hosted_one when model declines to call a tool."""

    async def stub_decline(client, model, task, hosted_tools, system_prompt, temperature):
        """Stub that returns no tool calls."""

        class FakeMessage:
            tool_calls = None

        class FakeChoice:
            message = FakeMessage()

        class FakeResponse:
            choices = [FakeChoice()]

        return FakeResponse()

    result = await route_hosted_one(
        client=None,
        model="test",
        task="Just chat",
        hosted_tools=[],
        system_prompt=HOSTED_SYSTEM,
        attempts=3,
        temperature=0.7,
        call_model=stub_decline,
    )

    assert result is None


# =========================================================================== #
# Case format tests
# =========================================================================== #


def test_case_kind_parsing(sample_cases):
    """Test that case kind is parsed correctly (defaults to skill)."""
    # First case has no kind -> should default to skill
    case_1 = sample_cases[0]
    kind = case_1.get("kind", "skill")
    assert kind == "skill"

    # Second case has kind:hosted
    case_2 = sample_cases[1]
    kind = case_2.get("kind")
    assert kind == "hosted"


def test_summarize_per_kind(sample_cases):
    """Test that summarize tracks accuracy per kind."""
    # Simulate results
    results = [
        {
            "id": "skill_1",
            "kind": "skill",
            "expected": "ast-repo-map",
            "cluster": "nav",
            "task": "Map the repo",
            "prediction": "ast-repo-map",
            "correct": True,
            "stability": 1.0,
        },
        {
            "id": "hosted_1",
            "kind": "hosted",
            "expected": "mcp__ast-ts-refactor__find_references",
            "cluster": "ts_refactor",
            "task": "Find usages",
            "prediction": "mcp__ast-ts-refactor__find_references",
            "correct": True,
            "stability": 1.0,
        },
        {
            "id": "hosted_2",
            "kind": "hosted",
            "expected": "mcp__ast-ts-refactor__rename_symbol",
            "cluster": "ts_refactor",
            "task": "Rename it",
            "prediction": "mcp__ast-ts-refactor__find_references",
            "correct": False,
            "stability": 0.67,
        },
    ]

    summary = summarize(results)

    assert summary["n"] == 3
    assert summary["overall"] == pytest.approx(2 / 3, abs=0.01)
    assert "by_kind" in summary
    assert summary["by_kind"]["skill"] == 1.0  # 1/1 correct
    assert summary["by_kind"]["hosted"] == pytest.approx(0.5, abs=0.01)  # 1/2 correct


# =========================================================================== #
# Fixture I/O tests
# =========================================================================== #


def test_load_hosted_tools_from_file():
    """Test loading hosted tools from JSON file."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "mcp__test__foo",
                "description": "Test tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(tools, f)
        f.flush()
        path = f.name

    try:
        loaded = extend_load_hosted_tools(path)
        assert len(loaded) == 1
        assert loaded[0]["function"]["name"] == "mcp__test__foo"
    finally:
        Path(path).unlink()


def test_load_hosted_tools_fixture(sample_hosted_tools):
    """Test that sample fixture parses correctly."""
    assert len(sample_hosted_tools) == 2
    assert sample_hosted_tools[0]["function"]["name"] == "mcp__ast-ts-refactor__find_references"
    assert sample_hosted_tools[1]["function"]["name"] == "mcp__ast-ts-refactor__rename_symbol"


def test_hosted_tools_example_file_parses():
    """Test that the real hosted_tools.example.json file loads and parses."""
    from pathlib import Path

    tools_path = (
        Path(__file__).parent.parent.parent / "eval" / "fixtures" / "hosted_tools.example.json"
    )
    tools = extend_load_hosted_tools(str(tools_path))

    assert tools is not None
    assert len(tools) > 0
    assert all(t["type"] == "function" for t in tools)
    assert all("name" in t["function"] for t in tools)


def test_ast_ts_refactor_file_parameter_optional():
    """Test that file parameter is optional for ast-ts-refactor tools."""
    from pathlib import Path

    tools_path = (
        Path(__file__).parent.parent.parent / "eval" / "fixtures" / "hosted_tools.example.json"
    )
    tools = extend_load_hosted_tools(str(tools_path))

    # Find the tools
    find_refs = next(
        (t for t in tools if t["function"]["name"] == "mcp__ast-ts-refactor__find_references"), None
    )
    rename = next(
        (t for t in tools if t["function"]["name"] == "mcp__ast-ts-refactor__rename_symbol"), None
    )

    assert find_refs is not None, "find_references tool not found"
    assert rename is not None, "rename_symbol tool not found"

    # Check that 'file' is NOT in required for find_references
    find_refs_required = find_refs["function"]["parameters"]["required"]
    assert "file" not in find_refs_required
    assert "tsconfig" in find_refs_required
    assert "symbol" in find_refs_required

    # Check that 'file' is NOT in required for rename_symbol
    rename_required = rename["function"]["parameters"]["required"]
    assert "file" not in rename_required
    assert "tsconfig" in rename_required
    assert "symbol" in rename_required
    assert "new_name" in rename_required

    # Verify 'file' still exists in properties (just not required)
    assert "file" in find_refs["function"]["parameters"]["properties"]
    assert "file" in rename["function"]["parameters"]["properties"]


def test_component_doc_gen_path_parameter_broadened():
    """Test that component_path parameter accepts name/relative paths."""
    from pathlib import Path

    tools_path = (
        Path(__file__).parent.parent.parent / "eval" / "fixtures" / "hosted_tools.example.json"
    )
    tools = extend_load_hosted_tools(str(tools_path))

    generate = next(
        (t for t in tools if t["function"]["name"] == "mcp__component-doc-gen__generate"), None
    )
    assert generate is not None, "generate tool not found"

    # Check description includes workspace-relative path guidance
    desc = generate["function"]["parameters"]["properties"]["component_path"]["description"]
    assert "workspace-relative path" in desc.lower() or "component name" in desc.lower()

    # Verify component_path is still required
    assert "component_path" in generate["function"]["parameters"]["required"]


# =========================================================================== #
# Case generation tests
# =========================================================================== #


def test_covered_hosted_tools():
    """Test tracking which hosted tools are already covered."""
    cases = [
        {
            "id": "s1",
            "expected": "ast-repo-map",
            "kind": "skill",
        },  # skill, ignored
        {
            "id": "h1",
            "expected": "mcp__ast-ts-refactor__find_references",
            "kind": "hosted",
        },
        {
            "id": "h2",
            "expected": "mcp__ast-ts-refactor__rename_symbol",
            "kind": "hosted",
        },
    ]
    covered = covered_hosted_tools(cases)
    assert len(covered) == 2
    assert "mcp__ast-ts-refactor__find_references" in covered
    assert "mcp__ast-ts-refactor__rename_symbol" in covered


def test_templated_hosted_positive():
    """Test deterministic task generation from tool description."""
    tool_desc = "Find all usages of a symbol in TypeScript code"
    tasks = templated_hosted_positive("mcp__test__find_refs", tool_desc, 3)

    assert len(tasks) == 3
    # Should contain task variations without tool name
    assert not any("mcp__" in t for t in tasks)
    assert any("find" in t.lower() for t in tasks)


def test_templated_hosted_positive_with_parens():
    """Test that parenthetical scope info is removed."""
    tool_desc = "Rename a symbol (across multiple files) in the project"
    tasks = templated_hosted_positive("mcp__test__rename", tool_desc, 2)

    assert len(tasks) >= 1
    # Should not contain the parenthetical
    assert not any("across multiple files" in t for t in tasks)


def test_no_tool_name_in_generated_tasks():
    """Test that generated task descriptions don't leak tool names."""
    tool_name = "mcp__ast-ts-refactor__find_references"
    tool_desc = "Find all usages of a symbol in TypeScript code"
    tasks = templated_hosted_positive(tool_name, tool_desc, 5)

    # None should contain the tool name
    assert all(tool_name not in t for t in tasks)
    # None should reference "mcp__" patterns
    assert all("mcp__" not in t for t in tasks)


# =========================================================================== #
# Workspace root context tests
# =========================================================================== #


def test_hosted_system_includes_workspace_context():
    """Test that HOSTED_SYSTEM includes workspace-root guidance."""
    assert "/workspace/project" in HOSTED_SYSTEM
    assert "absolute" in HOSTED_SYSTEM.lower()
    assert "join" in HOSTED_SYSTEM.lower() or "construct" in HOSTED_SYSTEM.lower()


def test_hosted_system_retains_tool_selector_guidance():
    """Test that HOSTED_SYSTEM still contains the core tool-selection guidance."""
    assert "tool selector" in HOSTED_SYSTEM
    assert "single best tool" in HOSTED_SYSTEM


# =========================================================================== #
# End-to-end integration tests
# =========================================================================== #


@pytest.mark.asyncio
async def test_e2e_hosted_eval_scoring():
    """End-to-end test: evaluate hosted cases with stubbed model."""
    from run_routing_eval import evaluate

    cases = [
        {
            "id": "h1",
            "task": "Where is X used?",
            "expected": "mcp__tool1__find",
            "kind": "hosted",
            "cluster": "tools",
        },
        {
            "id": "h2",
            "task": "Rename Y everywhere",
            "expected": "mcp__tool2__rename",
            "kind": "hosted",
            "cluster": "tools",
        },
    ]

    # Stub that alternates predictions
    call_count = [0]

    async def stub_model(client, model, task, hosted_tools, system_prompt, temperature):
        """Stub that returns different tools per call."""
        call_count[0] += 1
        tools = [
            "mcp__tool1__find",
            "mcp__tool2__rename",
        ]
        selected = tools[(call_count[0] - 1) % len(tools)]

        class FakeToolCall:
            def __init__(self, name):
                self.function = type("obj", (), {"name": name})()

        class FakeMessage:
            def __init__(self, tool_calls):
                self.tool_calls = tool_calls

        class FakeChoice:
            def __init__(self, tool_name):
                self.message = FakeMessage([FakeToolCall(tool_name)])

        class FakeResponse:
            def __init__(self, tool_name):
                self.choices = [FakeChoice(tool_name)]

        return FakeResponse(selected)

    results = await evaluate(
        cases,
        client=None,
        model="test",
        catalog={},  # No skills
        repeats=1,
        attempts=1,
        temperature=0.7,
        concurrency=1,
        hosted_tools=[{"type": "function", "function": {"name": "test"}}],
        call_model=stub_model,
    )

    assert len(results) == 2
    # First result should match (called with h1, got tool1)
    assert results[0]["correct"] is True
    # Second result should match (called with h2, got tool2)
    assert results[1]["correct"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
