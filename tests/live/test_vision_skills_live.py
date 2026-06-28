import os, sys, urllib.request
import pytest

from tests.live.conftest import require_service

pytestmark = pytest.mark.live

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "ui_sample.png")


def _vision_reachable() -> bool:
    base = (os.getenv("VISION_BASE") or "").rstrip("/")
    if not base:
        return False
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=2) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False


def test_design_critique_runs_on_image():
    require_service(_vision_reachable, "VISION_BASE vision endpoint")
    skill = os.path.join(os.path.dirname(__file__), "..", "..",
                         "services", "skills", "design-critique")
    sys.path.insert(0, skill)
    import critic  # noqa: E402
    result = critic.UICritic().critique(_FIXTURE)
    # pydantic CritiqueResult or a dict; either way it's a real, non-empty result
    payload = result if isinstance(result, dict) else result.model_dump()
    assert payload and "error" not in payload, f"critique failed: {payload}"


def test_screenshot_to_component_runs_on_image():
    require_service(_vision_reachable, "VISION_BASE vision endpoint")
    skill = os.path.join(os.path.dirname(__file__), "..", "..",
                         "services", "skills", "screenshot-to-component")
    sys.path.insert(0, skill)
    import pipeline  # noqa: E402
    out = pipeline.Pipeline().generate(_FIXTURE)
    # Pipeline.generate returns a dict with component_code, layout_plan, output_path
    assert out is not None
    text = str(out)
    assert "error" not in text.lower() and len(text) > 0
