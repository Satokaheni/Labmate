from __future__ import annotations

import json
import logging
import os

import litellm

logger = logging.getLogger(__name__)

CONSOLIDATION_INTERVAL = 50  # episodes between consolidation runs
GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")

_VALID_SOURCES = {
    "user_stated",
    "tool_output",
    "agent_generated",
    "compaction_reflection",
}
_DEFAULT_SOURCE = "agent_generated"


def _normalize_source(value) -> str:
    """Coerce an LLM-supplied source string to the allowed taxonomy."""
    if isinstance(value, str) and value.strip() in _VALID_SOURCES:
        return value.strip()
    return _DEFAULT_SOURCE


# ---------------------------------------------------------------------------
# Lazy Gemma tokenizer singleton (rule #3: never tiktoken)
# ---------------------------------------------------------------------------

_TOKENIZER = None
EXTRACT_TOKEN_BUDGET = 3000


def _get_tokenizer():
    global _TOKENIZER
    if _TOKENIZER is None:
        from transformers import AutoTokenizer  # noqa: WPS433
        _TOKENIZER = AutoTokenizer.from_pretrained("google/gemma-4-9b-it")
    return _TOKENIZER


def token_count(text: str) -> int:
    return len(_get_tokenizer().encode(text))


# ---------------------------------------------------------------------------
# EpisodicMemory — sliding window
# ---------------------------------------------------------------------------

class EpisodicMemory:
    """Sliding-window view over raw episodes in MongoDB."""

    WINDOW_SIZE = 20

    async def add(self, storage, session_id: str, content: str, metadata: dict | None = None) -> None:
        await storage.store_episode(session_id, content, metadata or {})

    async def get_recent(self, storage, session_id: str) -> list[dict]:
        """Most recent WINDOW_SIZE episodes, oldest-first."""
        cursor = (
            storage._db["episodes"]
            .find({"session_id": session_id})
            .sort("seq", -1)
            .limit(self.WINDOW_SIZE)
        )
        docs = [d async for d in cursor]
        docs.reverse()
        return docs


# ---------------------------------------------------------------------------
# SemanticMemory — deduplicated fact store with temporal validity
# ---------------------------------------------------------------------------

class SemanticMemory:
    """Deduplicated semantic fact store with Zep-style temporal validity."""

    async def search(self, storage, query: str, top_k: int = 5) -> list[dict]:
        return await storage.search_memories(query, top_k=top_k)

    async def upsert(self, storage, memory: dict) -> str:
        """Open a new fact interval (valid_from now, valid_to None)."""
        return await storage.store_memory(memory)

    async def supersede(self, storage, old_id: str, new_memory: dict) -> str:
        """Close the old fact (Zep) and open a new one that supersedes it."""
        await storage.close_memory(old_id)
        new_memory = {**new_memory, "supersedes": old_id}
        return await storage.store_memory(new_memory)


# ---------------------------------------------------------------------------
# MemoryConsolidator — Mem0 extract + self-edit + apply
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT = (
    "You are a memory extractor. From the conversation episodes below, extract "
    "atomic, self-contained facts worth keeping long-term.\n"
    "Include: user preferences, decisions, entities (people/papers/tools), "
    "constraints, failures and lessons, AND agent confirmations/recommendations.\n"
    "Omit: greetings, progress updates, ephemeral chit-chat, and anything already "
    "implicit in stable context.\n"
    "For each fact, assign an importance (1=trivial, 3=standard preference, "
    "5=safety-critical or identity-level). Higher importance slows decay.\n"
    "For each fact, classify its SOURCE as exactly one of: "
    '"user_stated" (the user asserted it), '
    '"tool_output" (it came from a tool/search/file result), '
    '"agent_generated" (an agent inferred or recommended it).\n'
    'Return STRICT JSON: '
    '[{{"fact": str, "importance": int, "source": str}}].\n\n'
    "EPISODES:\n{episodes}"
)

