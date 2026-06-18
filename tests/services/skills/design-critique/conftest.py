import json
from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def sample_png(tmp_path) -> str:
    p = tmp_path / "ui.png"
    Image.new("RGB", (64, 48), (200, 200, 200)).save(p, format="PNG")
    return str(p)


@pytest.fixture
def sample_jpeg(tmp_path) -> str:
    p = tmp_path / "ui.jpg"
    Image.new("RGB", (64, 48), (10, 20, 30)).save(p, format="JPEG")
    return str(p)


@pytest.fixture
def captured_calls():
    return []


@pytest.fixture
def mock_vision(monkeypatch, captured_calls):
    """Patch litellm.completion. Records kwargs; returns a canned JSON body."""
    import sys
    import os

    # Ensure the skill directory is on the path so critic can be imported
    skill_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "..",
        "services", "skills", "design-critique"
    )
    skill_dir = os.path.abspath(skill_dir)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)

    import critic as critic_mod

    payload = {
        "items": [
            {
                "issue": "Primary CTA blends into background",
                "status": "fail",
                "note": "Increase contrast or use accent color.",
                "severity": "high",
            }
        ],
        "overall": "needs_work",
        "summary": "Solid layout, contrast needs work.",
    }

    def fake_completion(**kwargs):
        captured_calls.append(kwargs)
        return {
            "choices": [{"message": {"content": json.dumps(payload)}}]
        }

    monkeypatch.setattr(critic_mod.litellm, "completion", fake_completion)
    return payload
