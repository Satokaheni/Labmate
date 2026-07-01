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
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import litellm
import redis.asyncio as aioredis

from services.orchestrator import events
from services.orchestrator.skill_telemetry import record_use_best_effort
from services.skill_runner.skill_runner import SkillRunner

_log = logging.getLogger("skill_router")

# Redis stream constants
SKILL_TASKS_STREAM = "labmate:skill-tasks"
RESULT_PREFIX = "labmate:result:"

# NOTE: routing is single-intent ONLY. A broadened A/B (3 batches + wall-clock,
# high confidence) concluded the multi-intent DECOMPOSE path added cost + flakiness
# with no quality benefit, so the decompose machinery and the ROUTING_MODE / routing_mode
# A/B toggle were removed. route() now treats the WHOLE message as ONE intent. The
# old eval tooling (call_counter.py, eval/ab_routing.*) may still mention a routing
# "mode" but it is no longer honored — single is the only mode.

# Retry budgets: the local Q4 model is non-deterministic, so a clearly-matching
# skill is occasionally missed on a single sample. Independent retries compound
# recall toward ~100% (precision is unaffected — we only accept catalog hits).
SELECT_ATTEMPTS = 3
PLAN_ATTEMPTS = 3
# Cap the CONTENT of the routing model calls. Their output is a tiny selection (a
# load_skill tool call or a `{"tool":...,"arguments":...}` JSON); without a cap the model
# can ramble prose out to thousands of tokens (and the JSON call RETRIES PLAN_ATTEMPTS
# times, compounding it) — a big share of pre-answer latency. 256 is ample for a selection.
ROUTE_MAX_TOKENS = int(os.getenv("ROUTE_MAX_TOKENS", "256"))

# Confidence threshold for accepting a routing decision. 2/3 (0.666...) == "at least
# 2 of 3 samples agreed". Below this (and not unanimous) the task has no confident skill
# match and route() falls through to the direct-answer path.
CONFIDENCE_THRESHOLD = 2.0 / 3.0


