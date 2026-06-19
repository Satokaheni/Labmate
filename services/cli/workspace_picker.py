from __future__ import annotations
import uuid
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, IntPrompt

console = Console()


@dataclass
class WorkspaceChoice:
    workspace_id: str
    name: str
    paths: list[str]
    instructions: str | None


def pick_workspace(existing: list[dict]) -> WorkspaceChoice:
    """Present a workspace selection menu; return the chosen (or newly created) workspace."""
    if not existing:
        return _create_workspace()

    table = Table(title="Your Workspaces", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Name", style="bold")
    table.add_column("Paths")
    table.add_column("ID", style="dim")

    for i, ws in enumerate(existing, 1):
        paths = ", ".join(ws.get("paths", [])) or "[dim]none[/dim]"
        table.add_row(str(i), ws["name"], paths, ws["workspace_id"][:8] + "…")

    table.add_row(
        str(len(existing) + 1),
        "[green]+ New workspace[/green]",
        "",
        "",
    )
    console.print(table)

    choice = IntPrompt.ask(
        "Select workspace",
        default=1,
        choices=[str(i) for i in range(1, len(existing) + 2)],
    )

    if choice == len(existing) + 1:
        return _create_workspace()

    ws = existing[choice - 1]
    return WorkspaceChoice(
        workspace_id=ws["workspace_id"],
        name=ws["name"],
        paths=ws.get("paths", []),
        instructions=ws.get("instructions"),
    )


def _create_workspace() -> WorkspaceChoice:
    name = Prompt.ask("Workspace name", default="my-lab")
    paths_raw = Prompt.ask(
        "Code directories (comma-separated, or leave blank)",
        default="",
    )
    paths = [p.strip() for p in paths_raw.split(",") if p.strip()]
    instructions = Prompt.ask(
        "Per-workspace instructions (or leave blank)",
        default="",
    ) or None
    return WorkspaceChoice(
        workspace_id=str(uuid.uuid4()),
        name=name,
        paths=paths,
        instructions=instructions,
    )
