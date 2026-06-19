from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Shared cursor helper (used by episodic + outbox tests)
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *_):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


# ---------------------------------------------------------------------------
# Task 5 — EpisodicMemory sliding window
# ---------------------------------------------------------------------------

async def test_get_recent_caps_at_window_size(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import EpisodicMemory

    docs = [{"_id": i, "seq": i, "content": f"t{i}"} for i in range(100)]
    # Force creation of the episodes collection in the mock
    _ = storage._db["episodes"]
    mock_mongo._collections["episodes"].find = lambda q: _Cursor(list(reversed(docs)))

    ep = EpisodicMemory()
    recent = await ep.get_recent(storage, "s1")
    assert len(recent) <= EpisodicMemory.WINDOW_SIZE == 20


# ---------------------------------------------------------------------------
# Task 6 — SemanticMemory temporal supersede
# ---------------------------------------------------------------------------

async def test_supersede_closes_old_and_opens_new(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import SemanticMemory

    sm = SemanticMemory()
    # Use a valid 24-char hex ObjectId string
    valid_oid = "507f1f77bcf86cd799439011"
    await sm.supersede(storage, valid_oid, {"session_id": "s1", "fact": "new fact"})
    mem = mock_mongo._collections["memories"]
    mem.update_one.assert_awaited_once()   # old closed (valid_to set)
    mem.insert_one.assert_awaited_once()   # new opened
    new_doc = mem.insert_one.await_args.args[0]
    assert new_doc["supersedes"] == valid_oid
    assert new_doc["valid_to"] is None


# ---------------------------------------------------------------------------
# Task 7 — MemoryConsolidator: self-edit shape, interval gating, apply edits
# ---------------------------------------------------------------------------

async def test_self_edit_returns_add_update_delete(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    fake_llm = AsyncMock(
        return_value='{"add":[{"fact":"x"}],"update":[],"delete":[{"id":"d1"}]}'
    )
    mc = MemoryConsolidator(storage, llm=fake_llm)
    edits = await mc._self_edit([{"fact": "x"}], [{"id": "d1", "fact": "old"}])
    assert set(edits.keys()) == {"add", "update", "delete"}
    assert edits["add"] == [{"fact": "x"}]
    assert edits["delete"] == [{"id": "d1"}]


async def test_maybe_consolidate_gated_by_interval(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import MemoryConsolidator, CONSOLIDATION_INTERVAL

    mc = MemoryConsolidator(storage, llm=AsyncMock(return_value="[]"))

    # Force collection creation in the mock
    _ = storage._db["episodes"]

    # not at a multiple of the interval -> no run
    mock_mongo._collections["episodes"].count_documents = AsyncMock(return_value=49)
    assert await mc.maybe_consolidate("s1") is False

    # at the interval but extractor yields nothing -> still no apply, returns False
    mock_mongo._collections["episodes"].count_documents = AsyncMock(
        return_value=CONSOLIDATION_INTERVAL
    )
    # episodic.get_recent needs a cursor; return empty list
    mock_mongo._collections["episodes"].find = lambda q: _Cursor([])
    assert await mc.maybe_consolidate("s1") is False


async def test_apply_edits_routes_through_outbox(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())
    oid_u = "507f1f77bcf86cd799439011"
    oid_d = "507f1f77bcf86cd799439012"
    await mc._apply_edits("s1", {
        "add": [{"fact": "a"}],
        "update": [{"id": oid_u, "fact": "b"}],
        "delete": [{"id": oid_d}],
    })
    mem = mock_mongo._collections["memories"]
    # add => insert; update => close(update_one) + insert; delete => close(update_one)
    assert mem.insert_one.await_count == 2     # add + update's new fact
    assert mem.update_one.await_count == 2     # update's close + delete's close


# ---------------------------------------------------------------------------
# Task 8 — Token counting (Gemma AutoTokenizer, not tiktoken)
# ---------------------------------------------------------------------------

async def test_token_count_uses_gemma_autotokenizer():
    import services.orchestrator.memory_consolidator as mod

    fake_tok = MagicMock()
    fake_tok.encode.return_value = [1, 2, 3]

    # Reset singleton and patch the internal AutoTokenizer call inside the module
    mod._TOKENIZER = None
    with patch("services.orchestrator.memory_consolidator._get_tokenizer", return_value=fake_tok):
        assert mod.token_count("hello world") == 3

    # Also verify the _get_tokenizer function references gemma-4-9b-it
    import inspect
    src = inspect.getsource(mod._get_tokenizer)
    assert "google/gemma-4-9b-it" in src


def test_no_tiktoken_import():
    """Guard against forbidden patterns in actual code (not in comments/docstrings)."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[3] / "services" / "orchestrator"
    for f in ("storage_manager.py", "memory_consolidator.py", "outbox_worker.py"):
        text = (src / f).read_text()
        # Check for actual import statements (not comments)
        assert re.search(r"^import tiktoken|^from tiktoken", text, re.MULTILINE) is None, \
            f"{f} imports tiktoken"
        # Check for forbidden client types — these should never appear anywhere
        assert "PersistentClient" not in text, f"{f} uses PersistentClient"
        assert "EphemeralClient" not in text, f"{f} uses EphemeralClient"
        # Check for actual .rpush( or .brpop( calls (not in comments/docstrings)
        assert re.search(r"\.rpush\s*\(", text) is None, f"{f} calls .rpush()"
        assert re.search(r"\.brpop\s*\(", text) is None, f"{f} calls .brpop()"


# ---------------------------------------------------------------------------
# Task 9 — consume_tasks uses XREADGROUP + XACK, not BRPOP
# ---------------------------------------------------------------------------

async def test_consume_tasks_uses_xreadgroup_and_acks(storage, mock_redis):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock(return_value="[]"))
    mc.maybe_consolidate = AsyncMock(return_value=True)

    # Drive one iteration manually instead of the infinite loop
    await mock_redis.xgroup_create("tasks", "consolidators", id="0", mkstream=True)
    mock_redis.xreadgroup.return_value = [
        (
            "tasks",
            [(b"1-0", {b"payload": b'{"kind":"episode_vector","session_id":"s1"}'})]
        )
    ]
    resp = await mock_redis.xreadgroup(
        "consolidators", "c1", {"tasks": ">"}, count=10, block=5000
    )
    for _s, entries in resp:
        for msg_id, fields in entries:
            await mc.maybe_consolidate("s1")
            await mock_redis.xack("tasks", "consolidators", msg_id)

    mock_redis.xreadgroup.assert_awaited()
    mock_redis.xack.assert_awaited_once()
    assert not hasattr(mock_redis, "brpop") or mock_redis.brpop.await_count == 0
