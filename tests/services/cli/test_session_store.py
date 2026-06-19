from __future__ import annotations
import pytest
from services.cli.session_store import SessionStore, SessionRecord


def test_append_and_list(tmp_path):
    store = SessionStore(tmp_path / "sessions.jsonl")
    store.append(SessionRecord(
        session_id="s-1",
        workspace_id="ws-1",
        workspace_name="my-lab",
        task_preview="Write hello world",
    ))
    store.append(SessionRecord(
        session_id="s-2",
        workspace_id="ws-1",
        workspace_name="my-lab",
        task_preview="Sort a list",
    ))
    sessions = store.list()
    assert len(sessions) == 2
    assert sessions[0].session_id == "s-2"   # most recent first


def test_list_by_workspace(tmp_path):
    store = SessionStore(tmp_path / "sessions.jsonl")
    store.append(SessionRecord(session_id="s-1", workspace_id="ws-1", workspace_name="a", task_preview="x"))
    store.append(SessionRecord(session_id="s-2", workspace_id="ws-2", workspace_name="b", task_preview="y"))
    result = store.list(workspace_id="ws-1")
    assert len(result) == 1
    assert result[0].session_id == "s-1"


def test_empty_store(tmp_path):
    store = SessionStore(tmp_path / "sessions.jsonl")
    assert store.list() == []
