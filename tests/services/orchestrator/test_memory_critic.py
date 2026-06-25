from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


async def test_critic_disabled_by_default_passes_all(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock())  # critic off
    edits = {"add": [{"fact": "a"}], "update": [{"id": "u", "fact": "b"}], "delete": []}
    out = await mc._filter_edits(edits, "episodes")
    assert out == edits  # untouched


async def test_critic_drops_invalid_add(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    fake_llm = AsyncMock(return_value='{"verdict":"INVALID","reason":"hallucinated"}')
    mc = MemoryConsolidator(storage, llm=fake_llm, critic_enabled=True)
    edits = {"add": [{"fact": "made up fact"}], "update": [], "delete": [{"id": "d"}]}
    out = await mc._filter_edits(edits, "source episodes")
    assert out["add"] == []           # rejected
    assert out["delete"] == [{"id": "d"}]  # delete passes through unchecked


async def test_critic_keeps_valid(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    fake_llm = AsyncMock(return_value='{"verdict":"VALID","reason":"ok"}')
    mc = MemoryConsolidator(storage, llm=fake_llm, critic_enabled=True)
    edits = {"add": [{"fact": "true fact"}], "update": [], "delete": []}
    out = await mc._filter_edits(edits, "source episodes")
    assert out["add"] == [{"fact": "true fact"}]


async def test_critic_fails_open_on_bad_json(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    mc = MemoryConsolidator(storage, llm=AsyncMock(return_value="not json"), critic_enabled=True)
    assert await mc._critique("ADD", "fact", "episodes") is True


async def test_critique_returns_false_on_invalid(storage):
    from services.orchestrator.memory_consolidator import MemoryConsolidator

    fake_llm = AsyncMock(return_value='{"verdict":"INVALID","reason":"contradicts"}')
    mc = MemoryConsolidator(storage, llm=fake_llm, critic_enabled=True)
    assert await mc._critique("UPDATE", "fact", "episodes") is False
