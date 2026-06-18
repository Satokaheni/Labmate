"""LayoutPlanner: GroundingResult -> hierarchical LayoutPlan.

stdout is sacred — never print(). SINGLE-GPU — all calls via llm.call_llm.
"""
from __future__ import annotations

import logging

from llm import call_llm, extract_json
from models import GroundingResult, LayoutPlan

log = logging.getLogger("screenshot-to-component.planner")

PLANNING_SYSTEM = (
    "You are a frontend layout planner. Given detected UI elements (with bounding "
    "boxes and labels), produce a hierarchical layout plan for a React + Tailwind "
    "component. Return STRICT JSON only (no prose, no code fences).\n"
    "Schema:\n"
    '{ "root_component": <Tailwind classes for the outermost container, e.g. '
    '"flex flex-col min-h-screen">, "sections": [ { "name": <string>, '
    '"tailwind": <container classes>, "children": [ ...nested sections or leaf '
    'descriptions... ] } ], "color_palette": [<hex strings>], '
    '"typography_notes": <string> }\n'
    "Infer the layout direction (row/column), spacing, and a small color palette "
    "from the elements and their descriptions."
)


class LayoutPlanner:
    """Stage 2: turn grounded elements into a structured layout plan."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model

    def plan(self, grounding: GroundingResult | str) -> LayoutPlan:
        if isinstance(grounding, str):
            grounding = GroundingResult.model_validate_json(grounding)

        log.info("planning over %d top-level element(s)", len(grounding.elements))
        messages = [
            {"role": "system", "content": PLANNING_SYSTEM},
            {
                "role": "user",
                "content": (
                    "Detected elements (JSON):\n"
                    + grounding.model_dump_json()
                    + "\n\nProduce the layout plan JSON."
                ),
            },
        ]
        raw = call_llm(messages, temperature=0.2, max_tokens=2048)
        plan = LayoutPlan.model_validate(extract_json(raw))
        log.info("plan: %d section(s), %d palette color(s)",
                 len(plan.sections), len(plan.color_palette))
        return plan
