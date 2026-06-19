from __future__ import annotations
import pytest
from services.cli.renderer import Renderer, extract_answer


def test_extract_answer_final():
    state = {"final_answer": "The answer is 42."}
    assert extract_answer(state) == "The answer is 42."


def test_extract_answer_root_result():
    state = {"goal_tree": {"root": {"result": "done"}}}
    assert extract_answer(state) == "done"


def test_extract_answer_fallback():
    state = {}
    result = extract_answer(state)
    assert isinstance(result, str)


def test_renderer_instantiates():
    r = Renderer()
    assert r is not None
