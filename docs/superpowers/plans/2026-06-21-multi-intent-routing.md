# Multi-Intent Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach Labmate's skill router to decompose compound multi-intent prompts into sub-intents, validate each maps to a known skill, and pause for clarification when routing confidence is low rather than guessing.

**Architecture:** A new `route()` method in `SkillRouter` runs a decompose → per-sub-intent confidence-check → solvability-gate → (clarify | route) pipeline, returning a `RouteResult`. The existing single-intent `select()` path is left byte-for-byte unchanged as the fallback. The LangGraph `plan` node calls `route()`, emits a `clarification_request` event and sets `awaiting_clarification` when the router cannot confidently route; otherwise it expands the matched skills into a chain of sequential child Goals.

**Tech Stack:** Python 3.12, `litellm` (Gemma 4 31B via OpenAI-compatible API), `redis.asyncio`, LangGraph, `pytest` + `pytest-asyncio`, `unittest.mock`.

---

## Conventions for every task

These apply to every step below — do not repeat them inline.

- **litellm calls**: always `model="openai/gemma-4-31b"`, `api_base=self._gemma_base`, `api_key="not-needed"`, and `extra_body={"thinking_budget_tokens": N}`.
- **No stdout writes** anywhere. All diagnostics go to `_log` (logger name `skill_router`, which writes to stderr).
- **Events** are emitted with the module-level `events.emit(...)` (no-op when no emitter is set, e.g. in unit tests — so tests never need to mock it).
- **Backward compatibility is mandatory**: `select()`, `_sample_select()`, `plan_tool_call()`, `execute()`, and `run()` MUST remain exactly as they are today. New behavior lives only in the new methods.
- **Fail-open philosophy** for `decompose()`: any parse/LLM error returns `[task]` (route the single original intent) rather than raising.
- **Test isolation**: new tests go in NEW files only. Do NOT modify any existing test file.
- **Run tests from the repo root** (`/Users/zachstallbohm/Work/Labmate`).
- **Commit after each task passes**, using the exact message shown in the step.

### Shared test fixtures (used by several tasks)

Both new test files use these helpers. They are defined at the top of `tests/services/orchestrator/test_skill_router_multi_intent.py`; `test_graph_multi_intent.py` redefines the ones it needs locally (the two test files must not import from each other).

```python
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
```

---

## Task 1 — `RouteResult` dataclass

- [ ] **1.1 Write the failing test**

Create `tests/services/orchestrator/test_skill_router_multi_intent.py` with the shared fixtures above, then append:

```python
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
```

- [ ] **1.2 Run to confirm failure**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_skill_router_multi_intent.py -q
```

Expected: collection/import error — `ImportError: cannot import name 'RouteResult' from 'services.orchestrator.skill_router'` (3 errors).

- [ ] **1.3 Implement**

In `services/orchestrator/skill_router.py`, add the dataclass import to the existing imports block and define `RouteResult` directly below the module constants (`PLAN_ATTEMPTS = 3`), before `class SkillRouter`.

Add to the top-of-file imports:

```python
from dataclasses import dataclass, field
```

Add after `PLAN_ATTEMPTS = 3`:

```python
# Confidence threshold for accepting a sub-intent's routing without clarification.
# 0.67 == "at least 2 of 3 samples agreed". Below this (and not unanimous) we treat
# the sub-intent as ambiguous and ask the user rather than guessing.
CONFIDENCE_THRESHOLD = 0.67


@dataclass
class RouteResult:
    """Outcome of multi-intent routing.

    skills and sub_intents are positionally parallel: skills[i] is the skill
    chosen for sub_intents[i]. When needs_clarification is True, skills is empty
    and clarification_question holds the single question to ask the user.
    """
    skills: list[str]
    needs_clarification: bool = False
    clarification_question: str = ""
    sub_intents: list[str] = field(default_factory=list)
```

- [ ] **1.4 Run to confirm pass**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_skill_router_multi_intent.py -q
```

Expected: `3 passed`.

