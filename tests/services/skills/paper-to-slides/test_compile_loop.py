import subprocess
import pytest

from compile_loop import CompileLoop

pytestmark = pytest.mark.mocked


class _Proc:
    def __init__(self, rc):
        self.returncode = rc
        self.stdout = "log"
        self.stderr = ""


def test_success_first_attempt(monkeypatch, tmp_path):
    tex = tmp_path / "slides.tex"; tex.write_text("x")
    (tmp_path / "slides.pdf").write_text("pdf")
    monkeypatch.setattr("compile_loop.shutil.which", lambda b: "/usr/bin/tectonic")
    monkeypatch.setattr("compile_loop.subprocess.run", lambda *a, **k: _Proc(0))
    res = CompileLoop().compile(str(tex))
    assert res.success and res.attempts == 1


def test_retry_then_give_up(monkeypatch, tmp_path):
    tex = tmp_path / "slides.tex"; tex.write_text("x")
    monkeypatch.setattr("compile_loop.shutil.which", lambda b: "/usr/bin/tectonic")
    monkeypatch.setattr("compile_loop.subprocess.run", lambda *a, **k: _Proc(1))
    calls = {"n": 0}
    def _repair(**k):
        calls["n"] += 1
        return {"choices": [{"message": {"content": "fixed"}}]}
    monkeypatch.setattr("compile_loop.litellm.completion", _repair)
    res = CompileLoop().compile(str(tex), max_retries=5)
    assert res.success is False and res.attempts == 5
    assert calls["n"] == 4  # repair called between attempts, not after the last


def test_fallback_to_pdflatex(monkeypatch, tmp_path):
    tex = tmp_path / "slides.tex"; tex.write_text("x")
    (tmp_path / "slides.pdf").write_text("pdf")
    monkeypatch.setattr("compile_loop.shutil.which",
                        lambda b: None if b == "tectonic" else "/usr/bin/pdflatex")
    captured = {}
    def _run(cmd, **k):
        captured["cmd"] = cmd
        return _Proc(0)
    monkeypatch.setattr("compile_loop.subprocess.run", _run)
    res = CompileLoop().compile(str(tex))
    assert res.success and captured["cmd"][0] == "pdflatex"


def test_no_engine_returns_error(monkeypatch, tmp_path):
    tex = tmp_path / "slides.tex"; tex.write_text("x")
    monkeypatch.setattr("compile_loop.shutil.which", lambda b: None)
    res = CompileLoop().compile(str(tex), max_retries=2)
    assert res.success is False and "PATH" in res.final_error
