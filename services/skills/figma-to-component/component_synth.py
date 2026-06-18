"""ComponentSynthesizer: serialize a ComponentSpec to a prompt and call Gemma 4
via litellm to synthesize a React + Tailwind component.

CRITICAL: stdout is the JSON-RPC channel. NEVER print(); log to sys.stderr only.
Single-GPU: all LLM calls target GEMMA_BASE. There is no QWEN_BASE here.
"""
from __future__ import annotations

import json
import logging
import os
import re

import litellm

from models import ComponentResult, ComponentSpec

log = logging.getLogger("figma-to-component.synth")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")

SUPPORTED_FRAMEWORKS = {"react-tailwind"}


def _to_pascal_case(name: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", name)
    pascal = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not pascal or not pascal[0].isalpha():
        pascal = "Component" + pascal
    return pascal


class ComponentSynthesizer:
    def _build_prompt(self, spec: ComponentSpec, component_name: str) -> str:
        node_json = spec.node.model_dump_json(indent=2)
        tokens_json = json.dumps(spec.tokens, indent=2)
        return f"""You are an expert React + Tailwind CSS engineer. Convert the following
STRUCTURED Figma node into a single React function component using Tailwind CSS.

Rules:
- Use the auto-layout fields (direction, gap, padding, align) to choose flexbox
  Tailwind utilities (flex, flex-col, gap-*, p-*, items-*, justify-*).
- Map fills to Tailwind color/background utilities; map text_style to font utilities.
- Prefer referenced design tokens (by name) over hardcoded values where present.
- The component MUST be named exactly `{component_name}`.
- Emit a TypeScript props interface named `{component_name}Props` for any text or
  configurable content (each TEXT node's characters should be a prop).

Return ONLY a JSON object with this exact shape, no prose, no code fences:
{{
  "component_code": "<the full .tsx component source>",
  "props_interface": "<the exported TypeScript interface source>"
}}

REFERENCED DESIGN TOKENS:
{tokens_json}

FIGMA NODE (structured, includes auto-layout):
{node_json}
"""

    def _call_llm(self, prompt: str) -> str:
        resp = litellm.completion(
            model=f"openai/{GEMMA_MODEL}",
            api_base=GEMMA_BASE,
            api_key="not-needed",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return resp["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_synth_json(raw: str) -> dict:
        s = raw.strip()
        if s.startswith("```"):
            s = s.split("```", 2)[1]
            if s.startswith("json"):
                s = s[4:]
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object found in synthesis output")
        return json.loads(s[start : end + 1])

    def synthesize(self, spec: ComponentSpec, framework: str) -> ComponentResult:
        if framework not in SUPPORTED_FRAMEWORKS:
            raise ValueError(
                f"unsupported framework {framework!r}; "
                f"supported: {sorted(SUPPORTED_FRAMEWORKS)}"
            )
        component_name = _to_pascal_case(spec.node.name or "Component")
        prompt = self._build_prompt(spec, component_name)
        raw = self._call_llm(prompt)
        parsed = self._parse_synth_json(raw)
        return ComponentResult(
            component_code=parsed.get("component_code", ""),
            component_name=component_name,
            props_interface=parsed.get("props_interface", ""),
            framework=framework,
        )
