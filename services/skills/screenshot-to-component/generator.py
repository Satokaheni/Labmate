"""ComponentGenerator: LayoutPlan -> React/Tailwind (or other) component code.

stdout is sacred — never print(). SINGLE-GPU — all calls via llm.call_llm.
"""
from __future__ import annotations

import logging
from pathlib import Path

from llm import call_llm
from models import GenerationResult, LayoutPlan

log = logging.getLogger("screenshot-to-component.generator")

FRAMEWORK_GUIDANCE: dict[str, str] = {
    "react-tailwind": (
        "Generate a single self-contained React function component in TypeScript "
        "(.tsx). Use Tailwind utility classes. Prefer shadcn/ui component "
        "conventions and primitives where natural. Export the component as the "
        "default export. Do not include build config or imports for packages that "
        "are not standard React/Tailwind/shadcn."
    ),
    "html-css": (
        "Generate a single self-contained HTML file with an embedded <style> block "
        "using plain CSS (no Tailwind, no frameworks)."
    ),
    "vue-tailwind": (
        "Generate a single Vue 3 single-file component (<template>, <script setup>, "
        "no <style> needed) using Tailwind utility classes."
    ),
}

GENERATION_SYSTEM = (
    "You are a senior frontend engineer. Given a structured layout plan, write "
    "clean, production-quality component code. Output ONLY the code — no prose, no "
    "markdown fences, no explanation."
)


class ComponentGenerator:
    """Stage 3: synthesize component code from a layout plan."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model

    def generate(
        self,
        plan: LayoutPlan,
        framework: str = "react-tailwind",
        output_path: str | None = None,
    ) -> GenerationResult:
        if framework not in FRAMEWORK_GUIDANCE:
            raise ValueError(
                f"unsupported framework {framework!r}; "
                f"choose one of {sorted(FRAMEWORK_GUIDANCE)}"
            )

        log.info("generating %s component", framework)
        messages = [
            {"role": "system", "content": GENERATION_SYSTEM},
            {
                "role": "user",
                "content": (
                    FRAMEWORK_GUIDANCE[framework]
                    + "\n\nLayout plan (JSON):\n"
                    + plan.model_dump_json()
                    + "\n\nWrite the component now."
                ),
            },
        ]
        raw = call_llm(messages, temperature=0.2, max_tokens=8192)
        code = self._strip_fences(raw)

        if output_path:
            dest = Path(output_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code, encoding="utf-8")
            log.info("wrote component to %s", output_path)

        return GenerationResult(
            component_code=code, framework=framework, output_path=output_path
        )

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Remove a leading/trailing ```lang fence if the model added one."""
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            # drop the opening fence line
            lines = lines[1:]
            # drop the closing fence line if present
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        return stripped
