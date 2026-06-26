from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


async def test_memory_dict_normalizes_source(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())
    # explicit valid source is preserved
    d = mc._memory_dict("s1", {"fact": "x", "importance": 2}, source="user_stated")
    assert d["source"] == "user_stated"
    # invalid source falls back to default
    d2 = mc._memory_dict("s1", {"fact": "y", "source": "garbage"})
    assert d2["source"] == "agent_generated"


async def test_extract_carries_source(storage, monkeypatch):
    from services.orchestrator.memory_consolidator import MemoryConsolidator
    from unittest.mock import patch

    # Mock token_count to avoid loading real tokenizer
    with patch("services.orchestrator.memory_consolidator.token_count", return_value=10):
        fake_llm = AsyncMock(
            return_value='[{"fact":"user likes dark mode","importance":3,"source":"user_stated"}]'
        )
        mc = MemoryConsolidator(storage, llm=fake_llm)
        out = await mc._extract_memories([{"content": "I prefer dark mode"}])
        assert out == [{"fact": "user likes dark mode", "importance": 3, "source": "user_stated"}]


async def test_store_memory_persists_source_and_importance(storage, mock_mongo):
    await storage.store_memory({
        "session_id": "s1",
        "fact": "f",
        "importance": 5,
        "source": "tool_output",
    })
    doc = mock_mongo._collections["memories"].insert_one.await_args.args[0]
    assert doc["source"] == "tool_output"
    assert doc["importance"] == 5


async def test_apply_edits_passes_source_to_store(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())
    await mc._apply_edits("s1", {
        "add": [{"fact": "a", "importance": 3, "source": "user_stated"}],
        "update": [],
        "delete": [],
    })
    doc = mock_mongo._collections["memories"].insert_one.await_args.args[0]
    assert doc["source"] == "user_stated"


async def test_outbox_projects_source_and_filters_none(storage, mock_mongo, mock_chroma):
    from services.orchestrator.outbox_worker import OutboxWorker
    from bson import ObjectId

    oid = ObjectId()
    mem_doc = {
        "_id": oid, "session_id": "s1", "fact": "f", "source": "tool_output",
        "importance": 4, "valid_to": None,
        "outbox": {"kind": "memory_vector", "processed": False},
    }

    class _Cur:
        def __init__(self, docs): self._docs = docs
        def limit(self, n): return self
        def __aiter__(self):
            async def g():
                for d in self._docs:
                    yield d
            return g()

    # Ensure collections are initialized
    mem_col = mock_mongo["labmate"]["memories"]
    ep_col = mock_mongo["labmate"]["episodes"]
    mem_col.find = lambda q: _Cur([mem_doc])
    ep_col.find = lambda q: _Cur([])

    worker = OutboxWorker(storage)
    await worker.process_once()

    meta = mock_chroma._collection.upsert.await_args.kwargs["metadatas"][0]
    assert meta["source"] == "tool_output"
    assert meta["importance"] == 4
    assert "valid_to" not in meta  # None filtered out


async def test_search_memories_tags_fact_with_source(storage, mock_chroma):
    mock_chroma._collection.query.return_value = {
        "ids": [["m1"]],
        "documents": [["user prefers dark mode"]],
        "metadatas": [[{"session_id": "s1", "source": "user_stated"}]],
        "distances": [[0.1]],
    }
    out = await storage.search_memories("preferences", top_k=1)
    assert out[0]["fact"] == "[user_stated] user prefers dark mode"
    assert out[0]["raw_fact"] == "user prefers dark mode"


async def test_search_memories_no_source_no_tag(storage, mock_chroma):
    mock_chroma._collection.query.return_value = {
        "ids": [["m1"]],
        "documents": [["a fact"]],
        "metadatas": [[{"session_id": "s1"}]],  # no source
        "distances": [[0.1]],
    }
    out = await storage.search_memories("q", top_k=1)
    assert out[0]["fact"] == "a fact"
    assert out[0]["raw_fact"] == "a fact"


async def test_normalize_source_empty_string():
    from services.orchestrator.memory_consolidator import _normalize_source
    assert _normalize_source("") == "agent_generated"


async def test_normalize_source_whitespace():
    from services.orchestrator.memory_consolidator import _normalize_source
    assert _normalize_source("  user_stated  ") == "user_stated"


async def test_normalize_source_none():
    from services.orchestrator.memory_consolidator import _normalize_source
    assert _normalize_source(None) == "agent_generated"


async def test_normalize_source_non_string():
    from services.orchestrator.memory_consolidator import _normalize_source
    assert _normalize_source(123) == "agent_generated"


async def test_self_edit_carries_source_through_flow(storage, monkeypatch):
    """Test that source is preserved from extract → self_edit → apply."""
    from services.orchestrator.memory_consolidator import MemoryConsolidator
    from unittest.mock import patch, AsyncMock

    # Mock token_count to avoid loading real tokenizer
    with patch("services.orchestrator.memory_consolidator.token_count", return_value=10):
        fake_llm = AsyncMock()
        mc = MemoryConsolidator(storage, llm=fake_llm)

        # Simulate the full flow:
        # 1. _extract_memories returns candidates with source
        # 2. _self_edit receives those candidates and returns them with source
        # 3. _apply_edits passes them to _memory_dict which normalizes source

        # Mock the storage methods
        storage.store_memory = AsyncMock(return_value="memory_id_1")
        storage.close_memory = AsyncMock()

        edits = {
            "add": [{"fact": "f1", "importance": 3, "source": "user_stated"}],
            "update": [],
            "delete": [],
        }

        # Apply the edits
        await mc._apply_edits("s1", edits)

        # Verify that store_memory was called with the source
        assert storage.store_memory.call_count == 1
        call_args = storage.store_memory.call_args
        memory_doc = call_args.args[0]
        assert memory_doc["source"] == "user_stated"
