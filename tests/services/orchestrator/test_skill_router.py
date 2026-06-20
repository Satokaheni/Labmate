"""Tests for skill_router.py (mocked, no GPU required)."""
from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.skill_router import SkillRouter
from services.orchestrator import events
from services.skill_runner.skill_runner import SkillRunner, SkillMeta
from pathlib import Path


def _make_mock_acompletion_response(content: str) -> MagicMock:
    """Create a mock litellm.acompletion response."""
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    r.choices[0].message.tool_calls = None
    return r


def _make_tool_call_response(skill_name: str) -> MagicMock:
    """Create a mock litellm.acompletion response with a load_skill tool call."""
    r = MagicMock()
    tool_call = MagicMock()
    tool_call.function.name = "load_skill"
    tool_call.function.arguments = json.dumps({"name": skill_name})
    r.choices = [MagicMock()]
    r.choices[0].message.tool_calls = [tool_call]
    return r


@pytest.mark.mocked
class TestSkillRouter:
    """Test the SkillRouter class."""

    @pytest.fixture
    def mock_runner(self):
        """Create a mock SkillRunner with a catalog."""
        runner = MagicMock(spec=SkillRunner)
        runner.catalog = {
            "ast-repo-map": SkillMeta(
                name="ast-repo-map",
                description="Map a repository structure",
                path=Path("/fake/SKILL.md"),
                tier="bundled",
            ),
            "web-search": SkillMeta(
                name="web-search",
                description="Search the web",
                path=Path("/fake/SKILL.md"),
                tier="bundled",
            ),
        }
        runner.catalog_prompt.return_value = (
            "Available skills (call load_skill(name) to activate one):\n"
            "- ast-repo-map: Map a repository structure\n"
            "- web-search: Search the web"
        )
        runner.tool_schema.return_value = {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": "Load a skill",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "enum": ["ast-repo-map", "web-search"]}},
                    "required": ["name"],
                },
            },
        }
        return runner

    @pytest.fixture
    def mock_redis(self):
        """Create a mock redis.asyncio.Redis client."""
        r = AsyncMock()
        r.xadd.return_value = b"1-0"
        r.get.return_value = None
        r.set.return_value = True
        return r

    @pytest.fixture
    def router(self, mock_runner, mock_redis):
        """Create a SkillRouter with mocked dependencies."""
        return SkillRouter(
            runner=mock_runner,
            redis=mock_redis,
            gemma_api_base="http://localhost:8000/v1",
            call_timeout=5.0,
        )

    # ────── select() tests ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_returns_skill_name_on_tool_call(self, router):
        """select() returns the skill name when model emits a load_skill tool call."""
        mock_response = _make_tool_call_response("ast-repo-map")

        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_acomp:
            result = await router.select("Map this repository")

            assert result == "ast-repo-map"
            mock_acomp.assert_awaited_once()
            call_kwargs = mock_acomp.call_args.kwargs
            assert call_kwargs["model"] == "openai/gemma-4-31b"
            assert call_kwargs["api_key"] == "not-needed"
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 0

    @pytest.mark.asyncio
    async def test_select_returns_none_on_no_tool_call(self, router):
        """select() returns None when there is no tool call."""
        mock_response = _make_mock_acompletion_response("I cannot select a skill for this task")

        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response):
            result = await router.select("Do something generic")
            assert result is None

    @pytest.mark.asyncio
    async def test_select_returns_none_on_empty_choices(self, router):
        """select() returns None when choices list is empty."""
        mock_response = MagicMock()
        mock_response.choices = []

        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response):
            result = await router.select("task")
            assert result is None

    @pytest.mark.asyncio
    async def test_select_returns_none_on_acompletion_error(self, router):
        """select() returns None when acompletion raises an exception."""
        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=RuntimeError("API error")):
            result = await router.select("task")
            assert result is None

    @pytest.mark.asyncio
    async def test_select_parses_json_arguments(self, router):
        """select() correctly parses JSON arguments in tool call."""
        r = MagicMock()
        tool_call = MagicMock()
        tool_call.function.name = "load_skill"
        tool_call.function.arguments = '{"name": "web-search"}'
        r.choices = [MagicMock()]
        r.choices[0].message.tool_calls = [tool_call]

        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new_callable=AsyncMock, return_value=r):
            result = await router.select("Search for something")
            assert result == "web-search"

    # ────── plan_tool_call() tests ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_plan_tool_call_returns_tool_and_arguments(self, router):
        """plan_tool_call() returns {"tool": str, "arguments": dict}."""
        router._runner.load_skill.return_value = {
            "response": {
                "status": "loaded",
                "name": "ast-repo-map",
                "body": "## Tools\n\n### map_repo\nMaps the repository.\nArguments: root_path (str)",
            }
        }

        json_response = '{"tool": "map_repo", "arguments": {"root_path": "/workspace"}}'
        mock_response = _make_mock_acompletion_response(json_response)

        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response):
            result = await router.plan_tool_call("Map /workspace", "ast-repo-map")

            assert result is not None
            assert result["tool"] == "map_repo"
            assert result["arguments"]["root_path"] == "/workspace"

    @pytest.mark.asyncio
    async def test_plan_tool_call_strips_code_fences(self, router):
        """plan_tool_call() strips markdown code fences from JSON response."""
        router._runner.load_skill.return_value = {
            "response": {
                "status": "loaded",
                "name": "ast-repo-map",
                "body": "## Tools\n\n### map_repo\nMaps the repository.",
            }
        }

        # Response wrapped in code fences
        json_response = (
            '```json\n'
            '{"tool": "map_repo", "arguments": {"root_path": "/workspace"}}\n'
            '```'
        )
        mock_response = _make_mock_acompletion_response(json_response)

        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response):
            result = await router.plan_tool_call("Map /workspace", "ast-repo-map")

            assert result is not None
            assert result["tool"] == "map_repo"

    @pytest.mark.asyncio
    async def test_plan_tool_call_returns_none_on_bad_json(self, router):
        """plan_tool_call() returns None if JSON is malformed."""
        router._runner.load_skill.return_value = {
            "response": {
                "status": "loaded",
                "name": "ast-repo-map",
                "body": "## Tools",
            }
        }

        mock_response = _make_mock_acompletion_response('{"tool": "broken json}')

        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response):
            result = await router.plan_tool_call("Map /workspace", "ast-repo-map")
            assert result is None

    @pytest.mark.asyncio
    async def test_plan_tool_call_returns_none_on_load_failure(self, router):
        """plan_tool_call() returns None if skill fails to load."""
        router._runner.load_skill.return_value = {
            "response": {
                "status": "error",
                "message": "skill not found",
            }
        }

        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new_callable=AsyncMock):
            result = await router.plan_tool_call("task", "missing-skill")
            assert result is None

    @pytest.mark.asyncio
    async def test_plan_tool_call_passes_thinking_budget_zero(self, router):
        """plan_tool_call() passes thinking_budget_tokens=0."""
        router._runner.load_skill.return_value = {
            "response": {
                "status": "loaded",
                "name": "web-search",
                "body": "## Tools\n\n### search\nSearch the web.",
            }
        }

        json_response = '{"tool": "search", "arguments": {"query": "test"}}'
        mock_response = _make_mock_acompletion_response(json_response)

        with patch("services.orchestrator.skill_router.litellm.acompletion",
                   new_callable=AsyncMock, return_value=mock_response) as mock_acomp:
            await router.plan_tool_call("Find something", "web-search")

            call_kwargs = mock_acomp.call_args.kwargs
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 0

    # ────── execute() tests ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_execute_writes_to_redis_and_polls_for_result(self, router):
        """execute() XADD to stream and polls GET for result."""
        result_json = json.dumps({"ok": True, "result": "mapped successfully"})
        router._redis.get.return_value = result_json

        result = await router.execute("ast-repo-map", "map_repo", {"root_path": "/workspace"})

        # Verify XADD was called
        router._redis.xadd.assert_awaited_once()
        call_args = router._redis.xadd.call_args
        assert call_args[0][0] == "labmate:skill-tasks"
        assert "payload" in call_args[0][1]

        # Verify result was parsed
        assert result["ok"] is True
        assert "mapped" in result["result"]

    @pytest.mark.asyncio
    async def test_execute_returns_timeout_on_poll_timeout(self, router):
        """execute() returns {"ok": False, "error": "timeout"} if result not found in time."""
        router._redis.get.return_value = None
        router._call_timeout = 0.1  # Very short timeout

        result = await router.execute("ast-repo-map", "map_repo", {"root_path": "/"})

        assert result["ok"] is False
        assert result["error"] == "timeout"

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_malformed_result(self, router):
        """execute() returns error if result JSON is malformed."""
        router._redis.get.return_value = "not valid json {{"

        result = await router.execute("skill", "tool", {})

        assert result["ok"] is False
        assert "malformed" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_redis_exception(self, router):
        """execute() returns error if redis operation fails."""
        router._redis.xadd.side_effect = RuntimeError("Redis connection lost")

        result = await router.execute("skill", "tool", {})

        assert result["ok"] is False

    # ────── run() tests ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_run_returns_none_when_select_returns_none(self, router):
        """run() returns None if select() returns None."""
        with patch.object(router, "select", new_callable=AsyncMock, return_value=None):
            result = await router.run("some task")
            assert result is None

    @pytest.mark.asyncio
    async def test_run_returns_none_when_plan_returns_none(self, router):
        """run() returns None if plan_tool_call() returns None."""
        with patch.object(router, "select", new_callable=AsyncMock, return_value="ast-repo-map"):
            with patch.object(router, "plan_tool_call", new_callable=AsyncMock, return_value=None):
                result = await router.run("some task")
                assert result is None

    @pytest.mark.asyncio
    async def test_run_full_pipeline(self, router):
        """run() executes the full select -> plan -> execute pipeline."""
        execute_result = {"ok": True, "result": "success"}

        with patch.object(router, "select", new_callable=AsyncMock, return_value="ast-repo-map"):
            with patch.object(
                router, "plan_tool_call",
                new_callable=AsyncMock,
                return_value={"tool": "map_repo", "arguments": {"path": "/ws"}}
            ):
                with patch.object(router, "execute", new_callable=AsyncMock, return_value=execute_result):
                    result = await router.run("Map the repository")

                    assert result is not None
                    assert result["ok"] is True
                    assert result["result"] == "success"

    @pytest.mark.asyncio
    async def test_run_returns_none_on_exception(self, router):
        """run() returns None if any step raises an exception."""
        with patch.object(router, "select", new_callable=AsyncMock, side_effect=RuntimeError("oops")):
            result = await router.run("task")
            assert result is None

    @pytest.mark.asyncio
    async def test_run_emits_tool_start_and_done(self, router):
        """run() emits tool.start and tool.done events with reasoning."""
        router.select = AsyncMock(return_value="ast-repo-map")
        router._last_reasoning = "the task asks to map the repo"
        router.plan_tool_call = AsyncMock(return_value={"tool": "map", "arguments": {"path": "."}})
        router.execute = AsyncMock(return_value={"ok": True, "result": {"content": [{"text": "ok"}]}})

        captured = []

        class FakeEmitter:
            async def emit(self, type, **f):
                captured.append({"type": type, **f})

        token = events.current_emitter.set(FakeEmitter())
        try:
            await router.run("map the repo")
        finally:
            events.current_emitter.reset(token)

        types = [e["type"] for e in captured]
        assert "tool.start" in types and "tool.done" in types
        start = next(e for e in captured if e["type"] == "tool.start")
        assert start["name"] == "ast-repo-map"
        assert start["kind"] == "skill"
        assert start["reasoning_why"] == "the task asks to map the repo"
        done = next(e for e in captured if e["type"] == "tool.done")
        assert done["status"] == "done"
        assert "tool_id" in start and start["tool_id"] == done["tool_id"]


