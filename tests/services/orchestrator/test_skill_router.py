"""Tests for skill_router.py (mocked, no GPU required)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator import events
from services.orchestrator.skill_router import SkillRouter
from services.skill_runner.skill_runner import SkillMeta, SkillRunner


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
                    "properties": {
                        "name": {"type": "string", "enum": ["ast-repo-map", "web-search"]}
                    },
                    "required": ["name"],
                },
            },
        }
        return runner

    @pytest.fixture
    def mock_registry(self):
        """Create a mock SkillRegistry whose call_tool() is awaitable."""
        r = AsyncMock()
        r.call_tool.return_value = {"content": [{"type": "text", "text": "ok"}]}
        return r

    @pytest.fixture
    def router(self, mock_runner, mock_registry):
        """Create a SkillRouter with mocked dependencies."""
        return SkillRouter(
            runner=mock_runner,
            registry=mock_registry,
            gemma_api_base="http://localhost:8000/v1",
            call_timeout=5.0,
        )

    # ────── select() tests ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_returns_skill_name_on_tool_call(self, router):
        """select() returns the skill name when model emits a load_skill tool call."""
        mock_response = _make_tool_call_response("ast-repo-map")

        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_acomp:
            result = await router.select("Map this repository")

            assert result == "ast-repo-map"
            # Two-tier: unanimous agreement on first 3 samples → no tiebreak
            assert mock_acomp.await_count == 3
            # All calls should use thinking_budget_tokens == 0
            for call in mock_acomp.await_args_list:
                assert call.kwargs["extra_body"]["thinking_budget_tokens"] == 0

    @pytest.mark.asyncio
    async def test_select_returns_none_on_no_tool_call(self, router):
        """select() returns None when there is no tool call."""
        mock_response = _make_mock_acompletion_response("I cannot select a skill for this task")

        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await router.select("Do something generic")
            assert result is None

    @pytest.mark.asyncio
    async def test_select_returns_none_on_empty_choices(self, router):
        """select() returns None when choices list is empty."""
        mock_response = MagicMock()
        mock_response.choices = []

        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await router.select("task")
            assert result is None

    @pytest.mark.asyncio
    async def test_select_returns_none_on_acompletion_error(self, router):
        """select() returns None when acompletion raises an exception."""
        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API error"),
        ):
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

        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=r,
        ):
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

        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
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
            "```json\n" '{"tool": "map_repo", "arguments": {"root_path": "/workspace"}}\n' "```"
        )
        mock_response = _make_mock_acompletion_response(json_response)

        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
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

        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
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

        with patch(
            "services.orchestrator.skill_router.litellm.acompletion", new_callable=AsyncMock
        ):
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

        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_acomp:
            await router.plan_tool_call("Find something", "web-search")

            call_kwargs = mock_acomp.call_args.kwargs
            assert call_kwargs["extra_body"]["thinking_budget_tokens"] == 0

    # ────── execute() tests ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_execute_dispatches_via_registry_and_shapes_result(self, router):
        """execute() calls registry.call_tool directly in-process and shapes the result."""
        router._registry.call_tool.return_value = "mapped successfully"

        result = await router.execute("ast-repo-map", "map_repo", {"root_path": "/workspace"})

        router._registry.call_tool.assert_awaited_once_with(
            "ast-repo-map.map_repo", {"root_path": "/workspace"}
        )
        assert result["ok"] is True
        assert "mapped" in result["result"]

    @pytest.mark.asyncio
    async def test_execute_returns_timeout_when_dispatch_hangs(self, router):
        """execute() returns a timeout error if the dispatch doesn't complete in time."""

        async def _hang(*args, **kwargs):
            await asyncio.sleep(10)

        router._registry.call_tool.side_effect = _hang
        router._call_timeout = 0.05  # Very short timeout

        result = await router.execute("ast-repo-map", "map_repo", {"root_path": "/"})

        assert result["ok"] is False
        assert "timed out" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_skill_unavailable(self, router):
        """execute() returns a shaped error if the skill/tool is unavailable."""
        from services.skill_runner.skill_registry import SkillUnavailable

        router._registry.call_tool.side_effect = SkillUnavailable("skill.tool")

        result = await router.execute("skill", "tool", {})

        assert result["ok"] is False
        assert result["error"] == "skill_unavailable"

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_registry_exception(self, router):
        """execute() returns error if the registry call raises unexpectedly."""
        router._registry.call_tool.side_effect = RuntimeError("skill process crashed")

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
                router,
                "plan_tool_call",
                new_callable=AsyncMock,
                return_value={"tool": "map_repo", "arguments": {"path": "/ws"}},
            ):
                with patch.object(
                    router, "execute", new_callable=AsyncMock, return_value=execute_result
                ):
                    result = await router.run("Map the repository")

                    assert result is not None
                    assert result["ok"] is True
                    assert result["result"] == "success"

    @pytest.mark.asyncio
    async def test_run_returns_none_on_exception(self, router):
        """run() returns None if any step raises an exception."""
        with patch.object(
            router, "select", new_callable=AsyncMock, side_effect=RuntimeError("oops")
        ):
            result = await router.run("task")
            assert result is None

    @pytest.mark.asyncio
    async def test_run_emits_tool_start_and_done(self, router):
        """run() emits tool.start and tool.done events with reasoning."""
        router.select = AsyncMock(return_value="ast-repo-map")
        router._last_reasoning = "the task asks to map the repo"
        router.plan_tool_call = AsyncMock(return_value={"tool": "map", "arguments": {"path": "."}})
        router.execute = AsyncMock(
            return_value={"ok": True, "result": {"content": [{"text": "ok"}]}}
        )

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
        """End-to-end test: select -> plan -> execute with a mocked in-process registry."""
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

        # Mock the SkillRegistry — call_tool returns the raw (already-jsonable) result.
        registry = AsyncMock()
        registry.call_tool.return_value = "work done"

        router = SkillRouter(
            runner=runner,
            registry=registry,
            gemma_api_base="http://localhost:8000/v1",
            call_timeout=5.0,
        )

        # Mock acompletion calls
        # Two-tier select: 3 unanimous calls, then 1 plan call
        select_response = _make_tool_call_response("test-skill")
        plan_response = _make_mock_acompletion_response(
            '{"tool": "do_work", "arguments": {"input": "data"}}'
        )

        responses = [select_response, select_response, select_response, plan_response]
        call_count = [0]

        async def mock_acomp(*args, **kwargs):
            resp = responses[call_count[0]]
            call_count[0] += 1
            return resp

        with patch(
            "services.orchestrator.skill_router.litellm.acompletion", side_effect=mock_acomp
        ):
            result = await router.run("Do work")

            assert result is not None
            assert result["ok"] is True
            assert result["result"] == "work done"
            registry.call_tool.assert_awaited_once_with("test-skill.do_work", {"input": "data"})

    def test_skill_router_requires_non_none_registry(self):
        """Regression test: SkillRouter must receive a non-None registry."""
        runner = MagicMock(spec=SkillRunner)
        runner.catalog = {"test-skill": MagicMock()}

        # This should not raise at construction, but registry must be non-None
        router = SkillRouter(
            runner=runner,
            registry=AsyncMock(),
            gemma_api_base="http://localhost:8000/v1",
        )

        assert router._registry is not None


