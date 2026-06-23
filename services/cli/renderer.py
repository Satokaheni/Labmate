from __future__ import annotations
import json
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.live import Live
from rich.text import Text

from .stream_renderer import StreamRenderer


def extract_answer(state: Any) -> str:
    if isinstance(state, dict):
        if state.get("final_answer"):
            return state["final_answer"]
        root = state.get("goal_tree", {}).get("root", {})
        if root.get("result"):
            return root["result"]
        raw = json.dumps(state, default=str)
        return raw[:3000] + ("…" if len(raw) > 3000 else "")
    return str(state)


class Renderer:
    """Wraps Rich console for Labmate CLI output."""

    def __init__(self) -> None:
        self._console = Console(highlight=False)

    def print_answer(self, text: str, session_id: str = "") -> None:
        self._console.print()
        self._console.print(Markdown(text))
        if session_id:
            self._console.print(
                f"\n[dim]session: {session_id}[/dim]",
                highlight=False,
            )

    def print_clarification(self, question: str, session_id: str = "") -> None:
        """Render an agent clarification request distinctly from a final answer.

        The agent halted to ask for more info (awaiting_clarification); surface it
        as a question the user should reply to, not as a finished answer.
        """
        self._console.print()
        self._console.print("[bold yellow]❓ I need a bit more to proceed:[/bold yellow]")
        self._console.print(Markdown(question))
        self._console.print(
            "[dim]Reply with the details to continue"
            + (f" (session: {session_id})" if session_id else "")
            + ".[/dim]",
            highlight=False,
        )

    def print_error(self, message: str) -> None:
        self._console.print(f"[bold red]Error:[/bold red] {message}")

    def print_info(self, message: str) -> None:
        self._console.print(f"[dim]{message}[/dim]")

    def print_workspace(self, name: str, workspace_id: str) -> None:
        self._console.print(
            f"[bold cyan]Workspace:[/bold cyan] {name}  "
            f"[dim]({workspace_id[:8]}…)[/dim]"
        )

    @contextmanager
    def thinking(self, label: str = "Thinking…"):
        """Context manager that shows a spinner while work is in flight."""
        with Live(
            Spinner("dots", text=Text(label, style="dim")),
            console=self._console,
            refresh_per_second=10,
            transient=True,
        ):
            yield

    async def stream_live(self, stream) -> "StreamRenderer":
        """Drive a Rich Live frame from an EventStream.

        Returns the StreamRenderer so the caller can read accumulated
        answer/status. Does not subscribe or close `stream`.
        """
        sr = StreamRenderer()
        with Live(
            sr.render(),
            console=self._console,
            refresh_per_second=12,
            transient=False,
        ) as live:
            async for event in stream.events():
                sr.handle(event)
                live.update(sr.render())
        return sr
