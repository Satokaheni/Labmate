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

_FALSEY = {"0", "false", "no", "off", ""}


def local_mode_enabled() -> bool:
    """True when ``LABMATE_LOCAL_MODE`` is set to any non-falsey value.

    Default OFF. Falsey set (case-insensitive, whitespace-stripped):
    ``{"0", "false", "no", "off", ""}``.
    """
    return os.getenv("LABMATE_LOCAL_MODE", "0").strip().lower() not in _FALSEY
