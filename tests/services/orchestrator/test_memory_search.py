from __future__ import annotations

import pytest

from services.orchestrator.memory_search import MemorySearch


class FakeStore:
    """Minimal stand-in for StorageManager.search_memories — no Chroma/Mongo."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, int]] = []

    async def search_memories(self, query: str, top_k: int = 5) -> list[dict]:
        self.calls.append((query, top_k))
        return self.rows[:top_k]


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_returns_raw_ranked_snippets():
    store = FakeStore([
        {"id": "1", "fact": "[decision] Use Postgres for billing.", "raw_fact": "Use Postgres for billing.", "metadata": {}, "distance": 0.1},
        {"id": "2", "fact": "[lesson] Retry budget capped at 2.", "raw_fact": "Retry budget capped at 2.", "metadata": {}, "distance": 0.2},
    ])
    ms = MemorySearch(store)
    out = await ms.search("billing database", k=8)
    assert "Use Postgres for billing." in out
    assert "Retry budget capped at 2." in out
    # ranked order preserved (first row first)
    assert out.index("Postgres") < out.index("Retry budget")


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_passes_k_through_to_store():
    store = FakeStore([{"id": str(i), "fact": f"fact {i}", "raw_fact": f"fact {i}", "metadata": {}, "distance": 0.0} for i in range(20)])
    ms = MemorySearch(store, max_results=8)
    await ms.search("q", k=3)
    assert store.calls[-1] == ("q", 3)


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_defaults_k_to_max_results():
    store = FakeStore([])
    ms = MemorySearch(store, max_results=5)
    await ms.search("q")
    assert store.calls[-1] == ("q", 5)


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_clamps_k_to_twenty():
    store = FakeStore([])
    ms = MemorySearch(store)
    await ms.search("q", k=999)
    assert store.calls[-1][1] == 20


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_empty_results_return_sentinel():
    ms = MemorySearch(FakeStore([]))
    out = await ms.search("nothing here")
    assert out == "(no relevant memory found)"


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_blank_facts_are_filtered_then_sentinel():
    ms = MemorySearch(FakeStore([{"id": "1", "fact": "  ", "raw_fact": "", "metadata": {}, "distance": 0.0}]))
    out = await ms.search("q")
    assert out == "(no relevant memory found)"


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_total_output_capped():
    big = "x" * 5000
    store = FakeStore([{"id": str(i), "fact": big, "raw_fact": big, "metadata": {}, "distance": 0.0} for i in range(5)])
    ms = MemorySearch(store)
    out = await ms.search("q")
    assert len(out) <= 4000


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_per_snippet_capped():
    big = "y" * 5000
    store = FakeStore([{"id": "1", "fact": big, "raw_fact": big, "metadata": {}, "distance": 0.0}])
    ms = MemorySearch(store)
    out = await ms.search("q")
    # a single snippet body is capped at 600 chars (+ small index prefix)
    assert len(out) <= 700


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_store_error_returns_error_text_not_raise():
    class Boom:
        async def search_memories(self, query, top_k=5):
            raise RuntimeError("chroma down")

    ms = MemorySearch(Boom())
    out = await ms.search("q")
    assert "memory search failed" in out
    assert "chroma down" in out


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_none_store_returns_unavailable():
    ms = MemorySearch(None)
    out = await ms.search("q")
    assert out == "(memory store not available)"
