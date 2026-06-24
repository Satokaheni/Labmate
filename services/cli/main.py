"""
Labmate CLI — interactive agent session.

Usage:
    python -m services.cli                   # start REPL with workspace picker
    python -m services.cli --resume s-abc    # resume a previous session
    python -m services.cli "do this task"    # one-shot (no REPL)

Environment variables:
    LABMATE_GATEWAY_URL   ws_gateway WebSocket URL (default ws://localhost:8787/ws)
    LABMATE_EMAIL         Login email (prompted if absent and no cached token)
    LABMATE_PASSWORD      Login password (prompted if absent and no cached token)
"""
from __future__ import annotations
import asyncio
import getpass
import json
import os
import uuid
from pathlib import Path
from typing import Optional

import typer

from .identity import load_or_create_identity, Identity
from .renderer import Renderer, extract_answer
from .repl import REPL, REPLContext
from .token_store import clear_token, load_token, save_token
from .workspace_picker import pick_workspace
from .ws_client import LabmateWSClient

app = typer.Typer(add_completion=False, help="Labmate — autonomous agent CLI")
_renderer = Renderer()

_WS_CACHE = Path.home() / ".labmate" / "workspaces.json"


def _gateway_url() -> str:
    return os.getenv("LABMATE_GATEWAY_URL", "ws://localhost:8787/ws")


def _load_workspaces(user_id: str) -> list[dict]:
    if not _WS_CACHE.exists():
        return []
    try:
        all_ws = json.loads(_WS_CACHE.read_text())
        return [w for w in all_ws if w.get("user_id") == user_id]
    except Exception:
        return []


def _default_workspace(user_id: str) -> dict:
    ws = {
        "workspace_id": "default",
        "name": "default",
        "paths": [os.getcwd()],
        "instructions": "",
        "user_id": user_id,
    }
    _save_workspace(ws)
    return ws


def _save_workspace(ws: dict) -> None:
    existing = []
    if _WS_CACHE.exists():
        try:
            existing = json.loads(_WS_CACHE.read_text())
        except Exception:
            pass
    if not any(
        w.get("workspace_id") == ws["workspace_id"]
        and w.get("user_id") == ws.get("user_id")
        for w in existing
    ):
        existing.append(ws)
    _WS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _WS_CACHE.write_text(json.dumps(existing, indent=2))


async def _get_token(ws_url: str) -> str:
    """Return a valid JWT: from cache, from env vars, or by interactive prompt."""
    token = load_token()
    if token:
        return token

    email = os.getenv("LABMATE_EMAIL", "")
    password = os.getenv("LABMATE_PASSWORD", "")

    if not email:
        email = input("Email: ").strip()
    if not password:
        password = getpass.getpass("Password: ")

    token = await LabmateWSClient.login(ws_url, email, password)
    save_token(token)
    return token