- [ ] **1.5 Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/skill_router.py tests/services/orchestrator/test_skill_router_multi_intent.py
git commit -m "Add RouteResult dataclass for multi-intent routing"
```

---

## Task 2 — `decompose(task)`

- [ ] **2.1 Write the failing test**

Append to `tests/services/orchestrator/test_skill_router_multi_intent.py`:

```python
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
async def test_decompose_uses_budget_512():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch("services.orchestrator.skill_router.litellm.acompletion") as m:
        m.return_value = content_response('["x"]')
        await router.decompose("x")
    kwargs = m.call_args.kwargs
    assert kwargs["extra_body"] == {"thinking_budget_tokens": 512}
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
```

- [ ] **2.2 Run to confirm failure**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_skill_router_multi_intent.py -q -k decompose
```

Expected: `AttributeError: 'SkillRouter' object has no attribute 'decompose'` (9 failures).

- [ ] **2.3 Implement**

Add the prompt constant after the `RouteResult` definition (module scope):

```python
_DECOMPOSE_PROMPT = (
    "You are a task decomposer for an AI agent. Split the following task into the "
    "minimum number of independent sub-tasks, each of which can be handled by a "
    "single specialized skill.\n\n"
    "Rules:\n"
    "- If the task needs only ONE skill, return a list with ONE element (the original "
    "task, possibly rephrased).\n"
    "- If the task needs multiple skills, return each as a separate, self-contained "
    "sub-task with enough context to be routed independently.\n"
    "- Maximum 4 sub-tasks. If you cannot decompose, return the original task as a "
    "single-element list.\n"
    "- Reply with ONLY a JSON array of strings. No markdown, no explanation.\n\n"
    "Task: {task}"
)
```

Add the method to `SkillRouter` (place it after `select()`, before `plan_tool_call()`):

```python
async def decompose(self, task: str) -> list[str]:
    """Split a compound task into sub-intents (fail-open to [task]).

    One litellm call at thinking_budget=512 asking for a JSON array of sub-tasks.
    Strips code fences, json.loads, validates it is a list of non-empty strings,
    caps at 4. Any error returns [task] — routing a single intent is always
    preferable to crashing the pipeline.
    """
    prompt = _DECOMPOSE_PROMPT.format(task=task)
    try:
        r = await litellm.acompletion(
            model="openai/gemma-4-31b",
            api_base=self._gemma_base,
            api_key="not-needed",
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking_budget_tokens": 512},
        )
    except Exception as exc:
        _log.warning("decompose() llm error: %s", exc)
        return [task]
    choices = getattr(r, "choices", None)
    if not choices:
        return [task]
    raw = getattr(getattr(choices[0], "message", None), "content", None)
    if not raw:
        return [task]
    raw = raw.strip()
    if raw.startswith("```json"):
        raw = raw[7:]
    elif raw.startswith("```"):
        raw = raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _log.warning("decompose() non-JSON response; routing single intent")
        return [task]
    if not isinstance(parsed, list):
        return [task]
    cleaned = [s.strip() for s in parsed if isinstance(s, str) and s.strip()]
    if not cleaned:
        return [task]
    return cleaned[:4]
```

- [ ] **2.4 Run to confirm pass**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_skill_router_multi_intent.py -q -k decompose
```

Expected: `9 passed`.

- [ ] **2.5 Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/skill_router.py tests/services/orchestrator/test_skill_router_multi_intent.py
git commit -m "Add decompose() to split compound prompts into sub-intents"
```

---

## Task 3 — `_validate_solvable(sub_intent)`

- [ ] **3.1 Write the failing test**

Append:

```python
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
```

- [ ] **3.2 Run to confirm failure**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_skill_router_multi_intent.py -q -k validate_solvable
```

Expected: `AttributeError: 'SkillRouter' object has no attribute '_validate_solvable'` (3 failures).

- [ ] **3.3 Implement**

Add after `decompose()`:

```python
async def _validate_solvable(self, sub_intent: str) -> bool:
    """Solvability gate: True iff a single zero-budget sample picks a known skill.

    Sub-intents that map to no skill are flagged for clarification rather than
    silently dropped (AOP-style — no blind dispatch).
    """
    return await self._sample_select(sub_intent, 0) is not None
```

