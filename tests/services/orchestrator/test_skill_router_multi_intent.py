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


@pytest.mark.asyncio
async def test_decompose_multi_intent():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response('["search for a dataset", "generate examples"]')
        out = await router.decompose("search for a dataset and generate examples")
    assert out == ["search for a dataset", "generate examples"]


@pytest.mark.asyncio
async def test_decompose_single_intent():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response('["just one thing"]')
        out = await router.decompose("just one thing")
    assert out == ["just one thing"]


@pytest.mark.asyncio
async def test_decompose_strips_code_fences():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response('```json\n["a", "b"]\n```')
        out = await router.decompose("a and b")
    assert out == ["a", "b"]


@pytest.mark.asyncio
async def test_decompose_uses_configured_budget():
    # FIX 10 (A3): decompose()'s thinking budget is now DECOMPOSE_THINKING_BUDGET
    # (configurable, default 384 — was the hardcoded 512 this test originally pinned).
    from services.orchestrator.skill_router import SkillRouter, DECOMPOSE_THINKING_BUDGET

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response('["x"]')
        await router.decompose("x")
    kwargs = m.call_args.kwargs
    assert kwargs["extra_body"] == {"thinking_budget_tokens": DECOMPOSE_THINKING_BUDGET}
    assert kwargs["model"] == "openai/gemma-4-31b"
    assert kwargs["api_key"] == "not-needed"
    assert kwargs["api_base"] == "http://test/v1"


@pytest.mark.asyncio
async def test_decompose_fails_open_on_llm_error():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.side_effect = RuntimeError("boom")
        out = await router.decompose("original task")
    assert out == ["original task"]


@pytest.mark.asyncio
async def test_decompose_fails_open_on_bad_json():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response("not json at all")
        out = await router.decompose("original task")
    assert out == ["original task"]


@pytest.mark.asyncio
async def test_decompose_fails_open_on_non_list_json():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response('{"not": "a list"}')
        out = await router.decompose("original task")
    assert out == ["original task"]


@pytest.mark.asyncio
async def test_decompose_caps_at_four():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response('["a", "b", "c", "d", "e", "f"]')
        out = await router.decompose("a b c d e f")
    assert out == ["a", "b", "c", "d"]


@pytest.mark.asyncio
async def test_decompose_drops_non_strings_and_empty():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response('["good", 5, "", "  ", "also good"]')
        out = await router.decompose("task")
    assert out == ["good", "also good"]


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


@pytest.mark.asyncio
async def test_generate_clarification_returns_question():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response("Should I search an existing dataset or generate one?")
        q = await router._generate_clarification(
            "find or make a dataset", ["find or make a dataset"]
        )
    assert q == "Should I search an existing dataset or generate one?"


@pytest.mark.asyncio
async def test_generate_clarification_uses_budget_256():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response("a question?")
        await router._generate_clarification("task", ["ambiguous"])
    assert m.call_args.kwargs["extra_body"] == {"thinking_budget_tokens": 256}


@pytest.mark.asyncio
async def test_generate_clarification_strips_whitespace():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response("  trimmed?  \n")
        q = await router._generate_clarification("task", ["x"])
    assert q == "trimmed?"


@pytest.mark.asyncio
async def test_generate_clarification_fallback_on_error():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.side_effect = RuntimeError("boom")
        q = await router._generate_clarification("task", ["x", "y"])
    assert q  # non-empty fallback question
    assert isinstance(q, str)


@pytest.mark.asyncio
async def test_generate_clarification_includes_ambiguous_intents_in_prompt():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response("q?")
        await router._generate_clarification("big task", ["sub one", "sub two"])
    sent = m.call_args.kwargs["messages"][0]["content"]
    assert "sub one" in sent
    assert "sub two" in sent
    assert "big task" in sent