_SELF_EDIT_PROMPT = (
    "You reconcile NEW candidate memories against EXISTING memories.\n"
    "CRITICAL disambiguation rule:\n"
    "  - UPDATE: candidate REFINES or EXTENDS an existing fact (e.g., adding a "
    "second dog is UPDATE of the 'user has a dog' fact, not DELETE + ADD).\n"
    "  - DELETE: candidate CONTRADICTS and fully replaces an existing fact "
    "(e.g., user changed employer — old fact is now false).\n"
    "  - ADD: candidate is wholly novel (no semantically-equivalent existing fact).\n"
    "  - NOOP: candidate is a duplicate already covered by an existing fact.\n"
    "When in doubt between UPDATE and DELETE, prefer UPDATE.\n"
    "For each item in add/update, include source field:\n"
    "  - If merging/synthesizing multiple sources: use 'agent_generated'.\n"
    "  - Otherwise: echo the source from the input candidates unchanged.\n"
    "Return STRICT JSON: "
    '{{"add": [{{"fact": str, "importance": int, "source": str}}], '
    '"update": [{{"id": str, "fact": str, "importance": int, "source": str}}], '
    '"delete": [{{"id": str}}], "noop": [{{"id": str}}]}}.\n\n'
    "NEW:\n{new}\n\nEXISTING:\n{existing}"
)

# Written to episodic memory at task-boundary (success or failure)
_TASK_REFLECTION_PROMPT = (
    "Write a concise, self-contained reflection on the following completed task.\n"
    "If it succeeded, note what approach worked and any reusable insight.\n"
    "If it failed, name the root cause and what to try differently next time.\n"
    "Be specific — avoid generic observations. 2-4 sentences max.\n\n"
    "TASK GOAL: {goal}\n"
    "OUTCOME: {outcome}\n"
    "SUMMARY: {summary}\n\n"
    "REFLECTION:"
)

_CRITIC_PROMPT = (
    "You are a strict memory critic. Decide whether the CANDIDATE memory should be "
    "committed, given the SOURCE episodes it was derived from.\n"
    "Reject (INVALID) if the candidate: contradicts the source, introduces facts not "
    "supported by the source (hallucination), or uses the wrong operation for its "
    "content.\n"
    "Accept (VALID) if it is faithful to the source and self-contained.\n"
    "OPERATION: {op}\n"
    "CANDIDATE: {fact}\n"
    "SOURCE EPISODES:\n{episodes}\n\n"
    'Return STRICT JSON: {{"verdict": "VALID"|"INVALID", "reason": str}}.'
)


