from __future__ import annotations

import sys
from pathlib import Path

from services.skill_runner.skill_registry import SkillManifest

# Default location: resolve relative to this file so it works from any cwd.
_DEFAULT_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


def discover_manifests(skills_root: Path | str | None = None) -> list[SkillManifest]:
    """Return one SkillManifest per skill that has a runnable MCP server.

    A skill is runnable if it contains server.py (Python) or dist/index.js
    (TypeScript, pre-compiled). Instruction-only skills (SKILL.md only) are
    silently skipped — they are activated by the SkillRunner, not the worker.

    When both server.py and dist/index.js exist, server.py wins.
    Results are sorted by directory name for deterministic registration order.
    """
    root = Path(skills_root).resolve() if skills_root else _DEFAULT_SKILLS_ROOT
    manifests: list[SkillManifest] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        server_py = d / "server.py"
        index_js = d / "dist" / "index.js"
        if server_py.exists():
            manifests.append(
                SkillManifest(
                    name=d.name,
                    command=sys.executable,
                    args=[str(server_py)],
                )
            )
        elif index_js.exists():
            manifests.append(
                SkillManifest(
                    name=d.name,
                    command="node",
                    args=[str(index_js)],
                )
            )
    return manifests