- [ ] **3.4 Run to confirm pass**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_skill_router_multi_intent.py -q -k validate_solvable
```

Expected: `3 passed`.

- [ ] **3.5 Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/skill_router.py tests/services/orchestrator/test_skill_router_multi_intent.py
git commit -m "Add _validate_solvable solvability gate for sub-intents"
```

---

## Task 4 — `_confidence_check(sub_intent)`

- [ ] **4.1 Write the failing test**

Append:

```python
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
```

- [ ] **4.2 Run to confirm failure**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_skill_router_multi_intent.py -q -k confidence_check
```

Expected: `AttributeError: 'SkillRouter' object has no attribute '_confidence_check'` (6 failures).

- [ ] **4.3 Implement**

Add after `_validate_solvable()`:

```python
async def _confidence_check(self, sub_intent: str) -> tuple[str | None, float]:
    """Run SELECT_ATTEMPTS zero-budget samples; return (winning_skill, confidence).

    confidence = (votes for the winning skill) / SELECT_ATTEMPTS. None samples
    count against confidence (they are part of the denominator) so a sub-intent
    that only sometimes matches a skill reads as low-confidence. Returns
    (None, 0.0) when no sample picked any skill.
    """
    from collections import Counter

    picks = [await self._sample_select(sub_intent, 0) for _ in range(SELECT_ATTEMPTS)]
    hits = [p for p in picks if p is not None]
    if not hits:
        return None, 0.0
    winner, votes = Counter(hits).most_common(1)[0]
    confidence = votes / SELECT_ATTEMPTS
    return winner, confidence
```

- [ ] **4.4 Run to confirm pass**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_skill_router_multi_intent.py -q -k confidence_check
```

Expected: `6 passed`.

- [ ] **4.5 Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/skill_router.py tests/services/orchestrator/test_skill_router_multi_intent.py
git commit -m "Add _confidence_check majority-vote routing scorer"
```

---

## Task 5 — `_generate_clarification(task, ambiguous_sub_intents)`

- [ ] **5.1 Write the failing test**

Append:

```python
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
```

- [ ] **5.2 Run to confirm failure**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_skill_router_multi_intent.py -q -k generate_clarification
```

Expected: `AttributeError: 'SkillRouter' object has no attribute '_generate_clarification'` (5 failures).

- [ ] **5.3 Implement**

Add the prompt constant at module scope (below `_DECOMPOSE_PROMPT`):

```python
_CLARIFY_PROMPT = (
    "You are an AI agent that could not confidently choose a skill for part of a "
    "user's request. Write a SINGLE, short clarifying question whose answer would "
    "tell you which approach the user wants for the ambiguous parts.\n\n"
    "Reply with ONLY the question text. No preamble, no markdown.\n\n"
    "Original task: {task}\n\n"
    "Ambiguous parts:\n{parts}"
)
_CLARIFY_FALLBACK = "Could you clarify what you'd like me to do here?"
```

Add the method after `_confidence_check()`:

```python
async def _generate_clarification(self, task: str, ambiguous_sub_intents: list[str]) -> str:
    """Ask Gemma for one clarifying question for the ambiguous sub-intents.

    One litellm call at thinking_budget=256. Returns a generic fallback question
    on any error (we still need *a* question to pause on).
    """
    parts = "\n".join(f"- {s}" for s in ambiguous_sub_intents)
    prompt = _CLARIFY_PROMPT.format(task=task, parts=parts)
    try:
        r = await litellm.acompletion(
            model="openai/gemma-4-31b",
            api_base=self._gemma_base,
            api_key="not-needed",
            messages=[{"role": "user", "content": prompt}],
            extra_body={"thinking_budget_tokens": 256},
        )
        choices = getattr(r, "choices", None)
        if not choices:
            return _CLARIFY_FALLBACK
        text = getattr(getattr(choices[0], "message", None), "content", None)
        text = (text or "").strip()
        return text or _CLARIFY_FALLBACK
    except Exception as exc:
        _log.warning("_generate_clarification() error: %s", exc)
        return _CLARIFY_FALLBACK
```

