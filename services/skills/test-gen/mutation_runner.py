"""Wraps mutmut and parses its output into a structured MutationResult.

CRITICAL: stdout is the JSON-RPC 2.0 channel for the parent server. NEVER
print(). subprocess/mutmut output is captured into strings, never inherited.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("test-gen.mutation")


@dataclass
class MutationResult:
    mutation_score: float          # killed / total (0.0 when total == 0)
    surviving_mutants: list[str]   # unified diffs of surviving mutants
    killed_count: int
    total_count: int
    raw_output: str

    def to_dict(self) -> dict:
        return {
            "mutation_score": self.mutation_score,
            "surviving_mutants": self.surviving_mutants,
            "killed_count": self.killed_count,
            "total_count": self.total_count,
            "raw_output": self.raw_output,
        }


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


# A CommandRunner takes (argv, cwd, timeout) and returns a CommandResult.
CommandRunner = Callable[[list[str], str | None, int], CommandResult]


def _subprocess_runner(argv: list[str], cwd: str | None, timeout: int) -> CommandResult:
    """Default local runner. Production wraps this with the code-sandbox skill."""
    proc = subprocess.run(
        argv,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,   # NEVER inherit stdout — it would corrupt JSON-RPC
        text=True,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def run_pytest(
    test_file: str,
    cwd: str | None = None,
    timeout: int = 120,
    runner: CommandRunner | None = None,
) -> dict:
    """Re-run an EXISTING pytest suite without regenerating anything.

    Use this when the test file already exists and the source under test has not
    changed — there is no need to call `generate`/`improve` again, just execute
    the suite. Returns a structured pass/fail result.
    """
    run_cmd = runner or _subprocess_runner
    argv = [sys.executable, "-m", "pytest", test_file, "-q", "--no-header"]
    try:
        res = run_cmd(argv, cwd, timeout)
    except subprocess.TimeoutExpired:
        log.error("pytest timed out after %ss on %s", timeout, test_file)
        return {
            "passed": False,
            "returncode": -1,
            "passed_count": 0,
            "failed_count": 0,
            "summary": f"pytest timed out after {timeout}s",
            "raw_output": "",
        }
    except FileNotFoundError as exc:
        log.error("pytest not runnable: %r", exc)
        return {
            "passed": False,
            "returncode": -1,
            "passed_count": 0,
            "failed_count": 0,
            "summary": f"could not run pytest: {exc}",
            "raw_output": "",
        }

    output = (res.stdout or "") + "\n" + (res.stderr or "")

    def _count(pat: str) -> int:
        m = re.search(pat, output)
        return int(m.group(1)) if m else 0

    passed_count = _count(r"(\d+)\s+passed")
    failed_count = _count(r"(\d+)\s+failed") + _count(r"(\d+)\s+error")
    # pytest exit code 0 == all passed (and at least one test collected)
    passed = res.returncode == 0 and failed_count == 0
    # Last non-empty line is pytest's summary, e.g. "3 passed in 0.04s".
    lines = [ln for ln in output.splitlines() if ln.strip()]
    summary = lines[-1].strip() if lines else "(no output)"
    log.info("pytest %s -> rc=%s passed=%d failed=%d", test_file, res.returncode, passed_count, failed_count)
    return {
        "passed": passed,
        "returncode": res.returncode,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "summary": summary,
        "raw_output": output[-4000:],
    }


class MutationRunner:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._run_cmd: CommandRunner = runner or _subprocess_runner

    def run(self, source_file: str, test_file: str, timeout: int = 120) -> MutationResult:
        log.info("running mutmut on %s with %s", source_file, test_file)

        # mutmut 3.x is driven by config (setup.cfg/pyproject) for paths_to_mutate
        # and the test runner. The runner here passes them explicitly; the
        # code-sandbox dispatcher in production writes a transient config.
        argv = [
            "mutmut", "run",
            "--paths-to-mutate", source_file,
            "--tests-dir", test_file,
            "--no-progress",
        ]
        try:
            res = self._run_cmd(argv, None, timeout)
        except FileNotFoundError as exc:
            log.error("mutmut not found: %r", exc)
            raise
        except subprocess.TimeoutExpired:
            log.error("mutmut timed out after %ss", timeout)
            raise

        # Exit code 1/2 == mutants survived; 0 == all killed. Both are success.
        log.info("mutmut exit=%s", res.returncode)
        result = self._parse_mutmut_output(res.stdout + "\n" + res.stderr)
        result.surviving_mutants = self._get_surviving_diffs(source_file)
        return result

    def _parse_mutmut_output(self, output: str) -> MutationResult:
        def _count(*patterns: str) -> int:
            for pat in patterns:
                m = re.search(pat, output, re.IGNORECASE)
                if m:
                    return int(m.group(1))
            return 0

        # mutmut 3.x summary, e.g. "🎉 18  🙁 4  ⏰ 0  🤔 0"
        # and/or labelled lines "killed: 18", "survived: 4".
        killed = _count(r"killed[:\s]+(\d+)", r"🎉\s*(\d+)")
        survived = _count(r"survived[:\s]+(\d+)", r"🙁\s*(\d+)")
        timeout = _count(r"timeout[:\s]+(\d+)", r"⏰\s*(\d+)")
        suspicious = _count(r"suspicious[:\s]+(\d+)", r"🤔\s*(\d+)")

        total = killed + survived + timeout + suspicious
        score = (killed / total) if total else 0.0
        log.info(
            "parsed: killed=%d survived=%d timeout=%d suspicious=%d total=%d score=%.3f",
            killed, survived, timeout, suspicious, total, score,
        )
        return MutationResult(
            mutation_score=round(score, 4),
            surviving_mutants=[],   # filled by _get_surviving_diffs in run()
            killed_count=killed,
            total_count=total,
            raw_output=output,
        )

    def _get_surviving_diffs(self, source_file: str) -> list[str]:
        try:
            listing = self._run_cmd(["mutmut", "results"], None, 30)
        except Exception as exc:  # noqa: BLE001 — diffs are best-effort
            log.warning("could not list mutmut results: %r", exc)
            return []

        # "Survived" section lists ids/ranges, e.g. "src.calc.x_1: survived" or
        # a "Survived 🙁 (4)" block followed by ids like "1-4" or "1, 2, 3".
        ids: list[str] = []
        in_survived = False
        for line in listing.stdout.splitlines():
            low = line.lower()
            if "survived" in low:
                in_survived = True
                ids += re.findall(r"\b([\w.]+_\d+|\d+)\b", line)
                continue
            if in_survived and (not line.strip() or ":" in low and "survived" not in low):
                in_survived = False
            if in_survived:
                ids += re.findall(r"\b([\w.]+_\d+|\d+)\b", line)

        diffs: list[str] = []
        for mid in dict.fromkeys(ids):  # dedupe, preserve order
            try:
                shown = self._run_cmd(["mutmut", "show", mid], None, 30)
                if shown.stdout.strip():
                    diffs.append(shown.stdout)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not show mutant %s: %r", mid, exc)
        log.info("collected %d surviving diffs", len(diffs))
        return diffs
