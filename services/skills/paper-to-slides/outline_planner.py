"""OutlinePlanner: pdf-parse JSON -> PresentationBlueprint (IMRaD slide plan)."""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field

import litellm

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("paper-to-slides.outline")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")


@dataclass
class SlideBlueprint:
    index: int
    title: str
    section: str          # 'title'|'intro'|'methods'|'results'|'discussion'|'conclusion'|'refs'
    bullets: list[str] = field(default_factory=list)
    figure_paths: list[str] = field(default_factory=list)   # absolute paths from pdf-parse
    table_html: str | None = None
    speaker_note_hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PresentationBlueprint:
    paper_title: str
    authors: list[str]
    venue: str
    talk_duration_min: int
    target_slide_count: int
    slides: list[SlideBlueprint] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "paper_title": self.paper_title,
            "authors": self.authors,
            "venue": self.venue,
            "talk_duration_min": self.talk_duration_min,
            "target_slide_count": self.target_slide_count,
            "slides": [s.to_dict() for s in self.slides],
        }


class OutlinePlanner:
    # 13 slides for a 20-min talk; scaled proportionally for other durations.
    IMRAD_SLIDE_BUDGET = {
        "title": 1,
        "outline": 1,        # skipped for talks < 15 min
        "intro": 2,
        "methods": 3,
        "results": 3,
        "discussion": 1,
        "conclusion": 1,
        "refs": 1,
    }

    def __init__(self) -> None:
        pass

    @staticmethod
    def _target_slide_count(talk_duration_min: int) -> int:
        # Rule: ~1 slide per 2 minutes, floor of 6.
        return max(6, int(talk_duration_min / 2))

    def _scaled_budget(self, talk_duration_min: int) -> dict[str, int]:
        """Scale the 13-slide base budget to the target count; drop 'outline' < 15 min."""
        budget = dict(self.IMRAD_SLIDE_BUDGET)
        if talk_duration_min < 15:
            budget.pop("outline", None)
        base_total = sum(budget.values())
        target = self._target_slide_count(talk_duration_min)
        scale = target / base_total
        scaled = {k: max(1, round(v * scale)) for k, v in budget.items()}
        log.info("scaled budget for %d min: %s (target=%d)",
                 talk_duration_min, scaled, target)
        return scaled

    PLAN_PROMPT = """You are a conference-talk planner. Build a slide outline for a \
{duration}-minute talk from the paper below. Produce EXACTLY these per-section slide \
counts: {budget}. Map paper content to IMRaD sections.

Return ONLY a JSON object with this shape:
{{"paper_title": str, "authors": [str], "venue": str,
  "slides": [{{"index": int, "title": str, "section": str,
              "bullets": [str], "figure_paths": [str], "table_html": str|null,
              "speaker_note_hint": str}}]}}

Use only figure paths drawn from AVAILABLE FIGURES. Keep bullets terse (<= 12 words).

PAPER METADATA: {metadata}
AVAILABLE FIGURES: {figures}
PAPER MARKDOWN (truncated):
{markdown}
"""

    def _call_llm(self, prompt: str) -> str:
        resp = litellm.completion(
            model=f"openai/{GEMMA_MODEL}",
            api_base=GEMMA_BASE,
            api_key="not-needed",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return resp["choices"][0]["message"]["content"]

    def _parse_blueprint_json(self, raw: str) -> PresentationBlueprint:
        s = raw.strip()
        if s.startswith("```"):
            s = s.split("```", 2)[1]
            if s.startswith("json"):
                s = s[4:]
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object found in outline output")
        data = json.loads(s[start : end + 1])
        slides = [
            SlideBlueprint(
                index=int(sl["index"]),
                title=sl["title"],
                section=sl["section"],
                bullets=sl.get("bullets", []),
                figure_paths=sl.get("figure_paths", []),
                table_html=sl.get("table_html"),
                speaker_note_hint=sl.get("speaker_note_hint", ""),
            )
            for sl in data.get("slides", [])
        ]
        return PresentationBlueprint(
            paper_title=data.get("paper_title", ""),
            authors=data.get("authors", []),
            venue=data.get("venue", ""),
            talk_duration_min=0,   # filled by caller in plan()
            target_slide_count=len(slides),
            slides=slides,
        )

    def plan(self, parsed_paper: dict, talk_duration_min: int = 20) -> PresentationBlueprint:
        meta = parsed_paper.get("metadata", {}) or {}
        figures = parsed_paper.get("figures", []) or []
        budget = self._scaled_budget(talk_duration_min)
        prompt = self.PLAN_PROMPT.format(
            duration=talk_duration_min,
            budget=json.dumps(budget),
            metadata=json.dumps({
                "title": meta.get("title", ""),
                "authors": meta.get("authors", []),
                "venue": meta.get("venue", meta.get("doi", "")),
            }),
            figures=json.dumps(
                [{"path": f["path"], "caption": f.get("caption", "")} for f in figures]
            ),
            markdown=(parsed_paper.get("markdown", "") or "")[:24000],
        )
        blueprint = self._parse_blueprint_json(self._call_llm(prompt))
        blueprint.talk_duration_min = talk_duration_min
        blueprint.target_slide_count = self._target_slide_count(talk_duration_min)
        log.info("planned %d slides for %d-min talk",
                 len(blueprint.slides), talk_duration_min)
        return blueprint
