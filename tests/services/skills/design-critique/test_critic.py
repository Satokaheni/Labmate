"""Tests for UICritic — all @pytest.mark.mocked, no GPU/network required."""
from __future__ import annotations

import base64
import os
import sys

import pytest

# Ensure the skill directory is on the path
_skill_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                 "services", "skills", "design-critique")
)
if _skill_dir not in sys.path:
    sys.path.insert(0, _skill_dir)

from critic import FOCUS_AREAS, CritiqueResult, UICritic


@pytest.mark.mocked
def test_critique_returns_result_with_items(mock_vision, sample_png):
    result = UICritic().critique(sample_png)
    assert isinstance(result, CritiqueResult)
    assert result.image_path == sample_png
    assert len(result.items) >= 1
    assert result.items[0].status in {"pass", "fail", "warning"}
    assert result.items[0].severity in {"high", "medium", "low"}


@pytest.mark.mocked
def test_default_checks_all_focus_areas(mock_vision, sample_png):
    result = UICritic().critique(sample_png)
    assert result.focus_areas_checked == FOCUS_AREAS


@pytest.mark.mocked
def test_focus_areas_filter_limits_checked(mock_vision, sample_png):
    subset = ["color_contrast", "typography"]
    result = UICritic().critique(sample_png, focus_areas=subset)
    assert result.focus_areas_checked == subset


@pytest.mark.mocked
def test_unknown_focus_area_falls_back_to_all(mock_vision, sample_png):
    result = UICritic().critique(sample_png, focus_areas=["not_a_real_area"])
    assert result.focus_areas_checked == FOCUS_AREAS


@pytest.mark.mocked
def test_prompt_contains_only_requested_areas(mock_vision, captured_calls, sample_png):
    UICritic().critique(sample_png, focus_areas=["typography"])
    text = captured_calls[0]["messages"][0]["content"][0]["text"]
    assert "typography" in text
    assert "color_contrast" not in text


@pytest.mark.mocked
def test_image_encoded_as_base64_png(mock_vision, captured_calls, sample_jpeg):
    UICritic().critique(sample_jpeg)
    content = captured_calls[0]["messages"][0]["content"]
    image_part = next(c for c in content if c["type"] == "image_url")
    url = image_part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.mocked
def test_compare_sends_both_images(mock_vision, captured_calls, sample_png, sample_jpeg):
    result = UICritic().compare(sample_png, sample_jpeg)
    content = captured_calls[0]["messages"][0]["content"]
    image_parts = [c for c in content if c["type"] == "image_url"]
    assert len(image_parts) == 2
    assert result["before_path"] == sample_png
    assert result["after_path"] == sample_jpeg


@pytest.mark.mocked
def test_overall_verdict_is_valid(mock_vision, sample_png):
    result = UICritic().critique(sample_png)
    assert result.overall in {"pass", "needs_work", "fail"}


@pytest.mark.mocked
def test_uses_gemma_base(mock_vision, captured_calls, sample_png, monkeypatch):
    monkeypatch.setenv("GEMMA_BASE", "http://host.docker.internal:8000/v1")
    import importlib
    import critic as critic_mod
    importlib.reload(critic_mod)
    # re-patch after reload
    monkeypatch.setattr(
        critic_mod.litellm,
        "completion",
        lambda **kw: captured_calls.append(kw) or
        {"choices": [{"message": {"content": '{"items":[],'
         '"overall":"pass","summary":"ok"}'}}]},
    )
    critic_mod.UICritic().critique(sample_png)
    assert captured_calls[-1]["api_base"] == "http://host.docker.internal:8000/v1"


@pytest.mark.mocked
def test_no_stdout_writes(mock_vision, sample_png, capsys):
    UICritic().critique(sample_png)
    captured = capsys.readouterr()
    assert captured.out == ""


@pytest.mark.mocked
def test_parses_fenced_json(monkeypatch, sample_png):
    import critic as critic_mod

    fenced = '```json\n{"items": [], "overall": "pass", "summary": "ok"}\n```'
    monkeypatch.setattr(
        critic_mod.litellm, "completion",
        lambda **kw: {"choices": [{"message": {"content": fenced}}]},
    )
    result = critic_mod.UICritic().critique(sample_png)
    assert result.overall == "pass"
