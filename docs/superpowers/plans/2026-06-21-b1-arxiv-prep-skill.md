# B1 arxiv-prep Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan.

**Goal:** Add an `arxiv-prep` skill that flattens, cleans, compiles, optionally anonymizes, and packages a finished LaTeX project for submission.

**Architecture:** A standard Labmate skill under `services/skills/arxiv-prep/`: `SKILL.md` (frontmatter + body), a logic module `arxiv_prep.py` with five pure-ish functions, and an MCP `server.py` exposing them over stdio. The `SkillRunner` discovers it from SKILL.md frontmatter at startup; the orchestrator's `catalog_prompt()` then advertises it. Heavy lifting shells out to `arxiv-latex-cleaner` and the `tectonic` binary.

**Tech Stack:** Python, `mcp` server SDK, `arxiv-latex-cleaner`, `tectonic` (external binary), `PyYAML`, pytest.

> **Skill rules:** Never `print()` — log to `sys.stderr` only. `server.py` uses `mcp.server.Server` + `mcp.server.stdio.stdio_server`. `tectonic` is a binary, not a pip package; verify via `subprocess.run(["tectonic", "--version"])`.

---

### Task 1: Create the skill logic module

**Files:**
- Create: `services/skills/arxiv-prep/arxiv_prep.py`
- Create: `services/skills/arxiv-prep/__init__.py`
- Create: `tests/services/skills/arxiv-prep/__init__.py`
- Create: `tests/services/skills/arxiv-prep/test_arxiv_prep.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/skills/arxiv-prep/__init__.py` (empty), then `tests/services/skills/arxiv-prep/test_arxiv_prep.py`. The skill dir name has a hyphen (not a valid Python module name), so the test imports the logic module by file path:

```python
from __future__ import annotations
import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
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
    # File must be UNCHANGED on disk (diff returned for approval).
    assert main.read_text() == original
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/skills/arxiv-prep/test_arxiv_prep.py -v`
Expected: FAIL (module does not exist)

- [ ] **Step 3: Write minimal implementation**

Create `services/skills/arxiv-prep/__init__.py` (empty file).

Create `services/skills/arxiv-prep/arxiv_prep.py`:

```python
"""arxiv-prep skill logic: clean, compile, anonymize, package a LaTeX project.

CRITICAL: never write to stdout. All logging goes to stderr.
"""
from __future__ import annotations

import difflib
import logging
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Callable

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("arxiv-prep")


def _find_main_tex(project_dir: str) -> Path | None:
    """Return the most likely main .tex (one containing \\documentclass)."""
    root = Path(project_dir)
    candidates = sorted(root.rglob("*.tex"))
    for tex in candidates:
        try:
            if "\\documentclass" in tex.read_text(encoding="utf-8", errors="ignore"):
                return tex
        except OSError:
            continue
    return candidates[0] if candidates else None


def clean_source(project_dir: str) -> dict:
    """Run arxiv-latex-cleaner on the project dir."""
    try:
        proc = subprocess.run(
            ["arxiv_latex_cleaner", project_dir],
            capture_output=True, text=True, timeout=300,
        )
        return {"ok": proc.returncode == 0, "log": (proc.stdout + proc.stderr).strip()}
    except FileNotFoundError:
        return {"ok": False, "log": "arxiv_latex_cleaner not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "log": "arxiv_latex_cleaner timed out"}


def verify_compile(project_dir: str) -> dict:
    """Compile the main .tex with tectonic; return ok + parsed error lines."""
    main = _find_main_tex(project_dir)
    if main is None:
        return {"ok": False, "errors": ["no .tex file found"]}
    try:
        proc = subprocess.run(
            ["tectonic", str(main)],
            capture_output=True, text=True, timeout=300, cwd=project_dir,
        )
    except FileNotFoundError:
        return {"ok": False, "errors": ["tectonic binary not found on PATH"]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "errors": ["tectonic compile timed out"]}
    ok = proc.returncode == 0
    errors: list[str] = []
    if not ok:
        for line in (proc.stdout + "\n" + proc.stderr).splitlines():
            low = line.lower()
            if "error" in low or "undefined" in low or "! " in line:
                errors.append(line.strip())
    return {"ok": ok, "errors": errors}


def extract_metadata(project_dir: str) -> dict:
    """Extract title/authors/abstract/category from the main .tex."""
    main = _find_main_tex(project_dir)
    text = main.read_text(encoding="utf-8", errors="ignore") if main else ""

    def _grab(pattern: str) -> str:
        m = re.search(pattern, text, re.DOTALL)
        return m.group(1).strip() if m else ""

    title = _grab(r"\\title\{(.+?)\}")
    authors = _grab(r"\\author\{(.+?)\}")
    abstract = _grab(r"\\begin\{abstract\}(.+?)\\end\{abstract\}")
    category = _grab(r"\\category\{(.+?)\}")
    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "category": category,
    }


def anonymize(project_dir: str, llm: Callable[[str], str] | None = None) -> dict:
    """LLM-assisted anonymization. Returns a unified diff WITHOUT editing files."""
    main = _find_main_tex(project_dir)
    if main is None:
        return {"diff": "", "changes": ["no .tex file found"]}
    original = main.read_text(encoding="utf-8", errors="ignore")
    if llm is None:
        return {"diff": "", "changes": ["no llm provided; anonymization skipped"]}
    prompt = (
        "Anonymize this LaTeX source for double-blind review. Remove \\author, "
        "acknowledgments, and first-person self-citation phrasing. Return ONLY the "
        "edited LaTeX source, no commentary.\n\n" + original
    )
    edited = llm(prompt)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            edited.splitlines(keepends=True),
            fromfile=str(main), tofile=str(main) + " (anonymized)",
        )
    )
    changes = [ln[1:].strip() for ln in diff.splitlines()
               if ln.startswith("-") and not ln.startswith("---") and ln[1:].strip()]
    return {"diff": diff, "changes": changes}


def package_tarball(project_dir: str, output_path: str | None = None) -> dict:
    """Create submission.tar.gz of the project dir."""
    root = Path(project_dir)
    out = Path(output_path) if output_path else root.parent / "submission.tar.gz"
    try:
        with tarfile.open(out, "w:gz") as tar:
            tar.add(root, arcname=root.name)
        return {"ok": True, "path": str(out)}
    except OSError as exc:
        return {"ok": False, "path": str(out), "error": str(exc)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/skills/arxiv-prep/test_arxiv_prep.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/skills/arxiv-prep/__init__.py services/skills/arxiv-prep/arxiv_prep.py tests/services/skills/arxiv-prep/
git commit -m "feat(skills): add arxiv-prep logic module (B1)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Add SKILL.md, server.py, and requirements.txt

**Files:**
- Create: `services/skills/arxiv-prep/SKILL.md`
- Create: `services/skills/arxiv-prep/server.py`
- Create: `services/skills/arxiv-prep/requirements.txt`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/skills/arxiv-prep/test_arxiv_prep.py`:

```python
def test_skill_md_frontmatter_parses():
    import frontmatter
    skill_md = _MODULE_PATH.parent / "SKILL.md"
    assert skill_md.exists()
    meta, _body = frontmatter.parse(skill_md.read_text(encoding="utf-8"))
    assert meta["name"] == "arxiv-prep"
    assert "submission" in meta["description"].lower()


def test_server_lists_all_five_tools():
    import importlib.util
    server_path = _MODULE_PATH.parent / "server.py"
    spec = importlib.util.spec_from_file_location("arxiv_prep_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import asyncio
    tools = asyncio.get_event_loop().run_until_complete(mod.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "clean_source", "verify_compile", "anonymize",
        "package_tarball", "extract_metadata",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/skills/arxiv-prep/test_arxiv_prep.py::test_skill_md_frontmatter_parses tests/services/skills/arxiv-prep/test_arxiv_prep.py::test_server_lists_all_five_tools -v`
Expected: FAIL (files do not exist)

- [ ] **Step 3: Write minimal implementation**

Create `services/skills/arxiv-prep/SKILL.md` (frontmatter pasted EXACTLY as specified):

```markdown
---
name: arxiv-prep
description: >-
  Prepares a finished LaTeX paper for arXiv or conference submission: flattens
  \input/\include, strips comments and unused assets via arxiv-latex-cleaner,
  verifies the source compiles with tectonic, optionally scrubs author identity
  for double-blind review, and emits an upload-ready tarball plus a
  submission-metadata summary. Use when packaging a completed paper for
  submission, anonymizing a manuscript for blind review, or checking that LaTeX
  source compiles cleanly. Distinct from academic-writing (which drafts the
  content) and paper-to-slides (which builds a talk) — this operates on a
  finished .tex project. Deterministic apart from the optional anonymization check.
version: "0.1.0"
license: MIT
requires: []
---

# arxiv-prep Skill

Packages a finished LaTeX project for arXiv or conference submission.

## When to use

- Packaging a completed paper into an upload-ready tarball.
- Anonymizing a manuscript for double-blind review.
- Verifying that LaTeX source compiles cleanly before submission.

## Tools

- `clean_source(project_dir)` — runs `arxiv-latex-cleaner`; returns `{ok, log}`.
- `verify_compile(project_dir)` — compiles the main `.tex` with `tectonic`;
  returns `{ok, errors}`.
- `anonymize(project_dir)` — returns a unified `{diff, changes}` for approval;
  never edits files in place.
- `package_tarball(project_dir, output_path=None)` — writes `submission.tar.gz`;
  returns `{ok, path}`.
- `extract_metadata(project_dir)` — returns `{title, authors, abstract, category}`.

## Constraints

- `tectonic` is an external binary, not a pip dependency.
- Anonymization returns a diff for human approval; it does not mutate the source.
```

