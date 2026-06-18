import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "screenshot-to-component"
)
sys.path.insert(0, str(SERVER_DIR))


@pytest.fixture
def sample_image(tmp_path) -> str:
    """A tiny real PNG so encode_image_b64 reads genuine bytes."""
    from PIL import Image

    img = Image.new("RGB", (1440, 900), color=(255, 255, 255))
    path = tmp_path / "mock.png"
    img.save(path, format="PNG")
    return str(path)


GROUNDING_JSON = json.dumps(
    {
        "elements": [
            {
                "label": "header",
                "bounds": {"x": 0, "y": 0, "width": 1440, "height": 64},
                "description": "top nav bar",
                "children": [
                    {
                        "label": "button",
                        "bounds": {"x": 1300, "y": 16, "width": 120, "height": 32},
                        "description": "primary CTA button with blue background",
                    }
                ],
            }
        ]
    }
)

PLAN_JSON = json.dumps(
    {
        "root_component": "flex flex-col min-h-screen",
        "sections": [{"name": "header", "tailwind": "flex items-center justify-between"}],
        "color_palette": ["#1d4ed8", "#ffffff"],
        "typography_notes": "sans-serif, medium weight headings",
    }
)

COMPONENT_CODE = "export default function App() {\n  return <div className=\"flex\" />;\n}\n"


@pytest.fixture
def fake_llm(monkeypatch):
    """Patch llm.call_llm to return a stage-appropriate canned response.

    Records every call so tests can assert ordering and that images are sent.
    Routing is by message shape: a vision message (list content with image_url)
    => grounding; a 'Detected elements' request => planning; otherwise generation.
    """
    import llm

    calls: list[dict] = []

    def _fake(messages, *, temperature=0.2, max_tokens=4096):
        calls.append({"messages": messages, "max_tokens": max_tokens})
        user = messages[-1]["content"]
        if isinstance(user, list):  # vision message -> grounding stage
            return GROUNDING_JSON
        if "Detected elements" in user:  # grounding JSON in user message -> planning
            return PLAN_JSON
        # Otherwise it's generation (layout plan JSON in user message)
        return COMPONENT_CODE

    monkeypatch.setattr(llm, "call_llm", _fake)
    # Stages import call_llm by name; patch their references too.
    for mod_name in ("grounder", "planner", "generator"):
        mod = __import__(mod_name)
        if hasattr(mod, "call_llm"):
            monkeypatch.setattr(mod, "call_llm", _fake)
    return calls
