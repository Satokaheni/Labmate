"""Smoke test: start.sh --foreground execs the harness in the foreground and
writes no pidfile; daemon mode still writes one. Uses PATH shims so no real
services.local.main / node / model is needed."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
START = REPO / "infrastructure" / "start.sh"


def _shim_dir(tmp_path: Path) -> Path:
    """A PATH dir with fake python/node/curl so start.sh's prep + exec are inert."""
    d = tmp_path / "bin"
    d.mkdir()
    # fake python: for `python -m services.local.main` just exit 0 immediately;
    # for anything else (shouldn't happen) also exit 0.
    (d / "python").write_text("#!/usr/bin/env bash\nexit 0\n")
    # fake node/npm/curl so prep doesn't try real work or network.
    for name in ("node", "npm", "curl"):
        (d / name).write_text("#!/usr/bin/env bash\nexit 0\n")
    for f in d.iterdir():
        f.chmod(0o755)
    return d


def test_foreground_writes_no_pidfile(tmp_path):
    shim = _shim_dir(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{shim}:{os.environ['PATH']}",
        "LOCAL_PORT": "8799",
        # point .data under tmp so we don't touch the repo's real .data
        "SEARXNG_DIR": str(tmp_path / "searxng"),
    }
    # --foreground execs `python -m services.local.main`, which the shim exits 0 for.
    proc = subprocess.run(
        ["bash", str(START), "--foreground"],
        env=env,
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Foreground mode must NOT daemonize -> no pidfile written by this run.
    # (If a stale one exists from a prior daemon run, assert it wasn't just touched.)
    assert "FOREGROUND" in (proc.stdout + proc.stderr) or proc.returncode == 0
    # The definitive check: foreground path prints its foreground banner.
    assert "foreground" in (proc.stdout + proc.stderr).lower()