Create `services/skills/arxiv-prep/server.py`:

```python
"""MCP server for the arxiv-prep skill (stdio JSON-RPC).

CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
import asyncio
import json
import logging
import sys

import arxiv_prep

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("arxiv-prep.server")
app: Server = Server("arxiv-prep")


@app.list_tools()
async def list_tools() -> list[Tool]:
    proj = {"project_dir": {"type": "string"}}
    return [
        Tool(name="clean_source", description="Run arxiv-latex-cleaner on a project dir.",
             inputSchema={"type": "object", "properties": proj, "required": ["project_dir"]}),
        Tool(name="verify_compile", description="Compile the main .tex with tectonic.",
             inputSchema={"type": "object", "properties": proj, "required": ["project_dir"]}),
        Tool(name="anonymize", description="Return a diff anonymizing the source (no in-place edit).",
             inputSchema={"type": "object", "properties": proj, "required": ["project_dir"]}),
        Tool(name="package_tarball", description="Create submission.tar.gz of the project dir.",
             inputSchema={"type": "object", "properties": {
                 **proj, "output_path": {"type": "string"}}, "required": ["project_dir"]}),
        Tool(name="extract_metadata", description="Extract title/authors/abstract/category.",
             inputSchema={"type": "object", "properties": proj, "required": ["project_dir"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "clean_source":
            result = arxiv_prep.clean_source(arguments["project_dir"])
        elif name == "verify_compile":
            result = arxiv_prep.verify_compile(arguments["project_dir"])
        elif name == "anonymize":
            result = arxiv_prep.anonymize(arguments["project_dir"])
        elif name == "package_tarball":
            result = arxiv_prep.package_tarball(
                arguments["project_dir"], arguments.get("output_path"))
        elif name == "extract_metadata":
            result = arxiv_prep.extract_metadata(arguments["project_dir"])
        else:
            raise ValueError(f"unknown tool: {name}")
        return [TextContent(type="text", text=json.dumps(result, default=str))]
    except Exception as exc:
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


async def main() -> None:
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

Create `services/skills/arxiv-prep/requirements.txt`:

```
mcp>=1.0.0
arxiv-latex-cleaner>=0.2.0
PyYAML>=6.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/skills/arxiv-prep/test_arxiv_prep.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/skills/arxiv-prep/SKILL.md services/skills/arxiv-prep/server.py services/skills/arxiv-prep/requirements.txt
git commit -m "feat(skills): add arxiv-prep SKILL.md + MCP server (B1)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Verify catalog registration

**Files:**
- Modify: `tests/services/skills/arxiv-prep/test_arxiv_prep.py`

The skill needs no code change to register — `SkillRunner.discover()` rglobs every `SKILL.md` under the skills root and indexes by frontmatter `name`. This task adds a guard test proving the runner catalogs `arxiv-prep` and `catalog_prompt()` advertises it.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/skills/arxiv-prep/test_arxiv_prep.py`:

```python
def test_skill_runner_catalogs_arxiv_prep():
    from services.skill_runner.skill_runner import SkillRunner
    skills_root = _MODULE_PATH.resolve().parent.parent  # .../services/skills
    runner = SkillRunner(roots=[skills_root])
    runner.discover()
    assert "arxiv-prep" in runner.catalog
    prompt = runner.catalog_prompt()
    assert "arxiv-prep" in prompt
    assert "submission" in prompt.lower()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/services/skills/arxiv-prep/test_arxiv_prep.py::test_skill_runner_catalogs_arxiv_prep -v`
Expected: PASS (discovery is automatic from SKILL.md frontmatter)

- [ ] **Step 3: Commit**

```bash
git add tests/services/skills/arxiv-prep/test_arxiv_prep.py
git commit -m "test(skills): assert arxiv-prep is cataloged by SkillRunner (B1)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
