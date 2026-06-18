"""CompileLoop: tectonic compile + Gemma 4 .tex self-correction (max 5 retries)."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import litellm

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("paper-to-slides.compile")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")


@dataclass
class CompileResult:
    success: bool
    pdf_path: str | None
    attempts: int
    final_error: str | None

    def to_dict(self) -> dict:
        return {"success": self.success, "pdf_path": self.pdf_path,
                "attempts": self.attempts, "final_error": self.final_error}


class CompileLoop:
    MAX_RETRIES: int = 5

    def _run_tectonic(self, tex_path: str) -> tuple[bool, str]:
        """Compile tex_path. Prefer tectonic; fall back to pdflatex. Returns (ok, log)."""
        path = Path(tex_path)
        if shutil.which("tectonic"):
            cmd = ["tectonic", "--keep-logs", "--outdir", str(path.parent), str(path)]
        elif shutil.which("pdflatex"):
            cmd = ["pdflatex", "-interaction=nonstopmode",
                   "-output-directory", str(path.parent), str(path)]
        else:
            return False, "neither 'tectonic' nor 'pdflatex' found on PATH"
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return False, "compile timed out after 120s"
        ok = proc.returncode == 0
        return ok, (proc.stdout + "\n" + proc.stderr)

    REPAIR_PROMPT = """The following LaTeX Beamer document failed to compile. \
Fix the errors and return ONLY the complete corrected .tex source (no commentary, \
no code fences).

COMPILE LOG (tail):
{log}

CURRENT .tex:
{tex}
"""

    def _repair_tex(self, tex: str, error_log: str) -> str:
        resp = litellm.completion(
            model=f"openai/{GEMMA_MODEL}",
            api_base=GEMMA_BASE,
            api_key="not-needed",
            temperature=0.0,
            messages=[{"role": "user", "content": self.REPAIR_PROMPT.format(
                log=error_log[-4000:], tex=tex)}],
        )
        out = resp["choices"][0]["message"]["content"].strip()
        if out.startswith("```"):
            out = out.split("```", 2)[1]
            if out.startswith("latex"):
                out = out[5:]
            elif out.startswith("tex"):
                out = out[3:]
        return out.strip()

    _NO_ENGINE_MSG = "neither 'tectonic' nor 'pdflatex' found on PATH"

    def compile(self, tex_path: str, max_retries: int = 5) -> CompileResult:
        path = Path(tex_path)
        pdf_path = path.with_suffix(".pdf")
        last_log = ""
        # Bail early if no LaTeX engine is available — no point retrying.
        if not shutil.which("tectonic") and not shutil.which("pdflatex"):
            return CompileResult(False, None, 0, self._NO_ENGINE_MSG)
        for attempt in range(1, max_retries + 1):
            ok, last_log = self._run_tectonic(str(path))
            if ok and pdf_path.exists():
                log.info("compile succeeded on attempt %d", attempt)
                return CompileResult(True, str(pdf_path), attempt, None)
            log.warning("compile attempt %d failed; repairing", attempt)
            if attempt < max_retries:
                repaired = self._repair_tex(path.read_text(), last_log)
                path.write_text(repaired)
        return CompileResult(False, None, max_retries, last_log[-2000:])