@pytest.mark.mocked
class TestTwoTierSelect:
    @pytest.fixture
    def router(self):
        runner = MagicMock(spec=SkillRunner)
        runner.catalog = {
            "ast-repo-map": SkillMeta(
                "ast-repo-map", "Map a repo", Path("/fake/SKILL.md"), "bundled"
            ),
            "web-search": SkillMeta(
                "web-search", "Search the web", Path("/fake/SKILL.md"), "bundled"
            ),
        }
        runner.catalog_prompt.return_value = (
            "- ast-repo-map: Map a repo\n- web-search: Search the web"
        )
        runner.tool_schema.return_value = {"type": "function", "function": {"name": "load_skill"}}
        return SkillRouter(
            runner=runner, registry=AsyncMock(), gemma_api_base="http://localhost:8000/v1"
        )

    @pytest.mark.asyncio
    async def test_unanimous_returns_immediately_without_tiebreak(self, router):
        """3 agreeing samples → return pick; no 4th (tiebreak) call."""
        resp = _make_tool_call_response("web-search")
        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new=AsyncMock(return_value=resp),
        ) as mac:
            pick = await router.select("find recent papers")
        assert pick == "web-search"
        # Exactly SELECT_ATTEMPTS (3) calls — no tiebreak sample.
        assert mac.await_count == 3
        # All three used a zero thinking budget.
        for call in mac.await_args_list:
            assert call.kwargs["extra_body"]["thinking_budget_tokens"] == 0

    @pytest.mark.asyncio
    async def test_disagreement_runs_tiebreak_with_budget(self, router):
        """Disagreeing samples → one extra tiebreak call with budget=1024."""
        responses = [
            _make_tool_call_response("web-search"),
            _make_tool_call_response("ast-repo-map"),
            _make_tool_call_response("web-search"),
            _make_tool_call_response("ast-repo-map"),  # tiebreak result
        ]
        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new=AsyncMock(side_effect=responses),
        ) as mac:
            pick = await router.select("ambiguous task")
        assert pick == "ast-repo-map"
        assert mac.await_count == 4  # 3 samples + 1 tiebreak
        # The 4th call uses the thinking budget.
        assert mac.await_args_list[3].kwargs["extra_body"]["thinking_budget_tokens"] == 1024

    @pytest.mark.asyncio
    async def test_returns_none_when_no_sample_picks(self, router):
        """No sample emits a valid catalog pick → None, no tiebreak."""
        no_call = MagicMock()
        no_call.choices = [MagicMock()]
        no_call.choices[0].message.tool_calls = None
        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new=AsyncMock(return_value=no_call),
        ) as mac:
            pick = await router.select("nothing matches")
        assert pick is None
        assert mac.await_count == 3  # no tiebreak when there are zero picks

    @pytest.mark.asyncio
    async def test_sample_select_returns_validated_pick(self, router):
        """_sample_select returns a catalog-valid name, ignores out-of-catalog."""
        good = _make_tool_call_response("web-search")
        bad = _make_tool_call_response("not-a-real-skill")
        with patch(
            "services.orchestrator.skill_router.litellm.acompletion",
            new=AsyncMock(side_effect=[good, bad]),
        ):
            assert await router._sample_select("t", 0) == "web-search"
            assert await router._sample_select("t", 0) is None


