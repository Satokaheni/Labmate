from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


async def test_gather_neighbors_dedupes_by_id(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())

    async def fake_search(_storage, query, top_k=5):
        # both candidates return overlapping ids
        if "dog" in query:
            return [{"id": "m1", "fact": "has a dog"}, {"id": "m2", "fact": "likes pets"}]
        return [{"id": "m2", "fact": "likes pets"}, {"id": "m3", "fact": "has a cat"}]

    mc._semantic.search = fake_search
    neighbors = await mc._gather_neighbors(
        [{"fact": "user got a second dog"}, {"fact": "user adopted a cat"}], per_fact=3
    )
    ids = sorted(n["id"] for n in neighbors)
    assert ids == ["m1", "m2", "m3"]  # m2 deduped


async def test_gather_neighbors_skips_empty_facts(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())
    calls = []

    async def fake_search(_storage, query, top_k=5):
        calls.append(query)
        return []

    mc._semantic.search = fake_search
    out = await mc._gather_neighbors([{"fact": ""}, {"fact": "real"}], per_fact=3)
    assert out == []
    assert calls == ["real"]  # empty-fact candidate skipped


async def test_gather_neighbors_uses_per_fact_top_k(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())
    seen_top_k = []

    async def fake_search(_storage, query, top_k=5):
        seen_top_k.append(top_k)
        return []

    mc._semantic.search = fake_search
    await mc._gather_neighbors([{"fact": "a"}], per_fact=3)
    assert seen_top_k == [3]


async def test_maybe_consolidate_uses_neighbors(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import (
        MemoryConsolidator, CONSOLIDATION_INTERVAL,
    )

    mc = MemoryConsolidator(storage, llm=AsyncMock())
    _ = storage._db["episodes"]
    mock_mongo._collections["episodes"].count_documents = AsyncMock(
        return_value=CONSOLIDATION_INTERVAL
    )

    class _Cur:
        def __init__(self, docs): self._docs = docs
        def sort(self, *_): return self
        def limit(self, n): return self
        def __aiter__(self):
            async def g():
                for d in self._docs:
                    yield d
            return g()

    mock_mongo._collections["episodes"].find = lambda q: _Cur([{"content": "ep"}])

    mc._extract_memories = AsyncMock(return_value=[{"fact": "f", "importance": 3}])
    mc._gather_neighbors = AsyncMock(return_value=[{"id": "n1", "fact": "old"}])
    mc._self_edit = AsyncMock(return_value={"add": [], "update": [], "delete": []})
    mc._apply_edits = AsyncMock()

    ran = await mc.maybe_consolidate("s1")
    assert ran is True
    mc._gather_neighbors.assert_awaited_once()
    # _self_edit received the neighbor set as the existing-memories arg
    assert mc._self_edit.await_args.args[1] == [{"id": "n1", "fact": "old"}]


async def test_maybe_consolidate_retains_filter_edits_call(storage, mock_mongo):
    from services.orchestrator.memory_consolidator import (
        MemoryConsolidator, CONSOLIDATION_INTERVAL,
    )

    mc = MemoryConsolidator(storage, llm=AsyncMock())
    _ = storage._db["episodes"]
    mock_mongo._collections["episodes"].count_documents = AsyncMock(
        return_value=CONSOLIDATION_INTERVAL
    )

    class _Cur:
        def __init__(self, docs): self._docs = docs
        def sort(self, *_): return self
        def limit(self, n): return self
        def __aiter__(self):
            async def g():
                for d in self._docs:
                    yield d
            return g()

    mock_mongo._collections["episodes"].find = lambda q: _Cur([{"content": "ep"}])

    mc._extract_memories = AsyncMock(return_value=[{"fact": "f", "importance": 3}])
    mc._gather_neighbors = AsyncMock(return_value=[])
    mc._self_edit = AsyncMock(return_value={"add": [], "update": [], "delete": []})
    mc._filter_edits = AsyncMock(return_value={"add": [], "update": [], "delete": []})
    mc._apply_edits = AsyncMock()

    await mc.maybe_consolidate("s1")
    mc._filter_edits.assert_awaited_once()