class MemoryConsolidator:
    def __init__(self, storage, lm_base_url: str | None = None, llm=None, critic_enabled: bool = False) -> None:
        self._s = storage
        self._base = lm_base_url or GEMMA_BASE
        self._llm = llm  # injectable async callable(messages) -> str; defaults to litellm
        self._episodic = EpisodicMemory()
        self._semantic = SemanticMemory()
        self._critic_enabled = critic_enabled

    async def _complete(self, prompt: str) -> str:
        if self._llm is not None:
            return await self._llm(prompt)
        resp = await litellm.acompletion(
            model=f"openai/{GEMMA_MODEL}",
            messages=[{"role": "user", "content": prompt}],
            api_base=self._base,
            api_key="not-needed",
            temperature=0.0,
            extra_body={"thinking_budget_tokens": 1024},  # FIX #6: avoid INT_MAX hang
        )
        return resp["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_json(text: str):
        start = text.find("{")
        arr = text.find("[")
        if arr != -1 and (start == -1 or arr < start):
            start = arr
        end = max(text.rfind("}"), text.rfind("]"))
        return json.loads(text[start:end + 1])

    async def _extract_memories(self, episodes: list[dict]) -> list[dict]:
        kept: list[str] = []
        total = 0
        for e in reversed(episodes):  # newest first, keep most recent under budget
            line = f"- {e.get('content', '')}"
            t = token_count(line)
            if total + t > EXTRACT_TOKEN_BUDGET:
                break
            kept.append(line)
            total += t
        joined = "\n".join(reversed(kept))
        raw = await self._complete(_EXTRACT_PROMPT.format(episodes=joined))
        try:
            data = self._parse_json(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("extract: non-JSON response, skipping batch")
            return []
        result = []
        for m in data:
            if isinstance(m, dict) and m.get("fact"):
                try:
                    imp = max(1, min(5, int(m.get("importance", 3))))
                except (TypeError, ValueError):
                    imp = 3
                result.append({
                    "fact": m["fact"],
                    "importance": imp,
                    "source": _normalize_source(m.get("source")),
                })
        return result

    async def _self_edit(self, new_memories: list[dict], existing: list[dict]) -> dict:
        raw = await self._complete(_SELF_EDIT_PROMPT.format(
            new=json.dumps(new_memories),
            existing=json.dumps([{"id": e["id"], "fact": e["fact"]} for e in existing]),
        ))
        try:
            data = self._parse_json(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("self_edit: non-JSON response, treating all as add")
            data = {"add": new_memories, "update": [], "delete": []}
        return {
            "add": data.get("add", []),
            "update": data.get("update", []),
            "delete": data.get("delete", []),
        }

    async def _critique(self, op: str, fact: str, episodes_text: str) -> bool:
        """Return True if the candidate is VALID. Fail-open on parse/LLM error.

        Fail-open (treat as VALID on error) is deliberate: a flaky critic must not
        silently drop legitimate memories. Genuine rejections are explicit INVALID.
        """
        try:
            raw = await self._complete(_CRITIC_PROMPT.format(
                op=op, fact=fact, episodes=episodes_text,
            ))
            data = self._parse_json(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("critic: non-JSON response, accepting candidate")
            return True
        verdict = str(data.get("verdict", "VALID")).strip().upper()
        if verdict == "INVALID":
            logger.info("critic rejected (%s): %s — %s",
                        op, fact[:60], data.get("reason", ""))
            return False
        return True

    async def _filter_edits(self, edits: dict, episodes_text: str) -> dict:
        """Drop ADD/UPDATE candidates the critic marks INVALID. DELETE/NOOP pass through."""
        if not self._critic_enabled:
            return edits
        kept_add = []
        for m in edits.get("add", []):
            if not m.get("fact"):
                kept_add.append(m)  # pass through (no fact to critique)
                continue
            if await self._critique("ADD", m.get("fact", ""), episodes_text):
                kept_add.append(m)
        kept_update = []
        for m in edits.get("update", []):
            if not m.get("fact"):
                kept_update.append(m)
                continue
            if await self._critique("UPDATE", m.get("fact", ""), episodes_text):
                kept_update.append(m)
        return {
            "add": kept_add,
            "update": kept_update,
            "delete": edits.get("delete", []),
        }

    def _memory_dict(self, session_id: str, m: dict, source: str | None = None) -> dict:
        """Build the memory document stored in Chroma/Mongo."""
        return {
            "session_id": session_id,
            "fact": m["fact"],
            "importance": m.get("importance", 3),
            "embedding_text": m["fact"],
            "source": _normalize_source(source if source is not None else m.get("source")),
        }

    async def _apply_edits(self, session_id: str, edits: dict) -> None:
        for m in edits.get("add", []):
            await self._semantic.upsert(self._s, self._memory_dict(session_id, m))
        for m in edits.get("update", []):
            await self._semantic.supersede(
                self._s, m["id"], self._memory_dict(session_id, m)
            )
        for m in edits.get("delete", []):
            await self._s.close_memory(m["id"])

    async def write_reflections(self, session_id: str, reflections: list[str]) -> None:
        """Write a list of insight strings (from compaction) to semantic memory.

        Each string is written as an ADD with importance=3 (standard) unless
        it looks like a lesson/failure insight (importance=4). This is the
        bridge from compaction-time reflection extraction to persistent memory.
        Debounced best-effort: individual write failures are logged, not raised.
        """
        lesson_signals = ("fail", "error", "lesson", "broke", "wrong", "bug", "avoid")
        for insight in reflections:
            try:
                importance = 4 if any(s in insight.lower() for s in lesson_signals) else 3
                await self._semantic.upsert(self._s, {
                    "session_id": session_id,
                    "fact": insight,
                    "importance": importance,
                    "embedding_text": insight,
                    "source": "compaction_reflection",
                })
            except Exception:
                logger.warning("write_reflections: failed to write '%s...'", insight[:60])

    async def on_task_complete(
        self,
        session_id: str,
        goal: str,
        success: bool,
        summary: str,
    ) -> None:
        """Write a Reflexion-style episode at task boundary (success or failure).

        This is the highest-signal memory write trigger: the task boundary is when
        we know for certain whether an approach worked. Failures get importance=4
        (lessons); successes get importance=3. Runs async in the background so it
        never adds latency to the task response.
        """
        outcome = "SUCCESS" if success else "FAILURE"
        try:
            reflection = await self._complete(
                _TASK_REFLECTION_PROMPT.format(
                    goal=goal[:500],
                    outcome=outcome,
                    summary=summary[:1_000],
                )
            )
            reflection = reflection.strip()
            if not reflection:
                return
            importance = 4 if not success else 3
            await self._semantic.upsert(self._s, {
                "session_id": session_id,
                "fact": reflection,
                "importance": importance,
                "embedding_text": reflection,
                "source": "task_reflection",
                "outcome": outcome,
            })
            logger.info(
                "on_task_complete: wrote %s reflection for session=%s",
                outcome, session_id,
            )
        except Exception:
            logger.warning("on_task_complete: reflection write failed (non-fatal)")

    async def maybe_consolidate(self, session_id: str) -> bool:
        """Run consolidation only every CONSOLIDATION_INTERVAL episodes.

        Returns True if a consolidation actually ran.
        """
        count = await self._s._db["episodes"].count_documents({"session_id": session_id})
        if count == 0 or count % CONSOLIDATION_INTERVAL != 0:
            return False
        episodes = await self._episodic.get_recent(self._s, session_id)
        candidates = await self._extract_memories(episodes)
        if not candidates:
            return False
        existing = await self._semantic.search(self._s, candidates[0]["fact"], top_k=10)
        edits = await self._self_edit(candidates, existing)
        _ep_lines = [f"- {e['content']}" for e in episodes if e.get('content')]
        episodes_text = ""
        for _line in reversed(_ep_lines):  # most-recent episodes first
            if len(episodes_text) + len(_line) + 1 > 4_000:
                break
            episodes_text = _line + "\n" + episodes_text
        episodes_text = episodes_text.rstrip()
        edits = await self._filter_edits(edits, episodes_text)
        await self._apply_edits(session_id, edits)
        logger.info("consolidated session=%s add=%d update=%d delete=%d",
                    session_id, len(edits["add"]), len(edits["update"]), len(edits["delete"]))
        return True

    # --- Task 9: Redis Streams consumer ---------------------------------

    async def consume_tasks(self, group: str = "consolidators", consumer: str = "c1") -> None:
        """XREADGROUP loop (rule #5). For each episode_vector task, maybe_consolidate."""
        r = self._s._redis
        try:
            await r.xgroup_create("tasks", group, id="0", mkstream=True)
        except Exception:  # group already exists
            pass
        while True:
            resp = await r.xreadgroup(group, consumer, {"tasks": ">"}, count=10, block=5000)
            if not resp:
                continue
            for _stream, entries in resp:
                for msg_id, fields in entries:
                    payload = json.loads(
                        fields[b"payload"] if b"payload" in fields else fields["payload"]
                    )
                    if payload.get("kind") == "episode_vector":
                        sid = payload.get("session_id")
                        if sid:
                            await self.maybe_consolidate(sid)
                    await r.xack("tasks", group, msg_id)
