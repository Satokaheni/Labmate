"""Pure serialize/deserialize unit tests for the inner-loop checkpoint.

No Mongo, no asyncio in this file — to_dict/from_dict are pure sync functions.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator.loop_checkpoint import (
    CHECKPOINT_VERSION,
    LoopCheckpoint,
    to_dict,
    from_dict,
    CheckpointStore,
    FakeCheckpointStore,
)


def _sample() -> LoopCheckpoint:
    return LoopCheckpoint(
        task_id="task-123",
        goal="fix the factorial off-by-one",
        messages=[
            {"role": "system", "content": "you are a coding agent"},
            {"role": "user", "content": "fix the factorial off-by-one"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "def factorial(...)"},
        ],
        used=3,
        absolute_turns=3,
        grace_used=False,
        edited_files=["src/math.py"],
        tests_passed=False,
        verify_nudges_used=1,
        loop_signatures=["read_file::{}", "write_file::{\"path\":\"x\"}"],
        tools_used=["read_file", "write_file"],
        start_monotonic_offset=12.5,
        turn=3,
    )


def test_round_trip_is_lossless():
    cp = _sample()
    restored = from_dict(to_dict(cp))
    assert restored == cp


def test_to_dict_is_json_serializable():
    cp = _sample()
    d = to_dict(cp)
    # Must survive a full JSON round-trip (Mongo stores BSON, but we keep it
    # JSON-able so the dict is trivially inspectable/loggable).
    reparsed = json.loads(json.dumps(d))
    assert from_dict(reparsed) == cp


def test_to_dict_includes_version():
    assert to_dict(_sample())["version"] == CHECKPOINT_VERSION


def test_from_dict_none_returns_none():
    assert from_dict(None) is None


def test_from_dict_missing_required_key_returns_none():
    d = to_dict(_sample())
    del d["messages"]
    assert from_dict(d) is None


def test_from_dict_unknown_version_returns_none():
    d = to_dict(_sample())
    d["version"] = 999
    assert from_dict(d) is None


def test_from_dict_wrong_type_returns_none():
    d = to_dict(_sample())
    d["used"] = "not-an-int"
    assert from_dict(d) is None


def test_from_dict_tolerates_extra_keys():
    d = to_dict(_sample())
    d["_id"] = "mongo-object-id"  # Mongo adds this; load must ignore it
    d["saved_at"] = "2026-06-26T00:00:00Z"
    assert from_dict(d) == _sample()


@pytest.mark.asyncio
async def test_fake_store_save_load_clear_round_trip():
    store = FakeCheckpointStore()
    cp = _sample()
    assert await store.load("task-123") is None
    await store.save(cp)
    assert await store.load("task-123") == cp
    await store.clear("task-123")
    assert await store.load("task-123") is None


@pytest.mark.asyncio
async def test_fake_store_save_overwrites_latest():
    store = FakeCheckpointStore()
    await store.save(_sample())
    cp2 = _sample()
    cp2.used = 99
    await store.save(cp2)
    assert (await store.load("task-123")).used == 99


@pytest.mark.asyncio
async def test_mongo_store_save_upserts_by_task_id():
    col = MagicMock()
    col.update_one = AsyncMock()
    store = CheckpointStore(col)
    await store.save(_sample())
    args, kwargs = col.update_one.call_args
    assert args[0] == {"task_id": "task-123"}      # filter
    assert kwargs.get("upsert") is True


@pytest.mark.asyncio
async def test_mongo_store_load_returns_checkpoint():
    col = MagicMock()
    col.find_one = AsyncMock(return_value=to_dict(_sample()))
    store = CheckpointStore(col)
    assert await store.load("task-123") == _sample()


@pytest.mark.asyncio
async def test_mongo_store_load_missing_returns_none():
    col = MagicMock()
    col.find_one = AsyncMock(return_value=None)
    store = CheckpointStore(col)
    assert await store.load("nope") is None


@pytest.mark.asyncio
async def test_store_errors_are_swallowed_never_raised():
    # save/load/clear must be best-effort: a raising collection must NOT
    # propagate (the ReAct loop must never break on a checkpoint failure).
    col = MagicMock()
    col.update_one = AsyncMock(side_effect=RuntimeError("mongo down"))
    col.find_one = AsyncMock(side_effect=RuntimeError("mongo down"))
    col.delete_one = AsyncMock(side_effect=RuntimeError("mongo down"))
    store = CheckpointStore(col)
    await store.save(_sample())          # must not raise
    assert await store.load("task-123") is None   # error -> None
    await store.clear("task-123")        # must not raise


# ─────────────────────────────────────────────────────────────────────────
# Storage manager accessor test
# ─────────────────────────────────────────────────────────────────────────

from services.orchestrator.loop_checkpoint import CHECKPOINT_COLLECTION


def test_storage_manager_exposes_loop_checkpoint_collection():
    from services.orchestrator.storage_manager import StorageManager
    db = MagicMock()
    sentinel = MagicMock()
    db.__getitem__ = MagicMock(return_value=sentinel)
    mongo = MagicMock()
    mongo.__getitem__ = MagicMock(return_value=db)
    redis = MagicMock()
    sm = StorageManager.from_clients(mongo=mongo, chroma=MagicMock(), redis=redis)
    col = sm.loop_checkpoint_collection
    db.__getitem__.assert_called_with(CHECKPOINT_COLLECTION)
    assert col is sentinel


# ─────────────────────────────────────────────────────────────────────────
# Wire-in tests (Insertion A/B/C + flag + _checkpoint_active + module reload)
# ─────────────────────────────────────────────────────────────────────────

import json as _json
from unittest.mock import patch

from services.orchestrator import events
from services.orchestrator.coding_orchestrator import AsyncOrchestrator


def _finish_response(summary: str):
    msg = MagicMock()
    msg.tool_calls = [MagicMock(
        id="c1",
        function=MagicMock(name="finish", arguments=_json.dumps({"summary": summary})),
    )]
    # MagicMock(name=...) sets the mock's repr name, not .name — set explicitly:
    msg.tool_calls[0].function.name = "finish"
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.mark.asyncio
async def test_run_react_loop_resumes_from_preseeded_checkpoint(monkeypatch):
    monkeypatch.setenv("ENABLE_LOOP_CHECKPOINT", "1")
    # Reload the module-level flag computed at import time.
    import importlib
    import services.orchestrator.coding_orchestrator as co
    importlib.reload(co)

    store = FakeCheckpointStore()
    # Pre-seed a checkpoint as if a prior process crashed after turn 2.
    seeded = LoopCheckpoint(
        task_id="task-resume",
        goal="resume me",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "resume me"},
            {"role": "assistant", "content": "prior work done"},
        ],
        used=2, absolute_turns=2, turn=2,
        tools_used=["read_file"],
    )
    await store.save(seeded)

    orch = co.AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.checkpoint_store = store
    orch.redis = MagicMock()

    # Active emitter so current_task_id() returns our task id.
    em = events.EventEmitter(MagicMock(), "task-resume")
    token = events.current_emitter.set(em)
    try:
        with patch.object(co.events, "is_cancelled", new=AsyncMock(return_value=False)), \
             patch.object(co.events, "read_and_clear_steer", new=AsyncMock(return_value=None)), \
             patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new=AsyncMock(return_value=_finish_response("finished after resume"))):
            result = await orch._run_react_loop("resume me", 6)
    finally:
        events.current_emitter.reset(token)
        importlib.reload(co)  # restore default flag for other tests

    # The seeded prior-work message must be present -> we resumed, not restarted.
    assert result["ok"] is True
    assert "finished after resume" in result["summary"]
    # Checkpoint cleared on finish.
    assert await store.load("task-resume") is None


@pytest.mark.asyncio
async def test_flag_off_performs_no_checkpoint_io(monkeypatch):
    # Default flag (OFF) -> store is never touched even when wired.
    # Ensure the flag is OFF by not setting the env var and reloading the module.
    import importlib
    import services.orchestrator.coding_orchestrator as co
    # Clear the env var to ensure it defaults to OFF.
    monkeypatch.delenv("ENABLE_LOOP_CHECKPOINT", raising=False)
    importlib.reload(co)

    store = FakeCheckpointStore()
    store.load = AsyncMock(wraps=store.load)
    store.save = AsyncMock(wraps=store.save)
    store.clear = AsyncMock(wraps=store.clear)

    orch = co.AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.checkpoint_store = store
    orch.redis = MagicMock()

    em = events.EventEmitter(MagicMock(), "task-off")
    token = events.current_emitter.set(em)
    try:
        with patch.object(co.events, "is_cancelled", new=AsyncMock(return_value=False)), \
             patch.object(co.events, "read_and_clear_steer", new=AsyncMock(return_value=None)), \
             patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
                   new=AsyncMock(return_value=_finish_response("done"))):
            await orch._run_react_loop("no checkpoint", 6)
    finally:
        events.current_emitter.reset(token)
        importlib.reload(co)  # restore state for other tests

    store.load.assert_not_awaited()
    store.save.assert_not_awaited()
    store.clear.assert_not_awaited()
