from __future__ import annotations

import json
import logging
import os

import litellm

logger = logging.getLogger(__name__)

CONSOLIDATION_INTERVAL = 50  # episodes between consolidation runs
GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")

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
    "atomic, durable facts worth remembering (preferences, decisions, entities, "
    "constraints). Return STRICT JSON: a list of objects with a single key "
    '"fact". Omit ephemeral chit-chat.\n\nEPISODES:\n{episodes}'
)

_SELF_EDIT_PROMPT = (
    "You reconcile NEW candidate memories against EXISTING memories. For each "
    "candidate decide: add (novel), update (refines/contradicts an existing one "
    "-> include its id), delete (an existing memory is now false -> include id), "
    "or noop (duplicate). Return STRICT JSON: "
    '{{"add": [{{"fact": str}}], "update": [{{"id": str, "fact": str}}], '
    '"delete": [{{"id": str}}]}}.\n\nNEW:\n{new}\n\nEXISTING:\n{existing}'
)


class MemoryConsolidator:
    def __init__(self, storage, lm_base_url: str | None = None, llm=None) -> None:
        self._s = storage
        self._base = lm_base_url or GEMMA_BASE
        self._llm = llm  # injectable async callable(messages) -> str; defaults to litellm
        self._episodic = EpisodicMemory()
        self._semantic = SemanticMemory()

    async def _complete(self, prompt: str) -> str:
        if self._llm is not None:
            return await self._llm(prompt)
        resp = await litellm.acompletion(
            model=f"openai/{GEMMA_MODEL}",
            messages=[{"role": "user", "content": prompt}],
            api_base=self._base,
            api_key="EMPTY",
            temperature=0.0,
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
        return [m for m in data if isinstance(m, dict) and m.get("fact")]

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

    async def _apply_edits(self, session_id: str, edits: dict) -> None:
        for m in edits.get("add", []):
            await self._semantic.upsert(self._s, {"session_id": session_id, "fact": m["fact"]})
        for m in edits.get("update", []):
            await self._semantic.supersede(self._s, m["id"], {"session_id": session_id, "fact": m["fact"]})
        for m in edits.get("delete", []):
            await self._s.close_memory(m["id"])

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
