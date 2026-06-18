"""SpeakerNotes: per-slide talk track timed to the talk duration (optional)."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import litellm

from outline_planner import PresentationBlueprint

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("paper-to-slides.notes")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")


class SpeakerNotes:
    def generate(self, bp: PresentationBlueprint, out_dir: str) -> str:
        per_slide_sec = int(bp.talk_duration_min * 60 / max(1, len(bp.slides)))
        sections = []
        for sl in bp.slides:
            prompt = (
                f"Write a {per_slide_sec}-second spoken talk track for this slide. "
                f"Plain prose, first person, no markdown.\n"
                f"TITLE: {sl.title}\nBULLETS: {sl.bullets}\nHINT: {sl.speaker_note_hint}"
            )
            resp = litellm.completion(
                model=f"openai/{GEMMA_MODEL}",
                api_base=GEMMA_BASE,
                api_key="not-needed",
                temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
            )
            note = resp["choices"][0]["message"]["content"].strip()
            sections.append(f"## Slide {sl.index}: {sl.title}\n\n{note}\n")
        path = Path(out_dir) / "notes.md"
        path.write_text("\n".join(sections) + "\n")
        log.info("wrote speaker notes: %s", path)
        return str(path)