@pytest.mark.mocked
class TestSkillRouterTelemetry:
    """run() records skill use best-effort without changing dispatch behavior."""

    @pytest.fixture
    def router(self, tmp_path):
        runner = MagicMock(spec=SkillRunner)
        runner.catalog = {"web-search": MagicMock()}
        registry = AsyncMock()
        r = SkillRouter(
            runner=runner,
            registry=registry,
            gemma_api_base="http://localhost:8000/v1",
            telemetry_path=tmp_path / "tele.json",
        )
        return r

    @pytest.mark.asyncio
    async def test_run_records_success(self, router, tmp_path):
        from services.orchestrator import skill_telemetry as st

        router.select = AsyncMock(return_value="web-search")
        router.plan_tool_call = AsyncMock(return_value={"tool": "search", "arguments": {}})
        router.execute = AsyncMock(return_value={"ok": True, "result": "done"})

        result = await router.run("find papers")

        assert result["ok"] is True
        store = st.load(tmp_path / "tele.json")
        assert store["skills"]["web-search"]["use_count"] == 1
        assert store["skills"]["web-search"]["success_count"] == 1

    @pytest.mark.asyncio
    async def test_run_records_failure(self, router, tmp_path):
        from services.orchestrator import skill_telemetry as st

        router.select = AsyncMock(return_value="web-search")
        router.plan_tool_call = AsyncMock(return_value={"tool": "search", "arguments": {}})
        router.execute = AsyncMock(return_value={"ok": False, "error": "timeout"})

        await router.run("find papers")

        store = st.load(tmp_path / "tele.json")
        assert store["skills"]["web-search"]["fail_count"] == 1
        assert store["skills"]["web-search"]["success_count"] == 0

    @pytest.mark.asyncio
    async def test_run_unaffected_by_telemetry_failure(self, router):
        """A telemetry exception must NOT break the dispatch return value."""
        from services.orchestrator import skill_router as sr

        router.select = AsyncMock(return_value="web-search")
        router.plan_tool_call = AsyncMock(return_value={"tool": "search", "arguments": {}})
        router.execute = AsyncMock(return_value={"ok": True, "result": "done"})

        with patch.object(sr, "record_use_best_effort", side_effect=RuntimeError("telemetry boom")):
            # record_use_best_effort itself swallows, but even if a future
            # change made it raise, run() wraps the call defensively.
            result = await router.run("find papers")

        assert result is not None and result["ok"] is True
