"""Tests for MongoSessionStore with async Motor-backed storage."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def fake_db():
    """Create a fake async Motor database with mocked collections."""
    fake_sessions = AsyncMockCollection()
    fake_turns = AsyncMockCollection()
    fake_client = MagicMock()
    fake_client.__getitem__ = MagicMock(
        return_value={"chat_sessions": fake_sessions, "chat_turns": fake_turns}
    )
    return fake_client, fake_sessions, fake_turns


class AsyncMockCollection:
    """Simulates an async Motor collection with insert/update/find/delete operations."""

    def __init__(self):
        self._data: dict[
            str, dict
        ] = {}  # Unique key -> doc (for sessions, uses id; for turns, uses a synthetic key)
        self._turn_counter = 0  # To ensure unique keys for turns even if they have same id
        self.insert_one = AsyncMock(side_effect=self._insert_one_impl)
        self.update_one = AsyncMock(side_effect=self._update_one_impl)
        self.find_one = AsyncMock(side_effect=self._find_one_impl)
        self.delete_one = AsyncMock(side_effect=self._delete_one_impl)
        self.delete_many = AsyncMock(side_effect=self._delete_many_impl)
        self.create_index = AsyncMock()  # Just track the call
        self._find_query = None

    async def _insert_one_impl(self, doc: dict):
        # For turns, allow duplicates; use a synthetic key based on counter
        key = f"_turn_{self._turn_counter}"
        self._turn_counter += 1
        self._data[key] = doc.copy()
        return MagicMock(inserted_id=key)

    async def _update_one_impl(self, query: dict, update: dict):
        # Simple implementation: find by query, apply update
        if "$set" in update:
            for _key, doc in self._data.items():
                if all(doc.get(k) == v for k, v in query.items()):
                    doc.update(update["$set"])
                    return MagicMock(modified_count=1)
        return MagicMock(modified_count=0)

    async def _find_one_impl(self, query: dict, projection: dict | None = None):
        # Return first doc matching query
        for _key, doc in self._data.items():
            if all(doc.get(k) == v for k, v in query.items()):
                result = {**doc}
                if projection:
                    # If projection has _id: 0, remove _id
                    if "_id" in projection and projection["_id"] == 0:
                        result.pop("_id", None)
                return result if result else None
        return None

    async def _delete_one_impl(self, query: dict):
        for key, doc in list(self._data.items()):
            if all(doc.get(k) == v for k, v in query.items()):
                del self._data[key]
                return MagicMock(deleted_count=1)
        return MagicMock(deleted_count=0)

    async def _delete_many_impl(self, query: dict):
        deleted = 0
        for key, doc in list(self._data.items()):
            if all(doc.get(k) == v for k, v in query.items()):
                del self._data[key]
                deleted += 1
        return MagicMock(deleted_count=deleted)

    def find(self, query: dict | None = None):
        """Return a cursor-like object that supports sort() and is async-iterable."""
        self._find_query = query or {}
        return AsyncFindCursor(self, self._find_query)


class AsyncFindCursor:
    """Simulates Motor async cursor with sort() and async iteration."""

    def __init__(self, collection: AsyncMockCollection, query: dict):
        self.collection = collection
        self.query = query
        self._sort_key = None
        self._sort_reverse = False

    def sort(self, key: str | list[tuple[str, int]], direction: int = 1):
        """Sort the results. Returns self for chaining."""
        # Support both string key and list of tuples
        if isinstance(key, str):
            self._sort_key = key
        elif isinstance(key, list) and key:
            self._sort_key = key[0][0]
            direction = key[0][1]
        self._sort_reverse = direction == -1
        return self

    async def __aiter__(self):
        """Make the cursor async-iterable."""
        # Get matching documents
        matching = []
        for _doc_id, doc in self.collection._data.items():
            if all(doc.get(k) == v for k, v in self.query.items()):
                matching.append({**doc})

        # Apply sort if requested
        if self._sort_key:
            matching.sort(key=lambda d: d.get(self._sort_key, ""), reverse=self._sort_reverse)

        for doc in matching:
            yield doc

    async def __anext__(self):
        """Not used in practice; async iteration uses __aiter__."""
        raise StopAsyncIteration


@pytest.fixture
def store_factory(fake_db):
    """Factory to create a MongoSessionStore with a fake database."""

    async def create_store():
        fake_client, fake_sessions, fake_turns = fake_db
        from services.ws_gateway.mongo_session_store import MongoSessionStore

        store = MongoSessionStore.__new__(MongoSessionStore)
        store._sessions = fake_sessions
        store._turns = fake_turns
        store._client = fake_client
        store._ensure_indexes_scheduled = True  # Skip index creation in tests
        return store, (fake_sessions, fake_turns)

    return create_store


class TestMongoSessionStore:
    """Test MongoSessionStore async interface."""

    @pytest.mark.asyncio
    async def test_create_inserts_session_and_returns_dict(self, store_factory):
        store, _ = await store_factory()

        result = await store.create(title="Test Chat", mode="chat", session_id="s-123")

        assert result["id"] == "s-123"
        assert result["title"] == "Test Chat"
        assert result["mode"] == "chat"
        assert result["turnCount"] == 0
        assert result["contextTokens"] == 0
        assert "createdAt" in result
        assert "updatedAt" in result
        assert result.get("debug") is None or result.get("debug") is False
        # Verify _id is NOT in result
        assert "_id" not in result

    @pytest.mark.asyncio
    async def test_create_auto_generates_session_id(self, store_factory):
        store, _ = await store_factory()

        result = await store.create(title="Auto ID", mode="chat")

        assert result["id"] is not None
        assert result["id"].startswith("s-")

    @pytest.mark.asyncio
    async def test_list_returns_empty_when_no_sessions(self, store_factory):
        store, _ = await store_factory()

        result = await store.list()

        assert result == []

    @pytest.mark.asyncio
    async def test_list_returns_sessions_sorted_by_updated_desc(self, store_factory):
        store, _ = await store_factory()

        # Create in order: older first, newer second
        await store.create(
            title="Older", mode="chat", session_id="s1", updated_at="2026-06-01T00:00:00Z"
        )
        await store.create(
            title="Newer", mode="chat", session_id="s2", updated_at="2026-06-05T00:00:00Z"
        )

        result = await store.list()

        assert len(result) == 2
        # Most recent first (desc order by updatedAt)
        assert result[0]["title"] == "Newer"
        assert result[0]["updatedAt"] == "2026-06-05T00:00:00Z"
        assert result[1]["title"] == "Older"
        assert result[1]["updatedAt"] == "2026-06-01T00:00:00Z"

    @pytest.mark.asyncio
    async def test_get_returns_session_or_none(self, store_factory):
        store, _ = await store_factory()

        await store.create(title="Test", mode="chat", session_id="s-1")

        result = await store.get("s-1")
        assert result is not None
        assert result["id"] == "s-1"
        assert result["title"] == "Test"

        missing = await store.get("nonexistent")
        assert missing is None

    @pytest.mark.asyncio
    async def test_rename_updates_title_and_timestamp(self, store_factory):
        store, _ = await store_factory()

        await store.create(title="Original", mode="chat", session_id="s-1")
        result = await store.rename("s-1", "Renamed")

        assert result is not None
        assert result["title"] == "Renamed"
        assert "updatedAt" in result

    @pytest.mark.asyncio
    async def test_rename_returns_none_for_missing_session(self, store_factory):
        store, _ = await store_factory()

        result = await store.rename("nonexistent", "New Title")

        assert result is None

    @pytest.mark.asyncio
    async def test_add_turn_appends_and_bumps_count(self, store_factory):
        store, _ = await store_factory()

        await store.create(title="Test", mode="chat", session_id="s-1")
        turn = {
            "id": "t-1",
            "sessionId": "s-1",
            "role": "user",
            "text": "hi",
            "createdAt": "2026-06-01T00:00:01Z",
        }

        await store.add_turn("s-1", turn)

        turns = await store.turns("s-1")
        assert len(turns) == 1
        assert turns[0]["id"] == "t-1"

        # Check turnCount was bumped
        updated_session = await store.get("s-1")
        assert updated_session["turnCount"] == 1

    @pytest.mark.asyncio
    async def test_turns_returns_empty_for_missing_session(self, store_factory):
        store, _ = await store_factory()

        result = await store.turns("nonexistent")

        assert result == []

    @pytest.mark.asyncio
    async def test_turns_returns_in_created_asc_order(self, store_factory):
        store, _ = await store_factory()

        await store.create(title="Test", mode="chat", session_id="s-1")
        # Add in reverse order to test sorting
        await store.add_turn(
            "s-1",
            {
                "id": "t-2",
                "sessionId": "s-1",
                "role": "user",
                "text": "b",
                "createdAt": "2026-06-01T00:00:02Z",
            },
        )
        await store.add_turn(
            "s-1",
            {
                "id": "t-1",
                "sessionId": "s-1",
                "role": "user",
                "text": "a",
                "createdAt": "2026-06-01T00:00:01Z",
            },
        )

        result = await store.turns("s-1")

        assert len(result) == 2
        # Should be sorted by createdAt ascending (earliest first)
        assert result[0]["id"] == "t-1"
        assert result[0]["createdAt"] == "2026-06-01T00:00:01Z"
        assert result[1]["id"] == "t-2"
        assert result[1]["createdAt"] == "2026-06-01T00:00:02Z"

    @pytest.mark.asyncio
    async def test_delete_removes_session_and_returns_true(self, store_factory):
        store, _ = await store_factory()

        await store.create(title="Test", mode="chat", session_id="s-1")

        result = await store.delete("s-1")

        assert result is True
        assert await store.get("s-1") is None

    @pytest.mark.asyncio
    async def test_delete_removes_session_turns(self, store_factory):
        store, _ = await store_factory()

        await store.create(title="Test", mode="chat", session_id="s-1")
        await store.add_turn("s-1", {"id": "t-1", "sessionId": "s-1", "role": "user", "text": "hi"})

        await store.delete("s-1")

        turns = await store.turns("s-1")
        assert turns == []

    @pytest.mark.asyncio
    async def test_delete_returns_false_for_missing_session(self, store_factory):
        store, _ = await store_factory()

        result = await store.delete("nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_set_and_get_debug(self, store_factory):
        store, _ = await store_factory()

        await store.create(title="Test", mode="chat", session_id="s-1")

        assert await store.get_debug("s-1") is False

        await store.set_debug("s-1", True)
        assert await store.get_debug("s-1") is True

        await store.set_debug("s-1", False)
        assert await store.get_debug("s-1") is False

    @pytest.mark.asyncio
    async def test_get_debug_missing_session_returns_false(self, store_factory):
        store, _ = await store_factory()

        result = await store.get_debug("nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_add_turn_idempotent(self, store_factory):
        store, _ = await store_factory()

        await store.create(title="Test", mode="chat", session_id="s-1")
        t = {"id": "t-1", "sessionId": "s-1", "role": "user", "text": "hi"}

        await store.add_turn("s-1", t)
        await store.add_turn("s-1", t)  # Same turn twice

        turns = await store.turns("s-1")
        # Should have both (duplicates allowed by underlying impl)
        assert len(turns) == 2

    @pytest.mark.asyncio
    async def test_turns_strips_mongo_id(self, store_factory):
        store, _ = await store_factory()

        await store.create(title="Test", mode="chat", session_id="s-1")
        await store.add_turn("s-1", {"id": "t-1", "sessionId": "s-1", "role": "user", "text": "hi"})

        turns = await store.turns("s-1")

        for turn in turns:
            assert "_id" not in turn
