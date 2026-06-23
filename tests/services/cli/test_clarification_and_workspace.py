"""CLI tests for the clarification affordance + auto-seeded default workspace."""
from __future__ import annotations

import json

from rich.console import Console


def _plain(renderable) -> str:
    console = Console(width=100, no_color=True, highlight=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


# ── StreamRenderer: live preview is clarification-aware ──────────────────────

def test_clarification_request_marks_renderer():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "turn.start", "task": "make it better"})
    r.handle({"type": "clarification_request", "question": "What is 'it'?", "task": "make it better"})
    assert r.is_clarification is True
    assert r.clarification_question == "What is 'it'?"


def test_clarification_live_preview_shows_affordance():
    """After clarification_request, the streamed text renders under the ❓ heading,
    not as a plain answer."""
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "turn.start", "task": "make it better"})
    r.handle({"type": "clarification_request", "question": "What is 'it'?", "task": "x"})
    r.handle({"type": "answer.delta", "text": "What is 'it', and what does better mean?"})
    out = _plain(r.render())
    assert "❓" in out
    assert "better mean" in out


def test_non_clarification_answer_unchanged():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "turn.start", "task": "2+2"})
    r.handle({"type": "answer.delta", "text": "2 + 2 is 4."})
    r.handle({"type": "answer.done", "text": "2 + 2 is 4."})
    out = _plain(r.render())
    assert "2 + 2 is 4." in out
    assert "❓" not in out


def test_clarification_falls_back_to_question_when_no_answer_text():
    from services.cli.stream_renderer import StreamRenderer
    r = StreamRenderer()
    r.handle({"type": "clarification_request", "question": "Which file?", "task": "x"})
    out = _plain(r.render())
    assert "Which file?" in out and "❓" in out


# ── Renderer.print_clarification ─────────────────────────────────────────────

def test_print_clarification_renders_question_and_hint():
    from services.cli.renderer import Renderer
    r = Renderer()
    r._console = Console(width=100, no_color=True, highlight=False)
    with r._console.capture() as cap:
        r.print_clarification("What is 'it'?", session_id="sess-123")
    out = cap.get()
    assert "What is 'it'?" in out
    assert "❓" in out
    assert "Reply with the details" in out
    assert "sess-123" in out


# ── Auto-seeded default workspace ────────────────────────────────────────────

def test_default_workspace_creates_and_persists(tmp_path, monkeypatch):
    from services.cli import main as cli_main
    cache = tmp_path / "workspaces.json"
    monkeypatch.setattr(cli_main, "_WS_CACHE", cache)

    ws = cli_main._default_workspace("user-1")
    assert ws["workspace_id"] == "default"
    assert ws["name"] == "default"
    assert ws["user_id"] == "user-1"
    assert ws["paths"]  # rooted at some path
    assert cache.exists()
    saved = json.loads(cache.read_text())
    assert any(w["workspace_id"] == "default" and w["user_id"] == "user-1" for w in saved)


def test_default_workspace_is_idempotent(tmp_path, monkeypatch):
    from services.cli import main as cli_main
    cache = tmp_path / "workspaces.json"
    monkeypatch.setattr(cli_main, "_WS_CACHE", cache)
    cli_main._default_workspace("user-1")
    cli_main._default_workspace("user-1")
    saved = json.loads(cache.read_text())
    assert sum(1 for w in saved if w["workspace_id"] == "default" and w["user_id"] == "user-1") == 1


def test_default_workspace_persists_per_user(tmp_path, monkeypatch):
    """Two users sharing the literal 'default' id are both persisted (dedup is
    by (workspace_id, user_id), not workspace_id alone)."""
    from services.cli import main as cli_main
    cache = tmp_path / "workspaces.json"
    monkeypatch.setattr(cli_main, "_WS_CACHE", cache)
    cli_main._default_workspace("user-1")
    cli_main._default_workspace("user-2")
    saved = json.loads(cache.read_text())
    defaults = [w for w in saved if w["workspace_id"] == "default"]
    assert {w["user_id"] for w in defaults} == {"user-1", "user-2"}
