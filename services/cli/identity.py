from __future__ import annotations
import json
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

IDENTITY_PATH = Path.home() / ".labmate" / "identity.json"


@dataclass
class Identity:
    user_id: str
    display_name: str

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Path = IDENTITY_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path = IDENTITY_PATH) -> "Identity":
        data = json.loads(path.read_text())
        return cls(**data)


def load_or_create_identity(path: Path | None = None) -> Identity:
    if path is None:
        path = IDENTITY_PATH
    if path.exists():
        return Identity.load(path)
    name = input("Welcome to Labmate! Enter your display name: ").strip() or "User"
    ident = Identity(user_id=str(uuid.uuid4()), display_name=name)
    ident.save(path)
    return ident
