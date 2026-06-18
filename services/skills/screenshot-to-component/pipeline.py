"""Pipeline: chains grounding -> planning -> generation.

stdout is sacred — never print(). SINGLE-GPU — every stage uses GEMMA_BASE.
"""
from __future__ import annotations

import logging

from generator import ComponentGenerator
from grounder import UIGrounder
from planner import LayoutPlanner

log = logging.getLogger("screenshot-to-component.pipeline")


class Pipeline:
    def __init__(
        self,
        grounder: UIGrounder | None = None,
        planner: LayoutPlanner | None = None,
        generator: ComponentGenerator | None = None,
    ) -> None:
        # Defaults are real stages; tests inject fakes to assert ordering.
        self.grounder = grounder or UIGrounder()
        self.planner = planner or LayoutPlanner()
        self.generator = generator or ComponentGenerator()

    def generate(
        self,
        image_path: str,
        framework: str = "react-tailwind",
        output_path: str | None = None,
    ) -> dict:
        # ORDER MATTERS: ground -> plan -> generate.
        grounding = self.grounder.ground(image_path)
        plan = self.planner.plan(grounding)
        gen = self.generator.generate(plan, framework=framework, output_path=output_path)
        return {
            "component_code": gen.component_code,
            "layout_plan": plan.model_dump(),
            "output_path": gen.output_path,
        }
