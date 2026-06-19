from services.orchestrator.models import User, Workspace, SessionMeta
from datetime import datetime, timezone

def test_user_defaults():
    u = User(display_name="Alice")
    assert u.user_id
    assert u.created_at <= datetime.now(timezone.utc)

def test_workspace_defaults():
    ws = Workspace(name="my-project", user_id="u-123")
    assert ws.workspace_id
    assert ws.paths == []
    assert ws.sources == []
    assert ws.description is None
    assert ws.instructions is None

def test_workspace_with_paths():
    ws = Workspace(
        name="ml-research",
        user_id="u-123",
        paths=["/workspace/myrepo", "/workspace/other"],
        sources=["arxiv:2401.12345"],
        instructions="Focus on Python. Use type hints.",
    )
    assert len(ws.paths) == 2
    assert "arxiv" in ws.sources[0]

def test_session_meta():
    sm = SessionMeta(
        session_id="s-abc",
        user_id="u-123",
        workspace_id="ws-456",
        task_preview="Write a sorting algorithm",
    )
    assert sm.session_id == "s-abc"
    assert sm.ok is True
    assert sm.completed_at is None
    assert sm.task_preview == "Write a sorting algorithm"
