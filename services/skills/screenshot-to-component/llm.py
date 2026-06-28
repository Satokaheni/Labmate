"""Shared LLM access for the screenshot_to_component skill.

Vision-endpoint: every call targets VISION_BASE (vision server on dual-GPU host).
Vision is opt-in: VISION_BASE unset => raises VisionNotConfigured, clean disable path.
stdout is sacred — never print(); litellm is routed to stderr via root logging.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path

import litellm

log = logging.getLogger("screenshot-to-component.llm")

from vision_config import resolve_vision_endpoint

VISION_API_KEY = os.getenv("VISION_API_KEY", "not-needed")  # local llama.cpp ignores the key


class VisionNotConfigured(RuntimeError):
    """Raised when VISION_BASE is unset so the pipeline can return a clean error."""


def encode_image_b64(image_path: str) -> tuple[str, int, int]:
    """Read an image, return (base64 PNG data URL payload, width, height).

    Always re-encodes to PNG so the data URL mime type is correct regardless of
    the source format (jpg, webp, etc.).
    """
    from io import BytesIO

    from PIL import Image

    src = Path(image_path)
    if not src.is_file():
        raise FileNotFoundError(f"image not found: {image_path}")

    with Image.open(src) as img:
        img = img.convert("RGB")
        width, height = img.size
        buf = BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64, width, height


def call_llm(messages: list[dict], *, temperature: float = 0.2, max_tokens: int = 4096) -> str:
    """Single litellm chat completion against the vision server. Returns content text."""
    endpoint = resolve_vision_endpoint()
    if endpoint is None:
        raise VisionNotConfigured("vision endpoint not configured (set VISION_BASE)")
    api_base, model = endpoint
    log.info("LLM call: %d message(s), max_tokens=%d", len(messages), max_tokens)
    resp = litellm.completion(
        model=model,
        api_base=api_base,
        api_key=VISION_API_KEY,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp["choices"][0]["message"]["content"] or ""


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM response, tolerating code fences/prose."""
    # Strip ```json ... ``` fences if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        # Fall back to the first balanced-looking { ... } span.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
    if candidate is None:
        raise ValueError(f"no JSON object found in LLM response: {text[:200]!r}")
    return json.loads(candidate)
