"""FigureTriage: Gemma 4 vision scoring/description of figure PNGs (optional)."""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import litellm

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("paper-to-slides.figtriage")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")


@dataclass
class FigureScore:
    path: str
    slide_worthy: bool
    score: float           # 0.0 - 1.0
    description: str

    def to_dict(self) -> dict:
        return {"path": self.path, "slide_worthy": self.slide_worthy,
                "score": self.score, "description": self.description}


def _encode_png(path: str) -> str:
    data = Path(path).read_bytes()
    return base64.b64encode(data).decode("ascii")


class FigureTriage:
    VISION_PROMPT = (
        "Score this scientific figure for use on a single conference slide. "
        'Return ONLY JSON: {{"slide_worthy": bool, "score": float, '
        '"description": str}}. score is 0..1 readability-at-distance. '
        "Caption: {caption}"
    )

    def score_figure(self, path: str, caption: str = "") -> FigureScore:
        b64 = _encode_png(path)
        resp = litellm.completion(
            model=f"openai/{GEMMA_MODEL}",
            api_base=GEMMA_BASE,
            api_key="not-needed",
            temperature=0.0,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": self.VISION_PROMPT.format(caption=caption)},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
        )
        raw = resp["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start : end + 1])
        return FigureScore(
            path=path,
            slide_worthy=bool(data.get("slide_worthy", False)),
            score=float(data.get("score", 0.0)),
            description=data.get("description", ""),
        )

    def triage(self, figures: list[dict]) -> list[FigureScore]:
        scored: list[FigureScore] = []
        for f in figures:
            try:
                scored.append(self.score_figure(f["path"], f.get("caption", "")))
            except Exception as exc:  # one bad figure must not abort triage
                log.warning("figure triage failed for %s: %s", f.get("path"), exc)
                scored.append(FigureScore(f["path"], False, 0.0, ""))
        return scored
