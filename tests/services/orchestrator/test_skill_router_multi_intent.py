from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def make_runner(catalog: dict[str, str] | None = None) -> MagicMock:
    """A SkillRunner double with a catalog dict and the prompt/schema helpers."""
    catalog = catalog or {"dataset-search": "x", "synthetic-gen": "y"}
    runner = MagicMock()
    runner.catalog = catalog
    runner.catalog_prompt.return_value = "CATALOG"
    runner.tool_schema.return_value = {"type": "function", "function": {"name": "load_skill"}}
    return runner


def make_redis() -> MagicMock:
    """A redis.asyncio double; xadd/get are awaitable."""
    redis = MagicMock()
    redis.xadd = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return redis


def tool_call_response(skill_name: str) -> MagicMock:
    """A litellm response whose message carries a load_skill tool call for skill_name."""
    func = MagicMock()
    func.name = "load_skill"
    func.arguments = f'{{"name": "{skill_name}"}}'
    tc = MagicMock()
    tc.function = func
    message = MagicMock()
    message.tool_calls = [tc]
    message.reasoning_content = "because it matches"
    return MagicMock(choices=[MagicMock(message=message)])


def no_tool_response() -> MagicMock:
    """A litellm response with no tool call (no skill matched)."""
    message = MagicMock()
    message.tool_calls = None
    message.content = "I cannot help"
    return MagicMock(choices=[MagicMock(message=message)])


def content_response(text: str) -> MagicMock:
    """A litellm response carrying plain text content (used by decompose/clarification)."""
    message = MagicMock()
    message.content = text
    message.tool_calls = None
    return MagicMock(choices=[MagicMock(message=message)])


def test_route_result_defaults():
    from services.orchestrator.skill_router import RouteResult

    r = RouteResult(skills=["a", "b"])
    assert r.skills == ["a", "b"]
    assert r.needs_clarification is False
    assert r.clarification_question == ""
    assert r.sub_intents == []


def test_route_result_clarification():
    from services.orchestrator.skill_router import RouteResult

    r = RouteResult(
        skills=[],
        needs_clarification=True,
        clarification_question="Which dataset?",
        sub_intents=["search", "generate"],
    )
    assert r.needs_clarification is True
    assert r.clarification_question == "Which dataset?"
    assert r.sub_intents == ["search", "generate"]


def test_route_result_sub_intents_independent():
    """Default list must not be shared across instances (field(default_factory))."""
    from services.orchestrator.skill_router import RouteResult

    a = RouteResult(skills=[])
    b = RouteResult(skills=[])
    a.sub_intents.append("x")
    assert b.sub_intents == []


# NOTE: decompose() was removed (single-intent routing is now the only mode; the
# multi-intent decompose path + routing_mode A/B toggle were deleted after an A/B
# showed no quality benefit at higher cost). Its tests were removed with it.


@pytest.mark.asyncio
async def test_validate_solvable_true_when_skill_matches():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = tool_call_response("dataset-search")
        assert await router._validate_solvable("find a dataset") is True


@pytest.mark.asyncio
async def test_validate_solvable_false_when_no_skill():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = no_tool_response()
        assert await router._validate_solvable("do something undefined") is False


