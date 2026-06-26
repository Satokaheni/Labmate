# tests/services/orchestrator/test_load_skill_guard.py
from __future__ import annotations

import json

import pytest

from services.orchestrator.load_skill_guard import (
    is_repeat_load,
    already_loaded_message,
)

pytestmark = pytest.mark.mocked


def test_is_repeat_load_true_when_already_loaded():
    assert is_repeat_load("code-review", {"code-review"}) is True


def test_is_repeat_load_false_when_first_load():
    assert is_repeat_load("test-gen", {"code-review"}) is False


def test_is_repeat_load_false_for_empty_name():
    # An empty / missing name must never be treated as a repeat — it falls
    # through to the real loader, which surfaces the proper "unknown skill" error.
    assert is_repeat_load("", {"code-review"}) is False


def test_already_loaded_message_shape_and_text():
    msg = already_loaded_message("code-review", {"test-gen", "code-review"})
    assert msg["name"] == "load_skill"
    resp = msg["response"]
    assert resp["status"] == "already_loaded"
    assert resp["name"] == "code-review"
    assert resp["loaded"] == ["code-review", "test-gen"]  # sorted
    text = resp["message"]
    assert "already loaded" in text
    assert "code-review" in text
    assert "do not load_skill it again" in text
    assert "Loaded skills: code-review, test-gen" in text


def test_already_loaded_message_is_json_serializable():
    msg = already_loaded_message("code-review", {"code-review"})
    # Must survive json.dumps so it can become a tool-result string.
    assert json.loads(json.dumps(msg))["response"]["status"] == "already_loaded"
