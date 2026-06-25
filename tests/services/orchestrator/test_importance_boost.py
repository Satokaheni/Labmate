from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]

_OID = "507f1f77bcf86cd799439011"


async def test_boost_increments_and_caps(storage):
    mem = storage._db["memories"]
    mem.find_one = AsyncMock(return_value={"importance": 4.95})
    await storage.boost_memory_importance(_OID, delta=0.1)
    update = mem.update_one.await_args.args[1]["$set"]
    assert update["importance"] == 5.0  # capped


async def test_boost_reopens_outbox(storage):
    mem = storage._db["memories"]
    mem.find_one = AsyncMock(return_value={"importance": 3})
    await storage.boost_memory_importance(_OID)
    update = mem.update_one.await_args.args[1]["$set"]
    assert update["importance"] == 3.1
    assert update["outbox.processed"] is False


async def test_boost_ignores_bad_id(storage):
    mem = storage._db["memories"]
    mem.update_one.reset_mock()
    await storage.boost_memory_importance("not-an-objectid")
    mem.update_one.assert_not_awaited()


async def test_boost_ignores_missing_memory(storage):
    mem = storage._db["memories"]
    mem.find_one = AsyncMock(return_value=None)
    mem.update_one.reset_mock()
    await storage.boost_memory_importance(_OID)
    mem.update_one.assert_not_awaited()