- [ ] **5.4 Run to confirm pass**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_skill_router_multi_intent.py -q -k generate_clarification
```

Expected: `5 passed`.

- [ ] **5.5 Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/skill_router.py tests/services/orchestrator/test_skill_router_multi_intent.py
git commit -m "Add _generate_clarification to produce a single disambiguating question"
```

---

## Task 6 — `route(task)` orchestration

- [ ] **6.1 Write the failing test**

Append:

```python
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
        result = await router.route("search a dataset and generate examples")
    assert result.needs_clarification is False
    assert result.skills == ["dataset-search", "synthetic-gen"]
    assert result.sub_intents == ["search dataset", "generate examples"]


@pytest.mark.asyncio
async def test_route_low_confidence_triggers_clarification():
    from services.orchestrator.skill_router import SkillRouter

    router = SkillRouter(make_runner(), make_redis(), "http://test/v1")
    with patch.object(router, "decompose", AsyncMock(return_value=["ambiguous thing"])), \
         patch.object(router, "_confidence_check",
                      AsyncMock(return_value=("dataset-search", 1 / 3))), \
         patch.object(router, "_generate_clarification",
                      AsyncMock(return_value="Which one?")):
        result = await router.route("ambiguous thing")
    assert result.needs_clarification is True
    assert result.skills == []
    assert result.clarification_question == "Which one?"


@pytest.mark.asyncio
async def test_route_unsolvable_subintent_triggers_clarification():
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
        result = await router.route("search dataset and do undefined thing")
    assert result.needs_clarification is True
    assert result.skills == []
    # Only the unsolvable sub-intent is handed to the clarifier.
    gc.assert_awaited_once()
    assert gc.call_args.args[1] == ["do undefined thing"]


@pytest.mark.asyncio
async def test_route_clarifier_receives_all_flagged():
    """Both an unsolvable and a low-confidence sub-intent are passed to clarifier."""
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
        result = await router.route("sub a sub b sub c")
    assert result.needs_clarification is True
    assert gc.call_args.args[1] == ["sub b", "sub c"]


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
```

- [ ] **6.2 Run to confirm failure**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_skill_router_multi_intent.py -q -k "test_route_"
```

Expected: `AttributeError: 'SkillRouter' object has no attribute 'route'` (6 failures).

- [ ] **6.3 Implement**

Add `route()` to `SkillRouter` immediately after `select()` (keeping `select()` untouched above it):

```python
async def route(self, task: str) -> RouteResult:
    """Multi-intent routing: decompose, confidence-check each, clarify if unsure.

    Pipeline:
      1. decompose(task) -> sub_intents
      2. for each: _confidence_check(); flag if no skill OR confidence below
         CONFIDENCE_THRESHOLD
      3. if anything flagged: _generate_clarification() and return a clarification
         RouteResult (no blind dispatch)
      4. else: return ordered skills parallel to sub_intents

    select() (single-intent path) is intentionally left unchanged; route() is the
    new entry point for the multi-intent flow.
    """
    sub_intents = await self.decompose(task)

    routed_skills: list[str] = []
    flagged: list[str] = []
    for sub in sub_intents:
        skill, confidence = await self._confidence_check(sub)
        if skill is None or confidence < CONFIDENCE_THRESHOLD:
            flagged.append(sub)
        else:
            routed_skills.append(skill)

    if flagged:
        question = await self._generate_clarification(task, flagged)
        _log.info("route() needs clarification for %d sub-intent(s)", len(flagged))
        return RouteResult(
            skills=[],
            needs_clarification=True,
            clarification_question=question,
            sub_intents=sub_intents,
        )

    _log.info("route() resolved %d sub-intent(s) to skills: %s",
              len(routed_skills), routed_skills)
    await events.emit(
        "reasoning",
        node="route",
        summary=events.reasoning_summary(self._last_reasoning),
        text=self._last_reasoning,
    )
    return RouteResult(skills=routed_skills, sub_intents=sub_intents)