@dataclass
class RouteResult:
    """Outcome of single-intent routing.

    skills holds the single chosen skill ([skill]) when a skill confidently matched,
    or [] when no skill matched (direct-answer fall-through). sub_intents is always
    [task]. needs_clarification is retained for backward compatibility but route()
    always sets it False — the assess_ambiguity node owns clarification.
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
        call_timeout: float = float(os.getenv("SKILL_CALL_TIMEOUT", "135")),
        telemetry_path: Path | None = None,
    ) -> None:
        """
        Args:
            runner: SkillRunner instance with .discover() already called
            redis: redis.asyncio.Redis client for task dispatch and result polling
            gemma_api_base: base URL for Gemma 4 31B (e.g. http://localhost:8000/v1)
            call_timeout: max seconds to wait for a skill result. MUST exceed the
                skill-worker's CALL_TIMEOUT (default 120s) so the router receives the
                worker's own result/timeout instead of giving up first — a 60s router
                budget cut off heavy skills (test-gen, code-review, critique) mid-run.
                Override via SKILL_CALL_TIMEOUT.
            telemetry_path: optional path to skill telemetry store. Defaults to
                None, which resolves to default_store_path() inside record_use_best_effort.
        """
        self._runner = runner
        self._redis = redis
        self._gemma_base = gemma_api_base
        self._call_timeout = call_timeout
        self._telemetry_path = telemetry_path
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
                max_tokens=ROUTE_MAX_TOKENS,
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
        # Run the SELECT_ATTEMPTS budget-0 samples concurrently. They share an
        # identical ~3k-token catalog prompt, so sequential awaits re-prefill that
        # prompt N times back-to-back (~6-7s each on the Q4 host). Firing them
        # together overlaps the prefills across the model's parallel slots and lets
        # the prompt cache serve the shared prefix — same samples, same unanimity
        # logic, just without the serialized re-prefill.
        picks = await asyncio.gather(
            *(self._sample_select(task, 0) for _ in range(SELECT_ATTEMPTS))
        )
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
        _route_reasoning = events.clean_reasoning(self._last_reasoning)
        if _route_reasoning:
            await events.emit(
                "reasoning",
                node="route",
                summary=events.reasoning_summary(_route_reasoning),
                text=_route_reasoning,
            )
        return chosen

    async def route(self, task: str) -> RouteResult:
        """Single-intent routing: confidence-check the WHOLE task as ONE intent.

        Pipeline:
          1. sub_intents = [task]  (no decompose — single-intent is the only mode)
          2. _confidence_check(task)
          3. if a skill matched with confidence >= CONFIDENCE_THRESHOLD:
                 RouteResult(skills=[skill], sub_intents=[task])
             else (no confident skill):
                 RouteResult(skills=[], needs_clarification=False, sub_intents=[task])
                 — the direct-answer fall-through (plan node answers directly).

        route() NEVER clarifies: the dedicated assess_ambiguity node (which runs BEFORE
        route) is the sole owner of clarification for genuine ambiguity. A clear-but-
        skill-less task (e.g. "What is 2+2?") is unambiguous and PROCEEDS to direct answer.
        """
        sub_intents = [task]
        skill, confidence = await self._confidence_check(task)
        if skill is not None and confidence >= CONFIDENCE_THRESHOLD:
            _log.info("route() resolved task to skill: %s (confidence=%.2f)", skill, confidence)
            _route_reasoning = events.clean_reasoning(self._last_reasoning)
            if _route_reasoning:
                await events.emit(
                    "reasoning",
                    node="route",
                    summary=events.reasoning_summary(_route_reasoning),
                    text=_route_reasoning,
                )
            return RouteResult(skills=[skill], sub_intents=sub_intents)

        # No confident skill -> direct-answer fall-through (plan node answers directly).
        _log.info("route() found no confident skill -> direct answer")
        return RouteResult(
            skills=[],
            needs_clarification=False,
            sub_intents=sub_intents,
        )

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

        # FIX 10 (A1): run the SELECT_ATTEMPTS samples CONCURRENTLY (was a sequential
        # list comprehension). The voting logic below is identical — only the wall-clock
        # latency of gathering the samples changes (3 sequential ~6 s calls -> ~6 s total
        # given llama-server --parallel >= SELECT_ATTEMPTS).
        picks = await asyncio.gather(
            *(self._sample_select(sub_intent, 0) for _ in range(SELECT_ATTEMPTS))
        )
        hits = [p for p in picks if p is not None]
        if not hits:
            return None, 0.0
        winner, votes = Counter(hits).most_common(1)[0]
        confidence = votes / SELECT_ATTEMPTS
        return winner, confidence

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
                    max_tokens=ROUTE_MAX_TOKENS,
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
        self, skill_name: str, tool: str, arguments: dict, timeout: float | None = None
    ) -> dict[str, Any]:
        """
        Dispatch to the skill worker via Redis Streams, then poll for result.

        XADD to "labmate:skill-tasks" with payload={task_id, skill, tool, arguments}.
        Polls GET "labmate:result:<task_id>" until present or timeout.

        Args:
            skill_name: Name of the skill
            tool: Tool to invoke within the skill
            arguments: Arguments to pass to the tool
            timeout: Optional per-call poll budget (seconds). Defaults to the
                router's call_timeout. Heavy multi-call skills (e.g. the critique
                verify gate, which fans out CoVe questions) pass a larger value —
                it must stay under the skill-worker's CALL_TIMEOUT (default 120s)
                so the worker actually writes the result before we stop polling.

        Returns:
            Result dict {"ok": bool, "result": ...} or {"ok": False, "error": "timeout"}
        """
        call_timeout = timeout if timeout is not None else self._call_timeout
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
                if elapsed > call_timeout:
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
                    "tool.done",
                    tool_id=tool_id,
                    status="error",
                    summary=str(exc)[:200],
                    result=None,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            ok = bool(result.get("ok"))
            try:
                record_use_best_effort(skill_name, ok, path=self._telemetry_path)
            except Exception:  # pragma: no cover - telemetry must never break dispatch
                _log.warning("skill telemetry wire-in failed for %s", skill_name, exc_info=True)
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
