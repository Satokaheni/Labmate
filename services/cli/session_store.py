from __future__ import annotations
import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

SESSION_PATH = Path.home() / ".labmate" / "sessions.jsonl"


@dataclass
class SessionRecord:
    session_id: str
    workspace_id: str
    workspace_name: str
    task_preview: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class SessionStore:
    def __init__(self, path: Path = SESSION_PATH) -> None:
        self._path = path

    def append(self, record: SessionRecord) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    def list(self, workspace_id: str | None = None, limit: int = 50) -> list[SessionRecord]:
        if not self._path.exists():
            return []
        records = []
        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if workspace_id and d.get("workspace_id") != workspace_id:
                    continue
                records.append(SessionRecord(**d))
            except (json.JSONDecodeError, TypeError):
                continue
        return list(reversed(records))[-limit:]