```

> Note: when any sub-intent is flagged, `route()` returns immediately with the full `sub_intents` list (so the caller has context) but an empty `skills` list — partial routing is never dispatched.

- [ ] **6.4 Run to confirm pass**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_skill_router_multi_intent.py -q
```

Expected: all tests in the file pass (Tasks 1–6 combined; `32 passed`).

- [ ] **6.5 Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/skill_router.py tests/services/orchestrator/test_skill_router_multi_intent.py
git commit -m "Add route() multi-intent orchestration with clarification gate"
```

---

## Task 7 — New `State` fields in `types.py`

- [ ] **7.1 Write the failing test**

Add these to a NEW file `tests/services/orchestrator/test_graph_multi_intent.py`. Start the file with its own local fixtures (do not import from the other test file):

```python
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_state_has_clarification_fields():
    """The new TypedDict keys must be declared (annotations present)."""
    from services.orchestrator.types import State

    annotations = State.__annotations__
    assert "awaiting_clarification" in annotations
    assert "clarification_question" in annotations


def test_state_clarification_fields_optional():
    """State is total=False, so a State without the new keys is still valid at runtime."""
    from services.orchestrator.types import State

    s: State = {"session_id": "abc"}
    assert s["session_id"] == "abc"
    s["awaiting_clarification"] = True
    s["clarification_question"] = "Which?"
    assert s["awaiting_clarification"] is True
    assert s["clarification_question"] == "Which?"
```

- [ ] **7.2 Run to confirm failure**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_graph_multi_intent.py -q -k state
```

Expected: `test_state_has_clarification_fields` fails with `AssertionError` (keys missing). (`test_state_clarification_fields_optional` may pass already since `total=False` permits arbitrary keys at runtime — that is acceptable; the meaningful gate is the annotations test.)

- [ ] **7.3 Implement**

In `services/orchestrator/types.py`, inside `class State(TypedDict, total=False)`, add the two fields after the existing `critique_notes` line:

```python
    # Multi-intent routing clarification gate
    awaiting_clarification: bool      # True when route() needs user input before proceeding
    clarification_question: str       # the single question to surface to the user
```

- [ ] **7.4 Run to confirm pass**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_graph_multi_intent.py -q -k state
```

Expected: `2 passed`.

- [ ] **7.5 Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/types.py tests/services/orchestrator/test_graph_multi_intent.py
git commit -m "Add awaiting_clarification + clarification_question State fields"
```

---

## Task 8 — Wire `route()` into the `plan` node

> **Before writing tests:** read the current `plan` node in `services/orchestrator/graph.py` end to end. The plan node references an orchestrator object (`orch`) with a `skill_router` attribute and builds a catalog prompt. The change is additive: after the existing catalog assembly, branch on `orch.skill_router.route(goal_desc)`. Match the actual variable names in the file (`goal_desc`, `state`, the goal-tree mutation helpers `create_goal` / `update_status`) — the snippets below use the names from the spec; reconcile them with what the file actually uses before implementing. If the plan node currently calls `select()`, replace that call site with `route()`; if it calls `run()` or `plan_and_dispatch`, leave those alone and only add the route()-driven Goal expansion.

- [ ] **8.1 Write the failing test**

Append to `tests/services/orchestrator/test_graph_multi_intent.py`:

