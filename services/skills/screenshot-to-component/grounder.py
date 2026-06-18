"""UIGrounder: Gemma 4 vision -> bounding boxes + semantic labels.

stdout is sacred — never print(). SINGLE-GPU — all calls via llm.call_llm.
"""
from __future__ import annotations

import logging

from llm import call_llm, encode_image_b64, extract_json
from models import GroundingResult

log = logging.getLogger("screenshot-to-component.grounder")

GROUNDING_SYSTEM = (
    "You are a UI grounding model. Given a screenshot, detect the visible UI "
    "elements and return STRICT JSON only (no prose, no code fences).\n"
    "Schema:\n"
    '{ "elements": [ { "label": <one of: header|nav|sidebar|card|button|input|'
    'text|image|other>, "bounds": {"x": <px>, "y": <px>, "width": <px>, '
    '"height": <px>}, "description": <short string>, "children": [ ...same '
    'shape... ] } ] }\n'
    "Coordinates are absolute pixels with origin at the top-left of the image. "
    "Nest elements as children when one visually contains another (e.g. buttons "
    "inside a card). Return every salient region."
)


class UIGrounder:
    """Stage 1: detect UI elements + bounding boxes from a screenshot."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model  # reserved; llm module resolves the default model

    def ground(self, image_path: str) -> GroundingResult:
        b64, width, height = encode_image_b64(image_path)  # base64 BEFORE the LLM call
        log.info("grounding image %s (%dx%d)", image_path, width, height)

        messages = [
            {"role": "system", "content": GROUNDING_SYSTEM},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Image is {width}x{height} pixels. Detect the UI "
                            "elements and return the JSON described above."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            },
        ]

        raw = call_llm(messages, temperature=0.1, max_tokens=4096)
        data = extract_json(raw)
        # Trust measured dimensions over the model's guess.
        data["image_width"] = width
        data["image_height"] = height
        result = GroundingResult.model_validate(data)
        log.info("grounded %d top-level element(s)", len(result.elements))
        return result
