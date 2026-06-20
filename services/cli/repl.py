from __future__ import annotations
import asyncio
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style

from .identity import Identity
from .redis_client import LabmateRedisClient
from .renderer import Renderer, extract_answer
from .session_store import SessionStore, SessionRecord

HISTORY_PATH = Path.home() / ".labmate" / "input_history"

SLASH_COMMANDS = {
    "/help": "Show this help",
    "/sessions": "List recent sessions",
    "/workspace": "Show current workspace",
    "/quit": "Exit Labmate",
}

PROMPT_STYLE = Style.from_dict({"prompt": "bold cyan"})


@dataclass
class REPLContext:
    identity: Identity
    workspace_id: str
    workspace_name: str
    workspace_paths: list[str]
    workspace_instructions: str | None
    session_id: str
    redis_url: str


class REPL:
    def __init__(self, ctx: REPLContext) -> None:
        self._ctx = ctx
        self._renderer = Renderer()
        self._redis = LabmateRedisClient(ctx.redis_url)
        self._sessions = SessionStore()
        self._prompt_session = PromptSession(
            history=FileHistory(str(HISTORY_PATH)),
            style=PROMPT_STYLE,
        )

    async def run(self) -> None:
        self._renderer.print_workspace(self._ctx.workspace_name, self._ctx.workspace_id)
        self._renderer.print_info(
            f"Hi {self._ctx.identity.display_name}! "
            "Type your task, !<cmd> to run shell commands, or /help."
        )

        while True:
            try:
                raw = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._prompt_session.prompt("> "),
                )
            except (EOFError, KeyboardInterrupt):
                self._renderer.print_info("Goodbye.")
                break

            line = raw.strip()
            if not line:
                continue

            if line.startswith("!"):
                self._run_shell(line[1:].strip())
            elif line.startswith("/"):
                if not await self._handle_slash(line):
                    break
            else:
                await self._send_task(line)

        await self._redis.aclose()

    def _run_shell(self, cmd: str) -> None:
        result = subprocess.run(cmd, shell=True, text=True, capture_output=True)
        if result.stdout:
            self._renderer._console.print(result.stdout, end="")
        if result.stderr:
            self._renderer._console.print(result.stderr, end="", style="dim red")

    async def _handle_slash(self, line: str) -> bool:
        """Return False to signal exit."""
        cmd = line.split()[0].lower()
        if cmd in ("/quit", "/exit", "/q"):
            self._renderer.print_info("Goodbye.")
            return False
        elif cmd == "/help":
            for c, desc in SLASH_COMMANDS.items():
                self._renderer._console.print(f"  [bold]{c}[/bold]  {desc}")
        elif cmd == "/workspace":
            self._renderer.print_workspace(self._ctx.workspace_name, self._ctx.workspace_id)
            if self._ctx.workspace_paths:
                self._renderer.print_info("Paths: " + ", ".join(self._ctx.workspace_paths))
        elif cmd == "/sessions":
            sessions = self._sessions.list(workspace_id=self._ctx.workspace_id, limit=10)
            if not sessions:
                self._renderer.print_info("No sessions yet.")
            for s in sessions:
                self._renderer._console.print(
                    f"  [dim]{s.created_at[:16]}[/dim]  {s.task_preview[:60]}"
                    f"  [dim]{s.session_id[:8]}…[/dim]"
                )
        else:
            self._renderer.print_error(f"Unknown command: {line}")
        return True

    async def _send_task(self, task: str) -> None:
        task_id = str(uuid.uuid4())
        turn_session_id = str(uuid.uuid4())
        self._sessions.append(SessionRecord(
            session_id=turn_session_id,
            workspace_id=self._ctx.workspace_id,
            workspace_name=self._ctx.workspace_name,
            task_preview=task[:120],
        ))

        try:
            await self._redis.push_task(
                task_id=task_id,
                task=task,
                session_id=turn_session_id,
                user_id=self._ctx.identity.user_id,
                workspace_id=self._ctx.workspace_id,
            )
            from .event_stream import run_task_with_streaming
            result = await run_task_with_streaming(self._redis, self._renderer, task_id)
        except Exception as exc:
            self._renderer.print_error(f"Connection error: {exc}")
            return

        if not result.get("ok"):
            self._renderer.print_error(result.get("error", "Unknown error"))
            return

        answer = extract_answer(result.get("state", {}))
        self._renderer.print_answer(answer, session_id=turn_session_id)
