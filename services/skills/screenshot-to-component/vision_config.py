"""Vision endpoint resolution (os-only — no heavy imports, unit-testable).

VISION_BASE is the OpenAI-compatible vision server (the dual-GPU host sets it to
http://localhost:8002/v1; unset elsewhere). Unset/empty => vision disabled.
"""
from __future__ import annotations

import os

_DEFAULT_VISION_MODEL = "openai/gemma-3-vision"


def resolve_vision_endpoint() -> tuple[str, str] | None:
    base = (os.getenv("VISION_BASE") or "").strip()
    if not base:
        return None
    model = (os.getenv("VISION_MODEL") or "").strip() or _DEFAULT_VISION_MODEL
    return base, model