@pytest.mark.mocked
class TestSkillRouterIntegration:
    """Integration-style tests with more realistic mocks."""

    @pytest.mark.asyncio
    async def test_end_to_end_with_mocked_skill_execution(self):
        """End-to-end test: select -> plan -> execute with mocked Redis result."""
        # Create a real SkillRunner with mocked catalog
        runner = MagicMock(spec=SkillRunner)
        runner.catalog = {"test-skill": MagicMock()}
        runner.catalog_prompt.return_value = "- test-skill: test skill"
        runner.tool_schema.return_value = {
            "type": "function",
            "function": {
                "name": "load_skill",
                "parameters": {"properties": {"name": {"enum": ["test-skill"]}}},
            },
        }
        runner.load_skill.return_value = {
            "response": {
                "status": "loaded",
                "body": "## Tools\n\n### do_work\nDoes work.\nArgs: input (str)",
            }
        }

        # Mock redis
        redis = AsyncMock()
        redis.xadd.return_value = b"1-0"
        redis.get.return_value = json.dumps({"ok": True, "result": "work done"})

        router = SkillRouter(
            runner=runner,
            redis=redis,
            gemma_api_base="http://localhost:8000/v1",
            call_timeout=5.0,
        )

        # Mock acompletion calls
        select_response = _make_tool_call_response("test-skill")
        plan_response = _make_mock_acompletion_response('{"tool": "do_work", "arguments": {"input": "data"}}')

        responses = [select_response, plan_response]
        call_count = [0]

        async def mock_acomp(*args, **kwargs):
            resp = responses[call_count[0]]
            call_count[0] += 1
            return resp

        with patch("services.orchestrator.skill_router.litellm.acompletion", side_effect=mock_acomp):
            result = await router.run("Do work")

            assert result is not None
            assert result["ok"] is True
            assert result["result"] == "work done"
            redis.xadd.assert_awaited_once()

    def test_skill_router_requires_non_none_redis(self):
        """Regression test: SkillRouter must receive a non-None redis client."""
        runner = MagicMock(spec=SkillRunner)
        runner.catalog = {"test-skill": MagicMock()}

        # This should not raise at construction, but redis must be non-None
        router = SkillRouter(
            runner=runner,
            redis=AsyncMock(),
            gemma_api_base="http://localhost:8000/v1",
        )

        assert router._redis is not None
