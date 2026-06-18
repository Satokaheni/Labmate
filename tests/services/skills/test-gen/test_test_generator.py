import ast
import pytest

pytestmark = pytest.mark.mocked

LLM_REPLY = (
    "```python\n"
    "def test_add():\n    assert add(1, 2) == 3\n"
    "```\n"
    "These tests cover the happy path and a boundary."
)


def test_generate_returns_valid_python(monkeypatch):
    from test_generator import TestGenerator

    gen = TestGenerator()
    monkeypatch.setattr(gen, "_read_source", lambda p: "def add(a, b):\n    return a + b\n")
    monkeypatch.setattr(gen, "_call_llm", lambda prompt: LLM_REPLY)

    out = gen.generate("src/calc.py")

    assert "test_code" in out and "explanation" in out
    ast.parse(out["test_code"])               # must be valid Python
    assert "def test_add" in out["test_code"]


def test_improve_includes_mutant_diffs_in_prompt(monkeypatch):
    from test_generator import TestGenerator

    gen = TestGenerator()
    monkeypatch.setattr(gen, "_read_source", lambda p: "x = 1\n")

    captured = {}
    def fake_call(prompt):
        captured["prompt"] = prompt
        return "```python\ndef test_sub():\n    assert sub(2, 1) == 1\n```"
    monkeypatch.setattr(gen, "_call_llm", fake_call)

    mutants = ["--- a\n+++ b\n-return a + b\n+return a - b", "MUTANT_SENTINEL_2"]
    out = gen.improve("src/calc.py", "tests/test_calc.py", mutants)

    assert "additional_test_code" in out
    for m in mutants:
        assert m in captured["prompt"]        # every diff must reach the LLM


def test_generate_includes_existing_tests(monkeypatch):
    from test_generator import TestGenerator

    gen = TestGenerator()
    monkeypatch.setattr(gen, "_read_source", lambda p: "def add(a, b): return a + b")

    captured = {}
    monkeypatch.setattr(gen, "_call_llm", lambda prompt: captured.setdefault("p", prompt) or "```python\npass\n```")

    gen.generate("src/calc.py", existing_tests="def test_existing(): assert True")

    assert "test_existing" in captured["p"]