@pytest.mark.asyncio
async def test_route_single_intent_high_confidence():
    """One sub-intent, unanimous skill → RouteResult with that skill, no clarification."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose", AsyncMock(return_value=["find a dataset"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(return_value=("dataset-search", 1.0))):
        result = await router.route("find a dataset")
    assert result.needs_clarification is False
    assert result.skills == ["dataset-search"]
    assert result.sub_intents == ["find a dataset"]
    assert result.clarification_question == ""


@pytest.mark.asyncio
async def test_route_multi_intent_all_confident():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose",
                      AsyncMock(return_value=["search dataset", "generate examples"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(side_effect=[
                          ("dataset-search", 1.0),
                          ("synthetic-gen", 1.0),
                      ])):
        # mode="multi" explicit: default flipped to "single"; this covers the multi
        # decompose path (the fallback) with two confident sub-intents.
        result = await router.route("search a dataset and generate examples", mode="multi")
    assert result.needs_clarification is False
    assert result.skills == ["dataset-search", "synthetic-gen"]
    assert result.sub_intents == ["search dataset", "generate examples"]


@pytest.mark.asyncio
async def test_route_low_confidence_triggers_clarification():
    # Trigger refinement (FIX 5): a SINGLE skill-less/low-confidence intent is NOT
    # ambiguous — it is a trivial direct-answer task. route() now falls through
    # (skills=[], needs_clarification=False) instead of clarifying. Clarification is
    # reserved for genuine MULTI-intent ambiguity (see the multi-intent tests below).
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose", AsyncMock(return_value=["ambiguous thing"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(return_value=("dataset-search", 1 / 3))), \
         patch.object(router, "_generate_clarification",
                      AsyncMock(return_value="Which one?")) as gc:
        result = await router.route("ambiguous thing")
    assert result.needs_clarification is False
    assert result.skills == []
    # No clarification is generated for a single skill-less intent.
    gc.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_unsolvable_subintent_triggers_clarification():
    # FIX 11: route() no longer clarifies on skill-absence. A MULTI-intent task with a
    # skill-less sub-intent is NOT ambiguous (genuine ambiguity is owned by the
    # assess_ambiguity gate that runs before route). It now PROCEEDS: needs_clarification
    # is False, the resolved skills (partial) are returned, all sub_intents are kept, and
    # _generate_clarification is never awaited. (Was: asserted needs_clarification=True.)
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose",
                      AsyncMock(return_value=["search dataset", "do undefined thing"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(side_effect=[
                          ("dataset-search", 1.0),
                          (None, 0.0),
                      ])), \
         patch.object(router, "_generate_clarification",
                      AsyncMock(return_value="What does 'undefined thing' mean?")) as gc:
        # mode="multi" explicit: default flipped to "single"; this covers the multi
        # decompose path (the fallback) with a skill-less sub-intent.
        result = await router.route("search dataset and do undefined thing", mode="multi")
    assert result.needs_clarification is False
    # Partial skills resolved; the skill-less sub-intent is re-resolved by ReAct at exec.
    assert result.skills == ["dataset-search"]
    assert result.sub_intents == ["search dataset", "do undefined thing"]
    gc.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_clarifier_receives_all_flagged():
    """FIX 11: a MULTI-intent task with multiple flagged sub-intents PROCEEDS (does not
    clarify on skill-absence). needs_clarification is False, all sub_intents are kept, and
    _generate_clarification is never awaited. (Was: asserted needs_clarification=True with
    both flagged sub-intents passed to the clarifier.)"""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose",
                      AsyncMock(return_value=["sub a", "sub b", "sub c"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(side_effect=[
                          ("dataset-search", 1.0),   # confident
                          (None, 0.0),               # unsolvable
                          ("synthetic-gen", 1 / 3),  # low confidence
                      ])), \
         patch.object(router, "_generate_clarification",
                      AsyncMock(return_value="q?")) as gc:
        # mode="multi" explicit: default flipped to "single"; this covers the multi
        # decompose path (the fallback) with multiple flagged sub-intents.
        result = await router.route("sub a sub b sub c", mode="multi")
    assert result.needs_clarification is False
    assert result.skills == ["dataset-search"]
    assert result.sub_intents == ["sub a", "sub b", "sub c"]
    gc.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_threshold_boundary_passes_at_two_thirds():
    """confidence exactly 0.67 (2/3) must be accepted (>= threshold)."""
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose", AsyncMock(return_value=["thing"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(return_value=("dataset-search", 2 / 3))):
        result = await router.route("thing")
    assert result.needs_clarification is False
    assert result.skills == ["dataset-search"]
