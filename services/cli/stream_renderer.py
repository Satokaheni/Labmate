from __future__ import annotations

from dataclasses import dataclass

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text


@dataclass
class _ToolRow:
    name: str
    label: str          # reasoning_why[:60] while running; summary once done
    status: str = "running"
    duration_ms: int = 0


class StreamRenderer:
    """Reduce orchestrator events into a renderable Rich frame."""

    def __init__(self) -> None:
        self._active: bool = False       # True after turn.start, False after turn.done
        self.reasoning_text: str = ""
        self.answer_text: str = ""
        self._answer_md: Markdown | None = None  # cached; rebuilt only on answer.done
        self.done: bool = False
        self.status: str = ""
        self.is_clarification: bool = False  # set when the agent asks for more info
        self.clarification_question: str = ""
        self._tools: dict[str, _ToolRow] = {}
        self._tool_order: list[str] = []

    def handle(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "turn.start":
            self._active = True
        elif etype == "reasoning":
            text = event.get("text", "")
            if self.reasoning_text:
                self.reasoning_text += "\n" + text
            else:
                self.reasoning_text = text
        elif etype == "tool.start":
            tid = event.get("tool_id", "")
            self._tools[tid] = _ToolRow(
                name=event.get("name", "tool"),
                label=(event.get("reasoning_why") or "")[:60],
            )
            self._tool_order.append(tid)
        elif etype == "tool.done":
            tid = event.get("tool_id", "")
            row = self._tools.get(tid)
            if row is not None:
                row.status = event.get("status", "done")
                row.label = event.get("summary", row.label)
                row.duration_ms = event.get("duration_ms", 0)
        elif etype == "answer.delta":
            self.answer_text += event.get("text", "")
        elif etype == "answer.done":
            self.answer_text = event.get("text", self.answer_text)
            self._answer_md = Markdown(self.answer_text)
        elif etype == "clarification_request":
            # Agent halted to ask for more info. Emitted before the answer.delta/done
            # that carry the question text, so the live preview can style it as a
            # clarification rather than flashing it as a plain answer.
            self.is_clarification = True
            self.clarification_question = event.get("question", "")
        elif etype == "turn.done":
            self._active = False
            self.done = True
            self.status = event.get("status", "complete")
        # All other types: silently ignored

    def _tool_line(self, row: _ToolRow) -> Text:
        if row.status == "running":
            return Text(f"⚙ {row.name}  {row.label}", style="yellow")
        secs = f"{row.duration_ms / 1000:.1f}s"
        if row.status == "error":
            return Text(f"✗ {row.name}  {row.label}  ({secs})", style="red")
        return Text(f"✓ {row.name}  {row.label}  ({secs})", style="green")

    def render(self):
        parts: list = []

        if self._active:
            parts.append(Text("◆ working…", style="cyan"))

        for tid in self._tool_order:
            parts.append(self._tool_line(self._tools[tid]))

        if self.reasoning_text:
            parts.append(Text(self.reasoning_text, style="dim italic"))

        answer = self.answer_text or self.clarification_question
        if self.is_clarification and answer:
            parts.append(Text("❓ I need a bit more to proceed:", style="bold yellow"))
            parts.append(Markdown(answer))
        elif self._answer_md is not None:
            parts.append(self._answer_md)
        elif self.answer_text:
            parts.append(Text(self.answer_text))

        if not parts:
            parts.append(Text("waiting…", style="dim"))

        return Group(*parts)
