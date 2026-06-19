from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock


def pytest_configure(config):
    config.addinivalue_line("markers", "mocked: no GPU required, all external calls mocked")
    config.addinivalue_line("markers", "live: requires running llama.cpp inference server")


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
def mock_chroma():
    """chromadb.AsyncHttpClient mock with get_or_create_collection."""
    client = AsyncMock(name="AsyncHttpClient")
    collection = AsyncMock(name="chroma_collection")
    collection.query.return_value = {
        "ids": [["mongo_id_1"]],
        "documents": [["a fact"]],
        "metadatas": [[{"session_id": "s1"}]],
        "distances": [[0.1]],
    }
    client.get_or_create_collection.return_value = collection
    client._collection = collection  # test hook
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
def storage(mock_mongo, mock_chroma, mock_redis):
    """A StorageManager with all three backends injected as mocks."""
    from services.orchestrator.storage_manager import StorageManager
    return StorageManager.from_clients(
        mongo=mock_mongo, chroma=mock_chroma, redis=mock_redis
    )
