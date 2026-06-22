"""
Skill Router — routes natural-language goals to skills via progressive disclosure.

Integrates the SkillRunner (already-implemented machinery) into the orchestrator
to:
  1. Use architect() + tool calling to select which skill to use (select)
  2. Load the skill body and ask for a specific tool call (plan_tool_call)
  3. Dispatch to the running Redis-Streams skill worker (execute)
  4. Poll for the result and return it (run)

All litellm calls use api_key="not-needed" and explicit thinking_budget_tokens.
No stdout writes; all logging goes to stderr.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import litellm
import redis.asyncio as aioredis

from services.skill_runner.skill_runner import SkillRunner
from services.orchestrator import events

_log = logging.getLogger("skill_router")

# Redis stream constants
SKILL_TASKS_STREAM = "labmate:skill-tasks"
RESULT_PREFIX = "labmate:result:"

# Retry budgets: the local Q4 model is non-deterministic, so a clearly-matching
# skill is occasionally missed on a single sample. Independent retries compound
# recall toward ~100% (precision is unaffected — we only accept catalog hits).
SELECT_ATTEMPTS = 3
PLAN_ATTEMPTS = 3

# Confidence threshold for accepting a sub-intent's routing without clarification.
# 2/3 (0.666...) == "at least 2 of 3 samples agreed". Below this (and not unanimous)
# we treat the sub-intent as ambiguous and ask the user rather than guessing.
CONFIDENCE_THRESHOLD = 2.0 / 3.0

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

_CLARIFY_PROMPT = (
    "You are an AI agent that could not confidently choose a skill for part of a "
    "user's request. Write a SINGLE, short clarifying question whose answer would "
    "tell you which approach the user wants for the ambiguous parts.\n\n"
    "Reply with ONLY the question text. No preamble, no markdown.\n\n"
    "Original task: {task}\n\n"
    "Ambiguous parts:\n{parts}"
)
_CLARIFY_FALLBACK = "Could you clarify what you'd like me to do here?"


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


class SkillRouter:
    """Routes natural-language goals to skills and manages the full lifecycle."""

    def __init__(
        self,
        runner: SkillRunner,
        redis: aioredis.Redis,
        gemma_api_base: str,
        *,
        call_timeout: float = 60.0,
    ) -> None:
        """
        Args:
            runner: SkillRunner instance with .discover() already called
            redis: redis.asyncio.Redis client for task dispatch and result polling
            gemma_api_base: base URL for Gemma 4 31B (e.g. http://localhost:8000/v1)
            call_timeout: max seconds to wait for a result
        """
        self._runner = runner
        self._redis = redis
        self._gemma_base = gemma_api_base
        self._call_timeout = call_timeout
        self._last_reasoning: str = ""

    @property
    def runner(self) -> SkillRunner:
        """Public accessor for the runner (SkillRunner with catalog and tool schema)."""
        return self._runner

    async def _sample_select(self, task: str, thinking_budget: int) -> str | None:
        catalog = self._runner.catalog_prompt()
        schema = self._runner.tool_schema()
        directive = (
            "You are a skill router. If ANY available skill is relevant to the "
            "user's task, you MUST call load_skill with that skill's name. Only "
            "decline to call a tool if truly no skill fits."
        )
        try:
            r = await litellm.acompletion(
                model="openai/gemma-4-31b",
                api_base=self._gemma_base,
                api_key="not-needed",
                messages=[
                    {"role": "system", "content": f"{directive}\n\n{catalog}"},
                    {"role": "user", "content": task},
                ],
                tools=[schema],
                tool_choice="auto",
                extra_body={"thinking_budget_tokens": thinking_budget},
            )
        except Exception as exc:
            _log.warning("_sample_select error: %s", exc)
            return None
        choices = getattr(r, "choices", None)
        if not choices:
            return None
        message = getattr(choices[0], "message", None)
        tool_calls = getattr(message, "tool_calls", None) if message else None
        if not tool_calls:
            return None
        for tc in tool_calls:
            func = getattr(tc, "function", None)
            if func is None or getattr(func, "name", None) != "load_skill":
                continue
            args_str = getattr(func, "arguments", "{}")
            if isinstance(args_str, str):
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    continue
            else:
                args = args_str or {}
            skill_name = args.get("name")
            if skill_name and skill_name in self._runner.catalog:
                self._last_reasoning = events.extract_reasoning(r)
                return skill_name
        return None

    async def select(self, task: str) -> str | None:
        """
        Ask Gemma 4 to select which skill to use for this task.

        Two-tier approach: first collect SELECT_ATTEMPTS independent samples with
        zero thinking budget. If unanimous, return immediately. If disagreement,
        run one tiebreak sample with full thinking budget.

        Args:
            task: Natural-language goal description

        Returns:
            Skill name (str) if selected, None otherwise
        """
        picks = [await self._sample_select(task, 0) for _ in range(SELECT_ATTEMPTS)]
        picks = [p for p in picks if p is not None]
        if not picks:
            return None
        if len(set(picks)) == 1:
            chosen = picks[0]
        else:
            chosen = await self._sample_select(task, 1024)
        if chosen is None:
            return None
        _log.info("selected skill: %s", chosen)
        await events.emit(
            "reasoning",
            node="route",
            summary=events.reasoning_summary(self._last_reasoning),
            text=self._last_reasoning,
        )
        return chosen

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

    async def _validate_solvable(self, sub_intent: str) -> bool:
        """Solvability gate: True iff a single zero-budget sample picks a known skill.

        Sub-intents that map to no skill are flagged for clarification rather than
        silently dropped (AOP-style — no blind dispatch).
        """
        return await self._sample_select(sub_intent, 0) is not None

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

    async def plan_tool_call(self, task: str, skill_name: str) -> dict | None:
        """
        Load the skill body, then ask Gemma 4 to plan a specific tool call.

        ONE acompletion call asking for STRICT JSON {"tool":"...","arguments":{...}}.
        Strips code fences and json.loads the result.

        Args:
            task: Natural-language goal description
            skill_name: Skill to use

        Returns:
            {"tool": str, "arguments": dict} or None on error
        """
        # Load the skill body once (progressive disclosure).
        load_result = self._runner.load_skill(skill_name)
        response = load_result.get("response", {})
        if response.get("status") not in ("loaded", "already_loaded"):
            _log.error("failed to load skill: %s", response.get("message"))
            return None
        # On a repeat load, load_skill omits the body (progressive-disclosure dedup),
        # so fall back to the runner's activation cache. Without this, plan_tool_call
        # returns None for any skill already loaded by an earlier task → that subtask
        # falls into the slow ReAct loop. (The constrained-decoding/fast-path attempts
        # from this session were reverted as regressive; THIS cache read was the one
        # correct latency fix. See docs/e2e-setup-findings.)
        body = response.get("body") or self._runner.loaded.get(skill_name, "")
        if not body:
            _log.error("skill %s has empty body", skill_name)
            return None

        prompt = (
            f"You have loaded the skill: {skill_name}\n\n"
            f"Skill documentation:\n{body}\n\n"
            f"Task: {task}\n\n"
            f"Reply with ONLY a JSON object (no markdown, no code fences):\n"
            f'{{"tool":"<tool_name>","arguments":{{"<arg>":<value>}}}}'
        )
        # Retry across independent samples: the model occasionally returns prose or
        # malformed JSON; a few retries compound the chance of a valid tool plan.
        for attempt in range(PLAN_ATTEMPTS):
            try:
                r = await litellm.acompletion(
                    model="openai/gemma-4-31b",
                    api_base=self._gemma_base,
                    api_key="not-needed",
                    messages=[{"role": "user", "content": prompt}],
                    extra_body={"thinking_budget_tokens": 0},
                )
                choices = getattr(r, "choices", None)
                if not choices:
                    continue
                raw_text = choices[0].message.content
                if not raw_text:
                    continue
                raw_text = raw_text.strip()
                if raw_text.startswith("```json"):
                    raw_text = raw_text[7:]
                elif raw_text.startswith("```"):
                    raw_text = raw_text[3:]
                if raw_text.endswith("```"):
                    raw_text = raw_text[:-3]
                try:
                    parsed = json.loads(raw_text)
                except json.JSONDecodeError:
                    continue  # malformed — retry
                if not isinstance(parsed, dict):
                    continue
                tool = parsed.get("tool")
                if not tool:
                    continue
                _log.info("planned tool call: %s.%s (attempt %d)", skill_name, tool, attempt + 1)
                return {"tool": tool, "arguments": parsed.get("arguments", {})}
            except Exception as exc:
                _log.warning("plan_tool_call() attempt %d error: %s", attempt + 1, exc)
        return None

    async def execute(
        self, skill_name: str, tool: str, arguments: dict
    ) -> dict[str, Any]:
        """
        Dispatch to the skill worker via Redis Streams, then poll for result.

        XADD to "labmate:skill-tasks" with payload={task_id, skill, tool, arguments}.
        Polls GET "labmate:result:<task_id>" until present or timeout.

        Args:
            skill_name: Name of the skill
            tool: Tool to invoke within the skill
            arguments: Arguments to pass to the tool

        Returns:
            Result dict {"ok": bool, "result": ...} or {"ok": False, "error": "timeout"}
        """
        task_id = str(uuid.uuid4())
        key = f"{RESULT_PREFIX}{task_id}"

        try:
            # Push to Redis Streams
            payload = {
                "task_id": task_id,
                "skill": skill_name,
                "tool": tool,
                "arguments": arguments,
            }
            await self._redis.xadd(
                SKILL_TASKS_STREAM,
                {"payload": json.dumps(payload)},
            )
            _log.info("dispatched task %s: %s.%s", task_id, skill_name, tool)

            # Poll for result
            start = asyncio.get_event_loop().time()
            while True:
                result_json = await self._redis.get(key)
                if result_json is not None:
                    try:
                        result = json.loads(result_json)
                        _log.info("task %s result: %s", task_id, result.get("ok"))
                        return result
                    except json.JSONDecodeError:
                        return {
                            "ok": False,
                            "error": "malformed_result_json",
                        }

                elapsed = asyncio.get_event_loop().time() - start
                if elapsed > self._call_timeout:
                    _log.warning("task %s timed out after %.1f s", task_id, elapsed)
                    return {"ok": False, "error": "timeout"}

                await asyncio.sleep(0.5)

        except Exception as exc:
            _log.exception("execute() error: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def run(self, task: str) -> dict | None:
        """
        Full skill routing pipeline: select → plan → execute.

        Args:
            task: Natural-language goal description

        Returns:
            Result dict {"ok": bool, "result": ...} if successful, None if select/plan fail
        """
        try:
            skill_name = await self.select(task)
            if skill_name is None:
                _log.debug("no skill selected for task")
                return None

            plan = await self.plan_tool_call(task, skill_name)
            if plan is None:
                _log.debug("failed to plan tool call for %s", skill_name)
                return None

            tool_id = uuid.uuid4().hex[:12]
            await events.emit(
                "tool.start",
                tool_id=tool_id,
                name=skill_name,
                kind="skill",
                args=plan.get("arguments", {}),
                reasoning_why=self._last_reasoning,
            )
            started = time.monotonic()
            try:
                result = await self.execute(skill_name, plan["tool"], plan["arguments"])
            except Exception as exc:
                await events.emit(
                    "tool.done", tool_id=tool_id, status="error",
                    summary=str(exc)[:200], result=None,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            ok = bool(result.get("ok"))
            await events.emit(
                "tool.done",
                tool_id=tool_id,
                status="done" if ok else "error",
                summary=("ok" if ok else str(result.get("error", "failed")))[:200],
                result=result.get("result"),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return result

        except Exception as exc:
            _log.exception("run() error: %s", exc)
            return None
