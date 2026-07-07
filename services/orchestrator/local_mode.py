"""Local state path helpers for the local-only harness.

Resolves where Labmate keeps its per-user local state (SQLite checkpoint DB
and related local files). experimental is local-mode only, so there is no
flag to read here anymore — these helpers are used unconditionally by
``services/orchestrator/graph.py`` to locate the SqliteSaver DB.
"""

from __future__ import annotations

import os
from pathlib import Path


def local_state_dir() -> Path:
    """Directory holding the per-user local state (SQLite DB + local files).

    ``LABMATE_STATE_DIR`` (default ``.data`` — matches the ``CURATOR_STATE_DIR``
    convention). Read at call time. Relative paths resolve against the process
    CWD, as the rest of the ``.data`` usage does.
    """
    return Path(os.getenv("LABMATE_STATE_DIR", ".data"))


def local_state_db_path() -> Path:
    """Path to the local SQLite state DB (LangGraph checkpoints; later: sessions).

    ``LABMATE_STATE_DB`` (a full file path) overrides everything; otherwise
    ``local_state_dir() / "labmate_state.sqlite"``. An empty ``LABMATE_STATE_DB``
    is treated as unset. Read at call time.
    """
    override = os.getenv("LABMATE_STATE_DB")
    if override:
        return Path(override)
    return local_state_dir() / "labmate_state.sqlite"
