# services/cli/token_store.py
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Optional

TOKEN_PATH = Path.home() / ".labmate" / "token.json"


def _decode_exp(token: str) -> Optional[int]:
    """Return the exp claim from a JWT payload, or None on any decode error."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        padding = -len(parts[1]) % 4
        payload = base64.urlsafe_b64decode(parts[1] + "=" * padding)
        return int(json.loads(payload).get("exp", 0))
    except Exception:
        return None


def load_token(*, path: Path = TOKEN_PATH) -> Optional[str]:
    """Return cached token if the file exists and the token has not expired."""
    if not path.exists():
        return None
    try:
        token = path.read_text().strip()
    except OSError:
        return None
    exp = _decode_exp(token)
    if exp is None or exp <= int(time.time()):
        return None
    return token


def save_token(token: str, *, path: Path = TOKEN_PATH) -> None:
    """Write token to disk (creates parent dirs as needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(token)
    path.chmod(0o600)


def clear_token(*, path: Path = TOKEN_PATH) -> None:
    """Delete the cached token file (idempotent)."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
