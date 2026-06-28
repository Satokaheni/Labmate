import os, sys
import pytest

_SKILL = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                      "services", "skills", "design-critique")
sys.path.insert(0, _SKILL)
import vision_config  # noqa: E402


def test_unset_returns_none(monkeypatch):
    monkeypatch.delenv("VISION_BASE", raising=False)
    assert vision_config.resolve_vision_endpoint() is None


def test_empty_returns_none(monkeypatch):
    monkeypatch.setenv("VISION_BASE", "")
    assert vision_config.resolve_vision_endpoint() is None


def test_set_returns_base_and_model(monkeypatch):
    monkeypatch.setenv("VISION_BASE", "http://localhost:8001/v1")
    monkeypatch.delenv("VISION_MODEL", raising=False)
    base, model = vision_config.resolve_vision_endpoint()
    assert base == "http://localhost:8001/v1"
    assert model == "openai/gemma-3-vision"


def test_model_override(monkeypatch):
    monkeypatch.setenv("VISION_BASE", "http://x/v1")
    monkeypatch.setenv("VISION_MODEL", "openai/gemma-3-12b")
    assert vision_config.resolve_vision_endpoint() == ("http://x/v1", "openai/gemma-3-12b")
