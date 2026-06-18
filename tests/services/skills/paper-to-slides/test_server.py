import json
import pytest

pytestmark = pytest.mark.mocked


def test_generate_pipeline_order(monkeypatch, tmp_path, parsed_paper):
    import server
    pp = tmp_path / "paper.json"; pp.write_text(json.dumps(parsed_paper))
    order = []

    class _BP:
        slides = [1, 2, 3]
    monkeypatch.setattr(server.OutlinePlanner, "plan",
                        lambda self, p, d: order.append("outline") or _BP())
    monkeypatch.setattr(server.SlideGenerator, "generate",
                        lambda self, bp, o, f: order.append("gen") or str(tmp_path / "slides.tex"))

    class _Res:
        success = True; pdf_path = str(tmp_path / "slides.pdf")
    monkeypatch.setattr(server.CompileLoop, "compile",
                        lambda self, p: order.append("compile") or _Res())

    out = json.loads(server._run_generate(
        {"parsed_paper_path": str(pp), "talk_duration_min": 20}))
    assert order == ["outline", "gen", "compile"]
    assert out["slide_count"] == 3 and out["compile_success"] is True
    assert set(out) == {"tex_path", "pdf_path", "notes_path",
                        "slide_count", "compile_success"}


def test_no_stdout_writes(monkeypatch, tmp_path, parsed_paper, capsys):
    import server
    pp = tmp_path / "paper.json"; pp.write_text(json.dumps(parsed_paper))
    monkeypatch.setattr(server.OutlinePlanner, "plan",
                        lambda self, p, d: type("B", (), {"slides": []})())
    monkeypatch.setattr(server.SlideGenerator, "generate",
                        lambda self, bp, o, f: str(tmp_path / "slides.md"))
    server._run_generate({"parsed_paper_path": str(pp), "output_format": "marp"})
    captured = capsys.readouterr()
    assert captured.out == ""   # stdout must be empty; logs go to stderr
