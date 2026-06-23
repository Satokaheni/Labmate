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
