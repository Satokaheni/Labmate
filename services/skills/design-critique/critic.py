"""design-critique — Gemma 4 vision critique of UI screenshots."""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
from typing import Literal

import litellm
from PIL import Image
from pydantic import BaseModel

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("design-critique.critic")

from vision_config import resolve_vision_endpoint

_VISION_NOT_CONFIGURED = {
    "error": "vision endpoint not configured (set VISION_BASE)"
}

FOCUS_AREAS: list[str] = [
    "visual_hierarchy",
    "spacing_alignment",
    "color_contrast",
    "typography",
    "layout_balance",
    "responsive_concerns",
    "accessibility_surface",
]


class ChecklistItem(BaseModel):
    issue: str
    status: Literal["pass", "fail", "warning"]
    note: str
    severity: Literal["high", "medium", "low"]


class CritiqueResult(BaseModel):
    image_path: str
    focus_areas_checked: list[str]
    items: list[ChecklistItem]
    overall: Literal["pass", "needs_work", "fail"]
    summary: str  # one-sentence overall verdict


class UICritic:
    def __init__(self, model: str | None = None, api_base: str | None = None) -> None:
        endpoint = resolve_vision_endpoint()
        self.enabled = endpoint is not None
        if endpoint is not None:
            self.api_base, default_model = endpoint
            self.model = model or default_model
        else:
            self.api_base, self.model = None, None

    def _encode_image(self, path: str) -> str:
        """Load any image, re-encode as PNG, return base64 string."""
        with Image.open(path) as img:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _call_gemma_vision(self, images: list[str], prompt: str) -> str:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for b64 in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                }
            )
        resp = litellm.completion(
            model=self.model,
            api_base=self.api_base,
            messages=[{"role": "user", "content": content}],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        return resp["choices"][0]["message"]["content"]

    def _critique_prompt(self, areas: list[str]) -> str:
        area_lines = "\n".join(f"- {a}" for a in areas)
        return (
            "You are a senior UI/UX reviewer. Critique the attached UI screenshot.\n"
            "Check ONLY these focus areas:\n"
            f"{area_lines}\n\n"
            "Return a SINGLE JSON object with this exact shape:\n"
            "{\n"
            '  "items": [\n'
            '    {"issue": str, "status": "pass"|"fail"|"warning",\n'
            '     "note": str, "severity": "high"|"medium"|"low"}\n'
            "  ],\n"
            '  "overall": "pass"|"needs_work"|"fail",\n'
            '  "summary": str\n'
            "}\n"
            "Produce at least one item per focus area. Keep notes concrete and "
            "actionable. Output JSON only, no prose, no markdown fences."
        )

    def critique(
        self, image_path: str, focus_areas: list[str] | None = None
    ) -> CritiqueResult | dict:
        if not self.enabled:
            return _VISION_NOT_CONFIGURED
        areas = self._resolve_areas(focus_areas)
        log.info("critiquing %s across %d areas", image_path, len(areas))
        b64 = self._encode_image(image_path)
        raw = self._call_gemma_vision([b64], self._critique_prompt(areas))
        data = self._parse_json(raw)
        items = [ChecklistItem(**i) for i in data.get("items", [])]
        return CritiqueResult(
            image_path=image_path,
            focus_areas_checked=areas,
            items=items,
            overall=data.get("overall", "needs_work"),
            summary=data.get("summary", ""),
        )

    def compare(self, before_path: str, after_path: str) -> dict:
        if not self.enabled:
            return _VISION_NOT_CONFIGURED
        log.info("comparing %s -> %s", before_path, after_path)
        before_b64 = self._encode_image(before_path)
        after_b64 = self._encode_image(after_path)
        prompt = (
            "You are a senior UI/UX reviewer. Image 1 is BEFORE, image 2 is AFTER "
            "a UI change. Critique the diff: what improved, what regressed, what is "
            "still unresolved.\n"
            "Return a SINGLE JSON object:\n"
            "{\n"
            '  "improved": [str], "regressed": [str], "unresolved": [str],\n'
            '  "overall": "pass"|"needs_work"|"fail", "summary": str\n'
            "}\n"
            "Output JSON only, no prose, no markdown fences."
        )
        raw = self._call_gemma_vision([before_b64, after_b64], prompt)
        result = self._parse_json(raw)
        result["before_path"] = before_path
        result["after_path"] = after_path
        return result

    def _resolve_areas(self, focus_areas: list[str] | None) -> list[str]:
        if not focus_areas:
            return list(FOCUS_AREAS)
        return [a for a in focus_areas if a in FOCUS_AREAS] or list(FOCUS_AREAS)

    @staticmethod
    def _parse_json(raw: str) -> dict:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip("` \n")
        return json.loads(text)