```python
def _import_plan():
    """Import the plan node. The graph module wires nodes via make_nodes/build_graph;
    grab the plan coroutine however the module exposes it. Adjust this helper to the
    real export (module-level `plan`, or via make_nodes) when implementing."""
    from services.orchestrator import graph as graph_mod
    return graph_mod


@pytest.mark.asyncio
async def test_plan_emits_clarification_request_when_needed(monkeypatch):
    """When route() needs clarification, plan emits clarification_request and sets the flag."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult

    captured: list[tuple[str, dict]] = []

    async def fake_emit(type, **fields):
        captured.append((type, fields))

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    route_result = RouteResult(
        skills=[],
        needs_clarification=True,
        clarification_question="Search or generate?",
        sub_intents=["search", "generate"],
    )
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"
    monkeypatch.setattr(graph_mod.orch, "skill_router", fake_router, raising=False)

    state = {
        "session_id": "s1",
        "goal_tree": {
            "root": {
                "id": "root", "parent_id": None, "children": [],
                "description": "search a dataset and generate examples",
                "status": "PENDING", "result": None, "error": None,
                "attempts": 0, "started_at": None, "updated_at": None,
            }
        },
    }

    out = await graph_mod.plan(state)

    assert out.get("awaiting_clarification") is True
    assert out.get("clarification_question") == "Search or generate?"
    types_emitted = [t for t, _ in captured]
    assert "clarification_request" in types_emitted
    clar = next(f for t, f in captured if t == "clarification_request")
    assert clar["question"] == "Search or generate?"
    assert clar["task"] == "search a dataset and generate examples"
    assert clar["session_id"] == "s1"


@pytest.mark.asyncio
async def test_plan_expands_skills_into_sequential_child_goals(monkeypatch):
    """When route() returns skills, plan adds one child Goal per skill, chained."""
    from services.orchestrator import graph as graph_mod
    from services.orchestrator.skill_router import RouteResult

    async def fake_emit(type, **fields):
        pass

    monkeypatch.setattr(graph_mod.events, "emit", fake_emit)

    route_result = RouteResult(
        skills=["dataset-search", "synthetic-gen"],
        needs_clarification=False,
        sub_intents=["search a dataset", "generate examples"],
    )
    fake_router = MagicMock()
    fake_router.route = AsyncMock(return_value=route_result)
    fake_router.runner.catalog_prompt.return_value = "CATALOG"
    monkeypatch.setattr(graph_mod.orch, "skill_router", fake_router, raising=False)

    state = {
        "session_id": "s1",
        "goal_tree": {
            "root": {
                "id": "root", "parent_id": None, "children": [],
                "description": "search a dataset and generate examples",
                "status": "PENDING", "result": None, "error": None,
                "attempts": 0, "started_at": None, "updated_at": None,
            }
        },
    }

    out = await graph_mod.plan(state)

    assert out.get("awaiting_clarification") in (False, None)
    tree = out["goal_tree"]
    children = tree["root"]["children"]
    assert len(children) == 2
    descs = [tree[c]["description"] for c in children]
    assert descs == ["search a dataset", "generate examples"]
    # Sequential: second child depends on the first (encoded as the first being a
    # child/parent of the second). Verify the chain via parent_id linkage.
    first, second = children
    assert tree[second]["parent_id"] == first
```

> If the real `plan` node is not exported as `graph_mod.plan` or `orch` is not a module attribute, adjust `_import_plan` / the `monkeypatch.setattr` targets to the actual structure discovered in the pre-task read. The assertions on behavior (event emitted, flag set, children created and chained) stay the same.

- [ ] **8.2 Run to confirm failure**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_graph_multi_intent.py -q -k "plan_emits or plan_expands"
```

Expected: failures — either the plan node still calls `select()` (no `route` attribute used / no clarification flag in output) or child Goals are not created. Confirm the failure is about the new behavior, not an import error (fix the monkeypatch targets first if it is an import/attribute error).

- [ ] **8.3 Implement**

In `services/orchestrator/graph.py`, in the `plan` node, after the existing catalog-prompt assembly and the `if getattr(orch, "skill_router", None) is not None:` guard, replace the `select()`-based path with the `route()`-based path. Use the helpers already imported in `graph.py` (`create_goal`, `update_status`, `now_iso` from `types`, plus `uuid`):

```python
        route_result = await orch.skill_router.route(goal_desc)

        if route_result.needs_clarification:
            await events.emit(
                "clarification_request",
                question=route_result.clarification_question,
                task=goal_desc,
                session_id=state.get("session_id", ""),
            )
            return {
                "awaiting_clarification": True,
                "clarification_question": route_result.clarification_question,
            }

        # Confident multi-intent route: one sequential child Goal per skill.
        tree = state["goal_tree"]
        root_id = current_goal_id  # the id of the goal being planned (use the file's var)
        prev_id: str | None = None
        for skill_name, sub_intent in zip(route_result.skills, route_result.sub_intents):
            child_id = uuid.uuid4().hex[:12]
            # Chain sequentially: each child's parent is the previous child, so a
            # child only becomes ready once its predecessor COMPLETED (get_ready_goals
            # requires all children COMPLETED). The first child hangs off the root.
            parent = prev_id if prev_id is not None else root_id
            create_goal(tree, child_id, parent, sub_intent)
            prev_id = child_id

        return {"goal_tree": tree, "awaiting_clarification": False}
