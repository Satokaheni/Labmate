from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "mocked: no GPU required, all external calls mocked")
    config.addinivalue_line("markers", "live: requires running llama.cpp inference server")


@pytest.fixture(autouse=True)
async def _close_local_stores():
    """Close every LocalStore aiosqlite connection after each test.

    Each pytest test gets its own event loop; an aiosqlite connection left open
    past its loop raises ``RuntimeError: Event loop is closed`` from its worker
    thread at a later teardown (surfaced as a PytestUnhandledThreadExceptionWarning).
    Draining all live stores in-loop here — plus resetting the process-cached
    singleton — prevents the dangling thread regardless of how a test created its
    store.
    """
    yield
    import services.orchestrator.local_store as _ls

    await _ls.close_all_local_stores()
    _ls._STORE = None


@pytest.fixture
def mock_mongo():
    """AsyncIOMotorClient mock. client[db][collection] -> AsyncMock collection."""
    client = MagicMock(name="AsyncIOMotorClient")
    db = MagicMock(name="db")
    collections: dict[str, AsyncMock] = {}

    def get_collection(name):
        if name not in collections:
            col = AsyncMock(name=f"collection:{name}")
            # default return shapes
            col.insert_one.return_value = MagicMock(inserted_id="mongo_id_1")
            col.update_one.return_value = MagicMock(modified_count=1)
            col.count_documents.return_value = 0
            col.create_index = AsyncMock(return_value=None)
            collections[name] = col
        return collections[name]

    db.__getitem__.side_effect = get_collection
    db.get_collection.side_effect = get_collection
    client.__getitem__.return_value = db
    client._collections = collections  # test hook
    return client


@pytest.fixture
def mock_redis():
    """redis.asyncio client mock."""
    r = AsyncMock(name="redis")
    r.xadd.return_value = b"1-0"
    r.get.return_value = None
    r.set.return_value = True
    return r


@pytest.fixture
def storage(mock_mongo, mock_redis):
    """A StorageManager with both backends injected as mocks."""
    from services.orchestrator.storage_manager import StorageManager

    return StorageManager.from_clients(mongo=mock_mongo, redis=mock_redis)
