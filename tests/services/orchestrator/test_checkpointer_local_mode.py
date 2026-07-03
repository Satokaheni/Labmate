"""Piece 1: build_graph selects a local SqliteSaver in LABMATE_LOCAL_MODE,
and keeps the MongoDBSaver pod path unchanged when the flag is off."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langgraph.checkpoint.memory import MemorySaver

from services.orchestrator.coding_orchestrator import AsyncOrchestrator, CodingOrchestrator
from services.orchestrator.graph import build_graph


def _mocks():
    return MagicMock(spec=CodingOrchestrator), MagicMock(spec=AsyncOrchestrator)


def test_local_mode_builds_sqlite_checkpointer(monkeypatch, tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    db = tmp_path / "nested" / "state.sqlite"  # parent dir does not exist yet
    monkeypatch.setenv("LABMATE_LOCAL_MODE", "1")
    monkeypatch.setenv("LABMATE_STATE_DB", str(db))

    mock_orch, mock_async = _mocks()
    graph, cp = build_graph(mock_orch, mock_async)

    assert isinstance(cp, SqliteSaver)
    assert db.exists()  # parent dir created + file opened
    assert graph is not None


def test_local_mode_checkpointer_round_trips(monkeypatch, tmp_path):
    """The returned SqliteSaver actually persists and reloads a checkpoint."""
    db = tmp_path / "state.sqlite"
    monkeypatch.setenv("LABMATE_LOCAL_MODE", "1")
    monkeypatch.setenv("LABMATE_STATE_DB", str(db))

    mock_orch, mock_async = _mocks()
    _graph, cp = build_graph(mock_orch, mock_async)

    cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    checkpoint = {
        "v": 1,
        "id": "c1",
        "ts": "2026-07-03T00:00:00+00:00",
        "channel_values": {"n": 7},
        "channel_versions": {},
        "versions_seen": {},
    }
    cp.put(cfg, checkpoint, {}, {})
    loaded = cp.get(cfg)
    assert loaded is not None
    assert loaded["channel_values"]["n"] == 7


def test_pod_mode_still_builds_mongodb_saver(monkeypatch):
    """Flag OFF (default) -> pod path: MongoDBSaver constructed via the patched
    symbols, SqliteSaver branch NOT taken. Behavior-preserving."""
    monkeypatch.delenv("LABMATE_LOCAL_MODE", raising=False)
    monkeypatch.delenv("LABMATE_STATE_DB", raising=False)

    mock_orch, mock_async = _mocks()
    sentinel = MemorySaver()
    with patch("pymongo.MongoClient", return_value=MagicMock()):
        with patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=sentinel):
            graph, cp = build_graph(mock_orch, mock_async)
    assert cp is sentinel  # the pod construction path ran
    assert graph is not None