```

Notes for the implementer:
- Use the actual goal id variable the `plan` node already has for the goal being planned (the spec calls it `goal_desc` for description; find the matching id, e.g. `current_goal_id` or the loop variable over ready goals). The test wires a single root goal named `"root"`, and asserts the first child's parent is `"root"` and the second child's parent is the first child — match that chaining.
- `get_ready_goals` (in `types.py`) returns a PENDING goal only when all its children are COMPLETED. Chaining children parent→child means deeper children stay blocked until the shallower one completes — this is how "sequential" is enforced without new fields.
- Do not write to stdout. Keep all diagnostics on `_log`.
- Keep the single-intent `select()` fallback available for any code path that still needs it; this task only changes the `plan` node call site.

- [ ] **8.4 Run to confirm pass**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/test_graph_multi_intent.py -q
```

Expected: all tests in the file pass (`4 passed`).

- [ ] **8.5 Commit**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add services/orchestrator/graph.py tests/services/orchestrator/test_graph_multi_intent.py
git commit -m "Wire route() into plan node: clarification gate + sequential child goals"
```

---

## Task 9 — Full regression + no-regression verification

- [ ] **9.1 Run the entire orchestrator test suite**

```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/ -q
```

Expected: all tests pass, including the pre-existing suite. The 184-test baseline (from prior E2E work) must not regress. If any pre-existing test fails, the change broke backward compatibility — most likely `select()`, `_sample_select()`, or the `plan` node's existing behavior. Fix forward without modifying existing test files.

- [ ] **9.2 Confirm `select()` is byte-for-byte unchanged**

```bash
cd /Users/zachstallbohm/Work/Labmate
git log -p -- services/orchestrator/skill_router.py | grep -A40 "async def select"
```

Verify the diff for `select()` shows no modifications across this branch's commits (only additions of new methods around it). `select()`, `_sample_select()`, `plan_tool_call()`, `execute()`, and `run()` must be identical to their pre-branch form.

- [ ] **9.3 Confirm no stdout writes were introduced**

```bash
cd /Users/zachstallbohm/Work/Labmate
grep -nE "\bprint\(|sys\.stdout" services/orchestrator/skill_router.py services/orchestrator/graph.py services/orchestrator/types.py
```

Expected: no matches.

- [ ] **9.4 Final commit (if any fixes were needed in 9.1)**

```bash
cd /Users/zachstallbohm/Work/Labmate
git add -A
git commit -m "Fix regressions surfaced by full orchestrator suite"
```

If no fixes were needed, skip this commit.

---

## Done — summary of changes

| File | Change |
|------|--------|
| `services/orchestrator/skill_router.py` | + `RouteResult` dataclass, `CONFIDENCE_THRESHOLD`, `_DECOMPOSE_PROMPT`, `_CLARIFY_PROMPT`/`_CLARIFY_FALLBACK` constants; + methods `decompose`, `_validate_solvable`, `_confidence_check`, `_generate_clarification`, `route`. `select()` and all existing methods unchanged. |
| `services/orchestrator/types.py` | + `awaiting_clarification: bool`, `clarification_question: str` to `State`. |
| `services/orchestrator/graph.py` | `plan` node now calls `route()`: emits `clarification_request` + sets flag when unsure, else expands matched skills into a sequential chain of child Goals. |
| `tests/services/orchestrator/test_skill_router_multi_intent.py` | NEW — 32 tests covering `RouteResult`, `decompose`, `_validate_solvable`, `_confidence_check`, `_generate_clarification`, `route`. |
| `tests/services/orchestrator/test_graph_multi_intent.py` | NEW — tests for the new `State` fields and `plan` node clarification/expansion behavior. |
