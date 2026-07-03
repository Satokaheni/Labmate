"""Single source of truth for the ``LABMATE_LOCAL_MODE`` strangler flag.

Piece 0 of the local-harness re-architecture
(docs/superpowers/specs/2026-07-03-local-harness-rearchitecture-design.md).

When ON, Labmate runs as a self-contained LOCAL harness (SQLite state,
in-process events, direct local tools) instead of the pod-hosted service
(networked Redis/Mongo/Chroma + tool-delegation). Piece 0 only READS and
logs this flag; each later migration piece adds its own ``if
local_mode_enabled():`` branch at its own insertion point (see
docs/superpowers/specs/2026-07-03-local-harness-seam-map.md).

Default OFF (pod mode). Read at call time so tests can flip it per-test;
mirrors ``message_repair_enabled()`` / ``conditional_gates_enabled()``.
"""

from __future__ import annotations

import os
from pathlib import Path

_FALSEY = {"0", "false", "no", "off", ""}


def local_mode_enabled() -> bool:
    """True when ``LABMATE_LOCAL_MODE`` is set to any non-falsey value.

    Default OFF. Falsey set (case-insensitive, whitespace-stripped):
    ``{"0", "false", "no", "off", ""}``.
    """
    return os.getenv("LABMATE_LOCAL_MODE", "0").strip().lower() not in _FALSEY


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
