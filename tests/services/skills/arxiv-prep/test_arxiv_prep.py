from __future__ import annotations
import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "arxiv-prep" / "arxiv_prep.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("arxiv_prep", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_clean_source_invokes_cleaner(tmp_path):
    ap = _load()
    proj = tmp_path / "paper"
    proj.mkdir()
    with patch.object(ap.subprocess, "run") as mrun:
        mrun.return_value = MagicMock(returncode=0, stdout="cleaned", stderr="")
        out = ap.clean_source(str(proj))
    assert out["ok"] is True
    assert "cleaned" in out["log"]
    assert mrun.called


def test_verify_compile_collects_errors(tmp_path):
    ap = _load()
    proj = tmp_path / "paper"
    proj.mkdir()
    (proj / "main.tex").write_text(r"\documentclass{article}\begin{document}x\end{document}")
    with patch.object(ap.subprocess, "run") as mrun:
        mrun.return_value = MagicMock(returncode=1, stdout="", stderr="error: undefined control sequence")
        out = ap.verify_compile(str(proj))
    assert out["ok"] is False
    assert any("undefined control sequence" in e for e in out["errors"])


def test_extract_metadata_reads_title_author_abstract(tmp_path):
    ap = _load()
    proj = tmp_path / "paper"
    proj.mkdir()
    (proj / "main.tex").write_text(
        r"\title{My Paper}" "\n"
        r"\author{Jane Doe}" "\n"
        r"\begin{abstract}We study X.\end{abstract}" "\n"
    )
    out = ap.extract_metadata(str(proj))
    assert out["title"] == "My Paper"
    assert "Jane Doe" in out["authors"]
    assert "We study X" in out["abstract"]


def test_package_tarball_creates_archive(tmp_path):
    ap = _load()
    proj = tmp_path / "paper"
    proj.mkdir()
    (proj / "main.tex").write_text("x")
    out = ap.package_tarball(str(proj), str(tmp_path / "submission.tar.gz"))
    assert out["ok"] is True
    assert Path(out["path"]).exists()


def test_anonymize_returns_diff_without_editing(tmp_path):
    ap = _load()
    proj = tmp_path / "paper"
    proj.mkdir()
    main = proj / "main.tex"
    original = r"\author{Jane Doe}" "\n" r"\section{Intro}" "\n"
    main.write_text(original)

    def fake_llm(prompt: str) -> str:
        return r"\author{}" "\n" r"\section{Intro}" "\n"

    out = ap.anonymize(str(proj), llm=fake_llm)
    assert "diff" in out
    assert isinstance(out["changes"], list)
    assert main.read_text() == original


def test_skill_md_frontmatter_parses():
    import yaml
    skill_md = _MODULE_PATH.parent / "SKILL.md"
    assert skill_md.exists()
    text = skill_md.read_text(encoding="utf-8")
    # Parse YAML frontmatter between --- markers
    import re
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    assert m, "No YAML frontmatter found"
    meta = yaml.safe_load(m.group(1))
    assert meta["name"] == "arxiv-prep"
    assert "submission" in meta["description"].lower()


def test_server_lists_all_five_tools():
    import importlib.util
    import sys
    server_path = _MODULE_PATH.parent / "server.py"
    # Add the skill module directory to sys.path so relative imports work
    skill_dir = str(_MODULE_PATH.parent)
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    try:
        spec = importlib.util.spec_from_file_location("arxiv_prep_server", server_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["arxiv_prep_server"] = mod
        spec.loader.exec_module(mod)
        import asyncio
        tools = asyncio.run(mod.list_tools())
        names = {t.name for t in tools}
        assert names == {
            "clean_source", "verify_compile", "anonymize",
            "package_tarball", "extract_metadata",
        }
    finally:
        if skill_dir in sys.path:
            sys.path.remove(skill_dir)


def test_skill_runner_catalogs_arxiv_prep():
    from services.skill_runner.skill_runner import SkillRunner
    skills_root = _MODULE_PATH.resolve().parent.parent  # .../services/skills
    runner = SkillRunner(roots=[skills_root])
    runner.discover()
    assert "arxiv-prep" in runner.catalog
    prompt = runner.catalog_prompt()
    assert "arxiv-prep" in prompt
    assert "submission" in prompt.lower()