@app.command()
def main(
    prompt: Optional[str] = typer.Argument(None, help="One-shot task (skips REPL)"),
    resume: Optional[str] = typer.Option(None, "--resume", "-r", help="Resume session ID"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace ID"),
) -> None:
    asyncio.run(_async_main(prompt, resume, workspace))


async def _async_main(
    one_shot: str | None,
    resume_id: str | None,
    workspace_id_flag: str | None,
) -> None:
    ws_url = _gateway_url()
    identity = load_or_create_identity()
    existing_ws = _load_workspaces(identity.user_id)

    try:
        token = await _get_token(ws_url)
    except Exception as exc:
        _renderer.print_error(f"Login failed: {exc}")
        raise SystemExit(1)

    if resume_id:
        from .session_store import SessionStore
        prior_sessions = SessionStore().list()
        prior = next((s for s in prior_sessions if s.session_id == resume_id), None)
        if prior is None:
            _renderer.print_info(f"Session {resume_id[:8]}… not found — pick a workspace.")
        if prior:
            prior_ws = next((w for w in existing_ws
                            if w.get("workspace_id") == prior.workspace_id), None)
            if prior_ws is None:
                _renderer.print_info(f"Session {resume_id[:8]}… found but workspace not in local cache.")
            if prior_ws:
                _renderer.print_info(f"Resuming session {resume_id[:8]}… (workspace: {prior_ws['name']})")
                ctx = REPLContext(
                    identity=Identity(user_id=identity.user_id, display_name=identity.display_name),
                    workspace_id=prior_ws["workspace_id"],
                    workspace_name=prior_ws["name"],
                    workspace_paths=prior_ws.get("paths", []),
                    workspace_instructions=prior_ws.get("instructions"),
                    session_id=resume_id,
                    ws_url=ws_url,
                    token=token,
                )
                await REPL(ctx).run()
                return

    if workspace_id_flag:
        match = next((w for w in existing_ws if w["workspace_id"] == workspace_id_flag), None)
        if match:
            ws_choice_raw = match
        else:
            ws_choice_raw = {
                "workspace_id": workspace_id_flag,
                "name": workspace_id_flag,
                "paths": [os.getcwd()],
                "instructions": "",
                "user_id": identity.user_id,
            }
            _save_workspace(ws_choice_raw)
            _renderer.print_info(f"Workspace '{workspace_id_flag}' not found — created a seeded workspace.")
    elif one_shot or not existing_ws:
        ws_choice_raw = _default_workspace(identity.user_id)
    else:
        from .workspace_picker import WorkspaceChoice
        ws_choice = pick_workspace(existing_ws)
        ws_choice_raw = {
            "workspace_id": ws_choice.workspace_id,
            "name": ws_choice.name,
            "paths": ws_choice.paths,
            "instructions": ws_choice.instructions,
            "user_id": identity.user_id,
        }
        _save_workspace(ws_choice_raw)

    session_id = resume_id or str(uuid.uuid4())
    if not resume_id:
        _renderer.print_header(f"Session: {session_id}  (resume with --resume {session_id})")

    if one_shot:
        from .event_stream import run_task_with_streaming
        client = LabmateWSClient(ws_url, token)
        try:
            await client.connect()
        except PermissionError as exc:
            clear_token()
            _renderer.print_error(f"Auth failed: {exc}")
            raise SystemExit(1)

        task_id = str(uuid.uuid4())
        _renderer.print_workspace(ws_choice_raw["name"], ws_choice_raw["workspace_id"])
        workspace = ws_choice_raw.get("paths", [None])[0]
        try:
            await client.push_task(
                task_id=task_id,
                task=one_shot,
                session_id=session_id,
                user_id=identity.user_id,
                workspace_id=ws_choice_raw["workspace_id"],
            )
            result = await run_task_with_streaming(
                client, _renderer, task_id, workspace=workspace
            )
        except Exception as exc:
            _renderer.print_error(f"Connection error: {exc}")
            await client.aclose()
            raise SystemExit(1)
        await client.aclose()
        if not result.get("ok"):
            _renderer.print_error(result.get("error", "unknown"))
            raise SystemExit(1)
        state = result.get("state", {})
        if isinstance(state, dict) and state.get("awaiting_clarification"):
            _renderer.print_clarification(
                state.get("clarification_question") or extract_answer(state),
                session_id=session_id,
            )
        else:
            _renderer.print_answer(extract_answer(state), session_id=session_id)
        return

    ctx = REPLContext(
        identity=Identity(user_id=identity.user_id, display_name=identity.display_name),
        workspace_id=ws_choice_raw["workspace_id"],
        workspace_name=ws_choice_raw["name"],
        workspace_paths=ws_choice_raw.get("paths", []),
        workspace_instructions=ws_choice_raw.get("instructions"),
        session_id=session_id,
        ws_url=ws_url,
        token=token,
    )
    await REPL(ctx).run()


if __name__ == "__main__":
    app()