@pytest.mark.asyncio
async def test_validate_solvable_single_sample():
    """Solvability gate runs exactly one _sample_select call at budget 0."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = tool_call_response("dataset-search")
        await router._validate_solvable("find a dataset")
    assert m.call_count == 1
    assert m.call_args.kwargs["extra_body"] == {"thinking_budget_tokens": 0}


@pytest.mark.asyncio
async def test_confidence_check_unanimous():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = tool_call_response("dataset-search")
        skill, conf = await router._confidence_check("find a dataset")
    assert skill == "dataset-search"
    assert conf == 1.0
    assert m.call_count == 3


@pytest.mark.asyncio
async def test_confidence_check_majority():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.side_effect = [
            tool_call_response("dataset-search"),
            tool_call_response("dataset-search"),
            tool_call_response("synthetic-gen"),
        ]
        skill, conf = await router._confidence_check("find a dataset")
    assert skill == "dataset-search"
    assert conf == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_confidence_check_three_way_split():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(
        make_runner({"a": "x", "b": "y", "c": "z"}), make_redis(), "http://test/v1"
    )
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.side_effect = [
            tool_call_response("a"),
            tool_call_response("b"),
            tool_call_response("c"),
        ]
        skill, conf = await router._confidence_check("ambiguous")
    # Winner is whichever has the plurality (all tie at 1); confidence is 1/3.
    assert skill in {"a", "b", "c"}
    assert conf == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_confidence_check_no_skill():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = no_tool_response()
        skill, conf = await router._confidence_check("undefined")
    assert skill is None
    assert conf == 0.0


@pytest.mark.asyncio
async def test_confidence_check_partial_none():
    """Some samples return None; confidence is over the 3 attempts, not over hits."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.side_effect = [
            tool_call_response("dataset-search"),
            no_tool_response(),
            no_tool_response(),
        ]
        skill, conf = await router._confidence_check("mostly unmatched")
    assert skill == "dataset-search"
    assert conf == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_confidence_check_all_zero_budget():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = tool_call_response("dataset-search")
        await router._confidence_check("find a dataset")
    for call in m.call_args_list:
        assert call.kwargs["extra_body"] == {"thinking_budget_tokens": 0}


# NOTE: _generate_clarification() was removed — route() no longer clarifies
# (the assess_ambiguity node owns all clarification). Its tests were removed with it.


@pytest.mark.asyncio
async def test_route_single_intent_high_confidence():
    """One intent, confident skill → RouteResult with that skill, no clarification."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "_confidence_check",
                      AsyncMock(return_value=("dataset-search", 1.0))):
        result = await router.route("find a dataset")
    assert result.needs_clarification is False
    assert result.skills == ["dataset-search"]
    assert result.sub_intents == ["find a dataset"]
    assert result.clarification_question == ""


@pytest.mark.asyncio
async def test_route_no_skill_falls_through_to_direct_answer():
    """No confident skill → skills=[], needs_clarification False (direct-answer path).
    route() NEVER clarifies — the assess_ambiguity gate owns ambiguity."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "_confidence_check",
                      AsyncMock(return_value=(None, 0.0))):
        result = await router.route("explain something")
    assert result.needs_clarification is False
    assert result.skills == []
    assert result.sub_intents == ["explain something"]


@pytest.mark.asyncio
async def test_route_low_confidence_falls_through_to_direct_answer():
    """A below-threshold confidence is NOT a confident skill → direct-answer path
    (skills=[], needs_clarification False)."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "_confidence_check",
                      AsyncMock(return_value=("dataset-search", 1 / 3))):
        result = await router.route("ambiguous thing")
    assert result.needs_clarification is False
    assert result.skills == []
    assert result.sub_intents == ["ambiguous thing"]


@pytest.mark.asyncio
async def test_route_is_single_intent_only():
    """route() always treats the whole message as ONE intent: sub_intents == [task],
    regardless of how many '+' or 'and' appear in the task text (no decompose)."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "_confidence_check",
                      AsyncMock(return_value=("dataset-search", 1.0))):
        result = await router.route("search a dataset and generate examples")
    assert result.sub_intents == ["search a dataset and generate examples"]
    assert result.skills == ["dataset-search"]
    assert result.needs_clarification is False


@pytest.mark.asyncio
async def test_route_threshold_boundary_passes_at_two_thirds():
    """confidence exactly 0.67 (2/3) must be accepted (>= threshold)."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "_confidence_check",
                      AsyncMock(return_value=("dataset-search", 2 / 3))):
        result = await router.route("thing")
    assert result.needs_clarification is False
    assert result.skills == ["dataset-search"]
