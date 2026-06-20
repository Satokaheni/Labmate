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
import uuid
from typing import Any

import litellm
import redis.asyncio as aioredis

from services.skill_runner.skill_runner import SkillRunner

_log = logging.getLogger("skill_router")

# Redis stream constants
SKILL_TASKS_STREAM = "labmate:skill-tasks"
RESULT_PREFIX = "labmate:result:"

# Retry budgets: the local Q4 model is non-deterministic, so a clearly-matching
# skill is occasionally missed on a single sample. Independent retries compound
# recall toward ~100% (precision is unaffected — we only accept catalog hits).
SELECT_ATTEMPTS = 3
PLAN_ATTEMPTS = 3


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

    @property
    def runner(self) -> SkillRunner:
        """Public accessor for the runner (SkillRunner with catalog and tool schema)."""
        return self._runner

    async def select(self, task: str) -> str | None:
        """
        Ask Gemma 4 to select which skill to use for this task.

        ONE acompletion call with tools=[runner.tool_schema()], system=catalog_prompt().
        Returns the skill name if the model emits a load_skill tool call, else None.
        Parses tool_calls defensively; returns None on any error.

        Args:
            task: Natural-language goal description

        Returns:
            Skill name (str) if selected, None otherwise
        """
        catalog = self._runner.catalog_prompt()
        schema = self._runner.tool_schema()
        # Without an explicit directive, Gemma 4 under-triggers: it returns no
        # tool call for ~half of clearly-matching tasks. Instructing it to call
        # load_skill whenever a skill fits raised live selection recall from
        # 9/18 to 18/18 (precision was already 100%). See docs/e2e-setup-findings.
        directive = (
            "You are a skill router. If ANY available skill is relevant to the "
            "user's task, you MUST call load_skill with that skill's name. Only "
            "decline to call a tool if truly no skill fits."
        )
        # Retry across independent samples: the local Q4 model is non-deterministic
        # and occasionally emits no tool call for a clearly-matching task. Each
        # attempt is an independent sample, so a few retries compound recall toward
        # ~100% without ever picking a WRONG skill (we only accept catalog hits).
        for attempt in range(SELECT_ATTEMPTS):
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
                    extra_body={"thinking_budget_tokens": 0},
                )
                choices = getattr(r, "choices", None)
                if not choices:
                    continue
                message = getattr(choices[0], "message", None)
                tool_calls = getattr(message, "tool_calls", None) if message else None
                if not tool_calls:
                    continue  # no tool call this sample — retry
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
                        _log.info("selected skill: %s (attempt %d)", skill_name, attempt + 1)
                        return skill_name
            except Exception as exc:
                _log.warning("select() attempt %d error: %s", attempt + 1, exc)
        return None

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
        body = response.get("body", "")
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
            # Step 1: Select skill
            skill_name = await self.select(task)
            if skill_name is None:
                _log.debug("no skill selected for task")
                return None

            # Step 2: Plan tool call
            plan = await self.plan_tool_call(task, skill_name)
            if plan is None:
                _log.debug("failed to plan tool call for %s", skill_name)
                return None

            # Step 3: Execute
            result = await self.execute(
                skill_name,
                plan["tool"],
                plan["arguments"],
            )
            return result

        except Exception as exc:
            _log.exception("run() error: %s", exc)
            return None
