from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter  # python-frontmatter; yaml.safe_load by default

# CRITICAL: never write to stdout. All logging goes to stderr via handlers
# configured by the host process. This module only acquires a named logger.
log = logging.getLogger("skill_runner")


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    path: Path                       # resolved, confined path to SKILL.md
    tier: str                        # 'project' | 'personal' | 'bundled'
    frontmatter: dict[str, Any] = field(default_factory=dict)


class SkillRunner:
    """Discovers, catalogs, and lazily activates markdown skills.

    Catalog (frontmatter only) is built eagerly at startup.
    Skill bodies load lazily on an LLM-issued load_skill(name) tool call.

    CRITICAL: SkillRunner itself must never write to stdout.
    All logging goes to sys.stderr via the host's configured handlers.
    """

    TIER_NAMES = ["project", "personal", "bundled"]

    def __init__(self, roots: list[Path], max_chain: int = 8) -> None:
        # roots ordered HIGHEST precedence first: project, personal, bundled
        self.roots: list[Path] = [Path(r).expanduser().resolve() for r in roots]
        self.catalog: dict[str, SkillMeta] = {}
        self.loaded: dict[str, str] = {}     # name -> body (activation cache)
        self.max_chain = max_chain
        self._activations = 0

    # ---------- STAGE 1: discovery (frontmatter only) ----------

    def discover(self) -> None:
        """Scan all roots, parse frontmatter only, build catalog.

        Skips SKILL.md files under node_modules or .git/dist to avoid
        catalog pollution from vendored dependencies.
        """
        self.catalog.clear()
        for tier, root in zip(self.TIER_NAMES, self.roots):
            if not root.is_dir():
                continue
            for skill_md in sorted(root.rglob("SKILL.md")):
                # Skip vendored paths (node_modules, .git, dist)
                parts = skill_md.parts
                if any(p in ("node_modules", ".git", "dist") for p in parts):
                    continue
                self._index(skill_md, tier, root)
        log.info("cataloged %d skills", len(self.catalog))  # -> stderr

    def _index(self, skill_md: Path, tier: str, root: Path) -> None:
        real = skill_md.resolve()
        if not self._within(real, root):             # symlink-escape guard
            log.warning("skipping out-of-root skill: %s", skill_md)
            return
        try:
            meta, _body = frontmatter.parse(real.read_text(encoding="utf-8"))
        except Exception as exc:                      # malformed YAML or IO error
            log.warning("bad frontmatter in %s: %s", real, exc)
            return
        name = meta.get("name")
        desc = meta.get("description")
        if not name or not desc:
            log.warning("skill %s missing required name/description, skipping", real)
            return
        if name in self.catalog:
            log.warning(
                "skill name '%s' shadowed: %s overrides %s",
                name, self.catalog[name].path, real,
            )
            return
        self.catalog[name] = SkillMeta(name, desc, real, tier, dict(meta))

    def reset_activations(self) -> None:
        """Reset the per-task activation counter.

        max_chain bounds how many skills may be auto-loaded within a SINGLE task
        (the requires-chain guard). The counter must be reset at each task
        boundary — otherwise it accumulates across the process lifetime and, after
        max_chain total loads, every load_skill fails with 'activation limit
        reached', silently breaking skill routing for all later tasks.
        """
        self._activations = 0

    def reload_catalog(self) -> None:
        """Re-run discovery. Safe to call on a filesystem change event.

        Preserves the activation cache (self.loaded) and the activation counter;
        only the metadata catalog is rebuilt.
        """
        self.discover()

    # ---------- STAGE 2: catalog -> system prompt + tool schema ----------

    def catalog_prompt(self) -> str:
        lines = ["Available skills (call load_skill(name) to activate one):"]
        for m in sorted(self.catalog.values(), key=lambda s: s.name):
            lines.append(f"- {m.name}: {m.description}")
        return "\n".join(lines)

    def tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": "Load the full instructions for a named skill.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": sorted(self.catalog),
                        }
                    },
                    "required": ["name"],
                },
            },
        }

    # ---------- STAGE 3: lazy activation ----------

    def load_skill(self, name: str) -> dict[str, Any]:
        self._activations += 1
        if self._activations > self.max_chain:
            return self._err("skill activation limit reached")
        meta = self.catalog.get(name)
        if meta is None:
            return self._err(
                f"unknown skill: {name}",
                available=sorted(self.catalog),
            )
        if name in self.loaded:
            return {"name": "load_skill",
                    "response": {"status": "already_loaded", "name": name}}
        # Re-validate confinement after any potential filesystem change.
        if not self._within(meta.path, *self.roots):
            return self._err(f"path confinement violation for skill: {name}")
        _meta, body = frontmatter.parse(meta.path.read_text(encoding="utf-8"))
        self.loaded[name] = body
        return {"name": "load_skill",
                "response": {"status": "loaded", "name": name, "body": body}}

    def dispatch(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """Entry point for model-issued tool calls."""
        if tool_call.get("name") != "load_skill":
            return self._err(f"unknown tool: {tool_call.get('name')}")
        args = tool_call.get("arguments") or tool_call.get("parameters") or {}
        if isinstance(args, str):
            args = json.loads(args)
        return self.load_skill(args.get("name", ""))

    # ---------- helpers ----------

    @staticmethod
    def _within(path: Path, *roots: Path) -> bool:
        real = path.resolve()
        return any(real.is_relative_to(r.resolve()) for r in roots)

    @staticmethod
    def _err(msg: str, **extra: Any) -> dict[str, Any]:
        return {"name": "load_skill",
                "response": {"status": "error", "message": msg, **extra}}
