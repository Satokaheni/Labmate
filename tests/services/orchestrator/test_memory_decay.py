from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.mocked]


def _now():
    return datetime(2026, 6, 25, tzinfo=timezone.utc)


def test_expires_at_table():
    """Test TTL table mapping (synchronous test, not async)."""
    from services.orchestrator.storage_manager import _expires_at

    n = _now()
    assert _expires_at(1, n) == n + timedelta(days=30)
    assert _expires_at(2, n) == n + timedelta(days=90)
    assert _expires_at(3, n) == n + timedelta(days=365)
    assert _expires_at(4, n) == n + timedelta(days=1095)
    assert _expires_at(5, n) is None      # never expires
    assert _expires_at(7, n) is None      # clamps high to never
    assert _expires_at("bad", n) == n + timedelta(days=365)  # default importance 3
    assert _expires_at(0, n) == n + timedelta(days=30)   # clamps low to 30 days
    assert _expires_at(-1, n) == n + timedelta(days=30)  # negative also clamps


@pytest.mark.asyncio
async def test_store_memory_sets_expires_at(storage, mock_mongo):
    await storage.store_memory({"session_id": "s1", "fact": "f", "importance": 1})
    doc = mock_mongo._collections["memories"].insert_one.await_args.args[0]
    assert doc["expires_at"] is not None
    # importance 5 -> never expires
    await storage.store_memory({"session_id": "s1", "fact": "g", "importance": 5})
    doc5 = mock_mongo._collections["memories"].insert_one.await_args.args[0]
    assert doc5["expires_at"] is None


@pytest.mark.asyncio
async def test_decay_closes_past_due(storage, mock_mongo):
    from bson import ObjectId

    expired = [{"_id": ObjectId()}, {"_id": ObjectId()}]

    class _Cur:
        def __init__(self, docs): self._docs = docs
        def __aiter__(self):
            async def g():
                for d in self._docs:
                    yield d
            return g()

    mem = mock_mongo._collections.setdefault("memories", AsyncMock())
    mem.find = lambda q, proj=None: _Cur(expired)
    storage.close_memory = AsyncMock()

    n = _now()
    closed = await storage.decay_expired_memories("s1", now=n)
    assert closed == 2
    assert storage.close_memory.await_count == 2
    # close called with the cutoff timestamp
    assert storage.close_memory.await_args.kwargs["valid_to"] == n


@pytest.mark.asyncio
async def test_decay_query_excludes_never_and_closed(storage, mock_mongo):
    captured = {}

    class _Cur:
        def __aiter__(self):
            async def g():
                if False:
                    yield None
            return g()

    def _find(q, proj=None):
        captured["q"] = q
        return _Cur()

    mem = mock_mongo._collections.setdefault("memories", AsyncMock())
    mem.find = _find
    await storage.decay_expired_memories("s1", now=_now())
    q = captured["q"]
    assert q["valid_to"] is None
    assert q["expires_at"]["$ne"] is None  # never-expiring excluded
