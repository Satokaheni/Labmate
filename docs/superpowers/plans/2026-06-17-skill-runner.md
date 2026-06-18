# SkillRunner + SkillRegistry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the SkillRunner (SKILL.md discovery + lazy body activation) and SkillRegistry (MCP subprocess lifecycle management) for Labmate's skills layer.

**Architecture:** SkillRunner scans layered roots for SKILL.md frontmatter at startup, building a compact catalog for the system prompt; bodies load lazily on `load_skill(name)` tool calls. SkillRegistry spawns long-lived child processes per skill, maintains the MCP initialize handshake, validates tool calls against cached JSON schemas before dispatching, and supervises for crashes with exponential-backoff restarts.

**Tech Stack:** Python 3.11+, `python-frontmatter`, `mcp` SDK (anyio), `jsonschema`, `asyncio.TaskGroup`, `watchfiles` (optional hot-reload), `pytest-asyncio`

---

## Conventions used throughout this plan

- All paths are absolute under the repo root `/Users/zachstallbohm/Work/gemma`.
- Every logger in production code is `logging.getLogger(...)`; tests configure the root logger to `sys.stderr`. **No `print()` anywhere.**
- Frontmatter is parsed exclusively via `frontmatter.parse(...)` from `python-frontmatter`, which uses `yaml.safe_load` internally. Never override the loader.
- TDD cycle for every code step: write failing test → run it (observe the named failure) → implement → run again (observe PASS) → commit.
- Run a single test with: `cd /Users/zachstallbohm/Work/gemma && python -m pytest tests/services/skill_runner/<file>::<test> -x -q`

---

## Phase 0 — Scaffolding

### Task 0.1 — Create directory layout

- [ ] Create the package and test directories.

```bash
cd /Users/zachstallbohm/Work/gemma
mkdir -p services/skill_runner
mkdir -p tests/services/skill_runner
touch services/skill_runner/__init__.py
```

### Task 0.2 — Write `services/skill_runner/requirements.txt`

- [ ] Create `/Users/zachstallbohm/Work/gemma/services/skill_runner/requirements.txt`:

```
python-frontmatter>=1.1.0
PyYAML>=6.0
jsonschema
mcp
anyio
watchfiles
```

### Task 0.3 — Install dependencies

- [ ] Install into the project environment.

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pip install -r services/skill_runner/requirements.txt
python -m pip install pytest pytest-asyncio
```

### Task 0.4 — Write `tests/services/skill_runner/conftest.py`

- [ ] Create `/Users/zachstallbohm/Work/gemma/tests/services/skill_runner/conftest.py`:

```python
import logging
import sys

import pytest


def pytest_configure(config):
    # Register the markers used across this suite.
    config.addinivalue_line("markers", "mocked: runs without real subprocesses or GPU")
    config.addinivalue_line("markers", "live: requires a real subprocess / inference server")


@pytest.fixture(autouse=True)
def _log_to_stderr():
    """Ensure any log output during tests goes to stderr, never stdout."""
    handler = logging.StreamHandler(sys.stderr)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield
    root.removeHandler(handler)


@pytest.fixture
def write_skill(tmp_path):
    """Helper: write a SKILL.md file at <root>/<subdir>/SKILL.md with the given text."""
    def _write(root_name: str, subdir: str, text: str):
        from pathlib import Path

        root = tmp_path / root_name
        skill_dir = root / subdir
        skill_dir.mkdir(parents=True, exist_ok=True)
        md = skill_dir / "SKILL.md"
        md.write_text(text, encoding="utf-8")
        return root, md

    return _write
```

### Task 0.5 — Add `pytest.ini` asyncio config (if not present)

- [ ] Ensure `/Users/zachstallbohm/Work/gemma/pytest.ini` contains asyncio mode. If the file exists, only add the missing keys; if not, create it:

```ini
[pytest]
asyncio_mode = auto
markers =
    mocked: runs without real subprocesses or GPU
    live: requires a real subprocess / inference server
```

### Task 0.6 — Commit scaffolding

- [ ] Commit.

```bash
cd /Users/zachstallbohm/Work/gemma
git add services/skill_runner/requirements.txt tests/services/skill_runner/conftest.py pytest.ini
git commit -m "scaffold skill_runner package and test harness"
```

---

## Phase 1 — SkillRunner: data model + discovery (Stage 1)

### Task 1.1 — Failing test: discovery parses frontmatter only and builds catalog

- [ ] Create `/Users/zachstallbohm/Work/gemma/tests/services/skill_runner/test_skill_runner.py` with the first test:

```python
import logging
import sys
from pathlib import Path

import pytest

# services/ is a proper package; add it to sys.path so plain imports work.
_SERVICES = str(Path(__file__).resolve().parents[3] / "services")
if _SERVICES not in sys.path:
    sys.path.insert(0, _SERVICES)

from skill_runner.skill_runner import SkillRunner, SkillMeta


VALID_SKILL = """---
name: {name}
description: {desc}
---

# {name} body
This is the body of {name}.
"""


@pytest.mark.mocked
def test_discover_parses_frontmatter_only(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    pers_root, _ = write_skill("personal", "web-search", VALID_SKILL.format(name="web-search", desc="Searches the web."))

    runner = SkillRunner(roots=[proj_root, pers_root, tmp_path / "bundled"])
    runner.discover()

    assert set(runner.catalog) == {"deploy", "web-search"}
    deploy = runner.catalog["deploy"]
    assert isinstance(deploy, SkillMeta)
    assert deploy.name == "deploy"
    assert deploy.description == "Deploys things."
    assert deploy.tier == "project"
    assert deploy.path.name == "SKILL.md"
    # No body has been read into the activation cache.
    assert runner.loaded == {}
```

### Task 1.2 — Run the test, observe ImportError / missing module

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py -x -q
```

Expected failure: `FileNotFoundError`/`ModuleNotFoundError` because `services/skill_runner/skill_runner.py` does not exist yet.

### Task 1.3 — Implement `SkillMeta` + `SkillRunner.discover` + `_index` + `_within`

- [ ] Create `/Users/zachstallbohm/Work/gemma/services/skill_runner/skill_runner.py`:

```python
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
        """Scan all roots, parse frontmatter only, build catalog."""
        self.catalog.clear()
        for tier, root in zip(self.TIER_NAMES, self.roots):
            if not root.is_dir():
                continue
            for skill_md in sorted(root.rglob("SKILL.md")):
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

    # ---------- helpers ----------

    @staticmethod
    def _within(path: Path, *roots: Path) -> bool:
        real = path.resolve()
        return any(real.is_relative_to(r.resolve()) for r in roots)

    @staticmethod
    def _err(msg: str, **extra: Any) -> dict[str, Any]:
        return {"name": "load_skill",
                "response": {"status": "error", "message": msg, **extra}}
```

### Task 1.4 — Run the test, observe PASS

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py -x -q
```

Expected: 1 passed.

### Task 1.5 — Commit

- [ ] Commit.

```bash
cd /Users/zachstallbohm/Work/gemma
git add services/skill_runner/skill_runner.py tests/services/skill_runner/test_skill_runner.py
git commit -m "SkillRunner: SkillMeta + frontmatter-only discovery"
```

---

## Phase 2 — SkillRunner discovery edge cases

### Task 2.1 — Failing test: malformed frontmatter is skipped and warns to stderr

- [ ] Append to `/Users/zachstallbohm/Work/gemma/tests/services/skill_runner/test_skill_runner.py`:

```python
MISSING_DESC = """---
name: broken
---

# broken body
"""


@pytest.mark.mocked
def test_malformed_frontmatter_skipped_and_warns(write_skill, tmp_path, caplog):
    proj_root, bad_md = write_skill("project", "broken", MISSING_DESC)
    write_skill("project", "ok", VALID_SKILL.format(name="ok", desc="Fine skill."))

    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    with caplog.at_level(logging.WARNING, logger="skill_runner"):
        runner.discover()

    assert "broken" not in runner.catalog        # excluded
    assert "ok" in runner.catalog                # other skills still discovered
    # A warning naming the offending file path was logged.
    assert any(str(bad_md.resolve()) in rec.getMessage() for rec in caplog.records)
```

### Task 2.2 — Run, observe PASS (already implemented in `_index`)

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py::test_malformed_frontmatter_skipped_and_warns -x -q
```

Expected: passes, because `_index` already validates `name`/`description` and logs the path. If it fails, fix `_index` before continuing.

### Task 2.3 — Failing test: project tier overrides personal tier on name collision

- [ ] Append:

```python
@pytest.mark.mocked
def test_project_tier_overrides_personal_on_collision(tmp_path, caplog):
    proj_root = tmp_path / "project"
    pers_root = tmp_path / "personal"
    (proj_root / "deploy").mkdir(parents=True)
    (pers_root / "deploy").mkdir(parents=True)
    proj_md = proj_root / "deploy" / "SKILL.md"
    pers_md = pers_root / "deploy" / "SKILL.md"
    proj_md.write_text(VALID_SKILL.format(name="deploy", desc="Project deploy."), encoding="utf-8")
    pers_md.write_text(VALID_SKILL.format(name="deploy", desc="Personal deploy."), encoding="utf-8")

    runner = SkillRunner(roots=[proj_root, pers_root, tmp_path / "bundled"])
    with caplog.at_level(logging.WARNING, logger="skill_runner"):
        runner.discover()

    entry = runner.catalog["deploy"]
    assert entry.path == proj_md.resolve()       # project wins
    assert entry.tier == "project"
    # Shadowing warning identifies the overridden personal path.
    assert any(str(pers_md.resolve()) in rec.getMessage() for rec in caplog.records)
```

### Task 2.4 — Run, observe PASS

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py::test_project_tier_overrides_personal_on_collision -x -q
```

Expected: passes (first-seen wins because roots are scanned highest-priority first). If it fails, confirm root ordering in `discover`.

### Task 2.5 — Failing test: yaml.safe_load blocks deserialization attack

- [ ] Append:

```python
MALICIOUS_YAML = """---
name: evil
description: !!python/object/apply:os.system ["echo pwned"]
---

# evil body
"""


@pytest.mark.mocked
def test_safe_loader_blocks_yaml_object_injection(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "evil", MALICIOUS_YAML)

    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    # Must not execute os.system; either skipped (handled error) or plain data.
    runner.discover()

    # The malicious skill is not present as an executable object; if it parsed at
    # all, description is a string, never the result of os.system.
    if "evil" in runner.catalog:
        assert isinstance(runner.catalog["evil"].description, str)
```

### Task 2.6 — Run, observe PASS

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py::test_safe_loader_blocks_yaml_object_injection -x -q
```

Expected: passes. `frontmatter.parse` uses `yaml.safe_load`, which raises on the `!!python/object` tag; `_index` catches and skips it.

### Task 2.7 — Commit

- [ ] Commit.

```bash
cd /Users/zachstallbohm/Work/gemma
git add tests/services/skill_runner/test_skill_runner.py
git commit -m "SkillRunner: discovery edge cases (malformed, shadowing, safe yaml)"
```

---

## Phase 3 — SkillRunner: catalog injection + tool schema (Stage 2)

### Task 3.1 — Failing test: catalog_prompt renders compact block

- [ ] Append:

```python
@pytest.mark.mocked
def test_catalog_prompt_renders_sorted_compact_block(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    write_skill("project", "alpha", VALID_SKILL.format(name="alpha", desc="Alpha skill."))

    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()
    prompt = runner.catalog_prompt()

    lines = prompt.splitlines()
    assert lines[0] == "Available skills (call load_skill(name) to activate one):"
    assert lines[1] == "- alpha: Alpha skill."       # sorted by name
    assert lines[2] == "- deploy: Deploys things."
```

### Task 3.2 — Failing test: tool_schema exposes load_skill with enum of names

- [ ] Append:

```python
@pytest.mark.mocked
def test_tool_schema_exposes_load_skill_with_enum(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    write_skill("project", "alpha", VALID_SKILL.format(name="alpha", desc="Alpha skill."))

    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()
    schema = runner.tool_schema()

    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "load_skill"
    props = fn["parameters"]["properties"]
    assert props["name"]["enum"] == ["alpha", "deploy"]   # sorted
    assert fn["parameters"]["required"] == ["name"]
```

### Task 3.3 — Run both, observe AttributeError

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py -k "catalog_prompt or tool_schema" -x -q
```

Expected failure: `AttributeError: 'SkillRunner' object has no attribute 'catalog_prompt'`.

### Task 3.4 — Implement `catalog_prompt` and `tool_schema`

- [ ] In `/Users/zachstallbohm/Work/gemma/services/skill_runner/skill_runner.py`, add these methods to `SkillRunner` after `discover`/`_index` and before the helpers section:

```python
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
```

### Task 3.5 — Run both, observe PASS

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py -k "catalog_prompt or tool_schema" -x -q
```

Expected: 2 passed.

### Task 3.6 — Commit

- [ ] Commit.

```bash
cd /Users/zachstallbohm/Work/gemma
git add services/skill_runner/skill_runner.py tests/services/skill_runner/test_skill_runner.py
git commit -m "SkillRunner: catalog_prompt + load_skill tool schema"
```

---

## Phase 4 — SkillRunner: lazy activation (Stage 3)

### Task 4.1 — Failing test: load_skill returns body, dedup returns already_loaded, unknown returns error

- [ ] Append:

```python
@pytest.mark.mocked
def test_load_skill_returns_body(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()

    result = runner.load_skill("deploy")
    assert result["name"] == "load_skill"
    assert result["response"]["status"] == "loaded"
    assert result["response"]["name"] == "deploy"
    assert "body of deploy" in result["response"]["body"]
    assert "deploy" in runner.loaded


@pytest.mark.mocked
def test_load_skill_dedup_returns_already_loaded(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()

    runner.load_skill("deploy")
    second = runner.load_skill("deploy")
    assert second["response"]["status"] == "already_loaded"
    assert "body" not in second["response"]      # not re-appended


@pytest.mark.mocked
def test_load_skill_unknown_returns_error_with_available(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()

    result = runner.load_skill("does-not-exist")
    assert result["response"]["status"] == "error"
    assert "unknown skill: does-not-exist" in result["response"]["message"]
    assert result["response"]["available"] == ["deploy"]
```

### Task 4.2 — Failing test: chain limit blocks further activations

- [ ] Append:

```python
@pytest.mark.mocked
def test_chain_limit_blocks_further_activations(tmp_path):
    proj_root = tmp_path / "project"
    for n in ("a", "b", "c", "d"):
        d = proj_root / n
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            VALID_SKILL.format(name=n, desc=f"Skill {n}."), encoding="utf-8"
        )

    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"], max_chain=3)
    runner.discover()

    assert runner.load_skill("a")["response"]["status"] == "loaded"
    assert runner.load_skill("b")["response"]["status"] == "loaded"
    assert runner.load_skill("c")["response"]["status"] == "loaded"
    fourth = runner.load_skill("d")
    assert fourth["response"]["status"] == "error"
    assert "skill activation limit reached" in fourth["response"]["message"]
    assert "d" not in runner.loaded
```

### Task 4.3 — Failing test: path confinement rejects traversal, and dispatch entry point

- [ ] Append:

```python
@pytest.mark.mocked
def test_path_confinement_rejects_after_fs_tamper(write_skill, tmp_path, monkeypatch):
    proj_root, md = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()

    # Simulate the catalog path being repointed outside all roots after discovery.
    outside = tmp_path / "outside" / "SKILL.md"
    outside.parent.mkdir(parents=True)
    outside.write_text(VALID_SKILL.format(name="deploy", desc="x"), encoding="utf-8")
    runner.catalog["deploy"] = skill_runner.SkillMeta(
        "deploy", "Deploys things.", outside.resolve(), "project", {}
    )

    result = runner.load_skill("deploy")
    assert result["response"]["status"] == "error"
    assert "path confinement violation" in result["response"]["message"]


@pytest.mark.mocked
def test_dispatch_rejects_unknown_tool_and_routes_load_skill(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()

    bad = runner.dispatch({"name": "not_load_skill", "arguments": {}})
    assert bad["response"]["status"] == "error"
    assert "unknown tool" in bad["response"]["message"]

    # arguments may arrive as a JSON string.
    ok = runner.dispatch({"name": "load_skill", "arguments": '{"name": "deploy"}'})
    assert ok["response"]["status"] == "loaded"
```

> Note on the directory-traversal BDD scenario (`name: "../../../../etc/passwd"`): because `tool_schema` constrains `name` to a closed `enum` of catalog keys and `load_skill` looks the name up in `self.catalog`, a traversal string is simply an unknown skill and returns `unknown skill: ...` before any file access. The `_within` re-check in Task 4.4 is the defense-in-depth layer for the case where a catalog entry's path is tampered with after discovery (covered by `test_path_confinement_rejects_after_fs_tamper`).

### Task 4.4 — Run the Phase 4 tests, observe AttributeError

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py -k "load_skill or chain_limit or confinement or dispatch" -x -q
```

Expected failure: `AttributeError: 'SkillRunner' object has no attribute 'load_skill'`.

### Task 4.5 — Implement `load_skill` and `dispatch`

- [ ] In `/Users/zachstallbohm/Work/gemma/services/skill_runner/skill_runner.py`, add to `SkillRunner` after the Stage 2 methods:

```python
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
```

### Task 4.6 — Run the Phase 4 tests, observe PASS

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py -k "load_skill or chain_limit or confinement or dispatch" -x -q
```

Expected: all passed.

> Note on activation counting: `_activations` increments on every `load_skill` call including the dedup and unknown paths, matching the spec stub. The chain-limit test uses three distinct successful loads then a fourth, so the cap is hit deterministically.

### Task 4.7 — Run the whole SkillRunner suite

- [ ] Run the full file to confirm no regressions.

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py -q
```

Expected: all SkillRunner tests pass.

### Task 4.8 — Commit

- [ ] Commit.

```bash
cd /Users/zachstallbohm/Work/gemma
git add services/skill_runner/skill_runner.py tests/services/skill_runner/test_skill_runner.py
git commit -m "SkillRunner: lazy load_skill activation, dedup, chain limit, confinement, dispatch"
```

---

## Phase 5 — SkillRegistry: data model + register/spawn

### Task 5.1 — Failing test: register spawns, handshakes, caches namespaced tools

- [ ] Create `/Users/zachstallbohm/Work/gemma/tests/services/skill_runner/test_skill_registry.py`:

```python
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_SERVICES = str(Path(__file__).resolve().parents[3] / "services")
if _SERVICES not in sys.path:
    sys.path.insert(0, _SERVICES)

from skill_runner.skill_registry import (
    SkillRegistry, SkillManifest, SkillProcess, SkillUnavailable, SkillDraining
)


def _tool(name, schema):
    return SimpleNamespace(name=name, inputSchema=schema)


def _patch_spawn(monkeypatch, registry, tools, *, session=None):
    """Replace _spawn so it sets up sp with mock session + given tools."""
    if session is None:
        session = AsyncMock()

    async def fake_spawn(sp):
        sp.session = session
        sp.stack = AsyncMock()
        sp.tools = {}
        sp.state = "READY"
        for t in tools:
            qualified = f"{sp.manifest.name}.{t.name}"
            sp.tools[t.name] = t.inputSchema
            registry._tool_index[qualified] = sp.manifest.name

    monkeypatch.setattr(registry, "_spawn", fake_spawn)
    return session


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_register_caches_namespaced_tools(monkeypatch):
    reg = SkillRegistry()
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    _patch_spawn(monkeypatch, reg, [_tool("commit", schema), _tool("log", {"type": "object"})])

    await reg.register(SkillManifest(name="git-ops", command="node", args=["index.js"]))

    sp = reg._skills["git-ops"]
    assert sp.state == "READY"
    assert set(sp.tools) == {"commit", "log"}
    assert reg._tool_index["git-ops.commit"] == "git-ops"
    assert reg._tool_index["git-ops.log"] == "git-ops"
```

### Task 5.2 — Run, observe missing module

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_registry.py -x -q
```

Expected failure: module `skill_registry.py` not found.

### Task 5.3 — Implement registry data model + register + _spawn

- [ ] Create `/Users/zachstallbohm/Work/gemma/services/skill_runner/skill_registry.py`:

```python
from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

import jsonschema
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# CRITICAL: log handler must write to sys.stderr, NEVER sys.stdout.
# The host's stdout is the JSON-RPC channel for any parent MCP server.
log = logging.getLogger("skill_registry")


class SkillUnavailable(Exception):
    pass


class SkillDraining(Exception):
    pass


@dataclass
class SkillManifest:
    name: str            # namespace prefix, e.g. "ast.repo-map"
    command: str         # "node" | "python" | absolute path to a rust binary
    args: list[str]
    env: dict | None = None
    version: str | None = None
    language: str | None = None


@dataclass
class SkillProcess:
    manifest: SkillManifest
    session: ClientSession | None = None
    stack: AsyncExitStack | None = None
    tools: dict[str, dict] = field(default_factory=dict)  # tool_name -> inputSchema
    state: str = "INIT"   # INIT | READY | DRAINING | DEAD
    inflight: int = 0
    restarts: int = 0


class SkillRegistry:
    """Manages long-lived MCP skill subprocesses.

    CRITICAL: ALL logging must go to sys.stderr. This class may itself
    run as an MCP server (parent stdio channel); stdout must be reserved
    for JSON-RPC framing exclusively.

    anyio cancel-scope rule: each ClientSession is entered AND exited by
    the SAME task. _spawn enters the session via the SkillProcess's own
    AsyncExitStack and _maybe_restart/reload close that same stack; the
    session object is never handed off to a different task to close.
    """

    def __init__(self, call_timeout: float = 30.0) -> None:
        self._skills: dict[str, SkillProcess] = {}
        self._tool_index: dict[str, str] = {}   # "ns.tool" -> skill name
        self._call_timeout = call_timeout
        self._lock = asyncio.Lock()

    async def register(self, m: SkillManifest) -> None:
        sp = SkillProcess(manifest=m)
        await self._spawn(sp)
        self._skills[m.name] = sp
        log.info("registered skill: %s (%d tools)", m.name, len(sp.tools))

    async def _spawn(self, sp: SkillProcess) -> None:
        m = sp.manifest
        params = StdioServerParameters(command=m.command, args=m.args, env=m.env)
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()                  # MCP handshake, once per lifetime
        listed = await session.list_tools()
        sp.session = session
        sp.stack = stack
        sp.tools = {}
        sp.state = "READY"
        for t in listed.tools:
            qualified = f"{m.name}.{t.name}"
            sp.tools[t.name] = t.inputSchema        # live schema from tools/list
            self._tool_index[qualified] = m.name    # namespace routing table
```

### Task 5.4 — Run, observe PASS

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_registry.py -x -q
```

Expected: 1 passed.

### Task 5.5 — Commit

- [ ] Commit.

```bash
cd /Users/zachstallbohm/Work/gemma
git add services/skill_runner/skill_registry.py tests/services/skill_runner/test_skill_registry.py
git commit -m "SkillRegistry: manifest/process model + register/_spawn"
```

---

## Phase 6 — SkillRegistry: call_tool with jsonschema gate + timeout

### Task 6.1 — Failing test: call_tool validates args, routes, returns result

- [ ] Append to `/Users/zachstallbohm/Work/gemma/tests/services/skill_runner/test_skill_registry.py`:

```python
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_valid_args_routes_to_session(monkeypatch):
    reg = SkillRegistry()
    schema = {
        "type": "object",
        "properties": {"width": {"type": "integer"}, "height": {"type": "integer"}},
        "required": ["width", "height"],
    }
    session = AsyncMock()
    session.call_tool.return_value = "resized"
    _patch_spawn(monkeypatch, reg, [_tool("resize_image", schema)], session=session)
    await reg.register(SkillManifest(name="img", command="python", args=["s.py"]))

    result = await reg.call_tool("img.resize_image", {"width": 10, "height": 20})

    assert result == "resized"
    session.call_tool.assert_awaited_once_with("resize_image", {"width": 10, "height": 20})
    assert reg._skills["img"].inflight == 0   # decremented in finally


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_bad_args_rejected_before_dispatch(monkeypatch):
    reg = SkillRegistry()
    schema = {
        "type": "object",
        "properties": {"width": {"type": "integer"}, "height": {"type": "integer"}},
        "required": ["width", "height"],
    }
    session = AsyncMock()
    _patch_spawn(monkeypatch, reg, [_tool("resize_image", schema)], session=session)
    await reg.register(SkillManifest(name="img", command="python", args=["s.py"]))

    with pytest.raises(jsonschema.ValidationError):
        await reg.call_tool("img.resize_image", {"width": "big"})

    session.call_tool.assert_not_awaited()    # subprocess never saw the call


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_unknown_skill_or_tool(monkeypatch):
    reg = SkillRegistry()
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})])
    await reg.register(SkillManifest(name="img", command="python", args=["s.py"]))

    with pytest.raises(SkillUnavailable):
        await reg.call_tool("nope.a", {})
    with pytest.raises(SkillUnavailable):
        await reg.call_tool("img.missing", {})


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_draining_raises(monkeypatch):
    reg = SkillRegistry()
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})])
    await reg.register(SkillManifest(name="img", command="python", args=["s.py"]))
    reg._skills["img"].state = "DRAINING"

    with pytest.raises(SkillDraining):
        await reg.call_tool("img.a", {})
```

`jsonschema` is imported in the registry module; reference it via `skill_registry.jsonschema` if needed — add `jsonschema = skill_registry.jsonschema` near the imports of the test file:

```python
jsonschema = skill_registry.jsonschema
```

### Task 6.2 — Run, observe AttributeError on call_tool

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_registry.py -k "call_tool" -x -q
```

Expected failure: `AttributeError: 'SkillRegistry' object has no attribute 'call_tool'`.

### Task 6.3 — Implement call_tool

- [ ] In `/Users/zachstallbohm/Work/gemma/services/skill_runner/skill_registry.py`, add to `SkillRegistry` after `_spawn`:

```python
    async def call_tool(self, qualified_name: str, arguments: dict) -> object:
        ns, _, tool = qualified_name.partition(".")
        sp = self._skills.get(ns)
        if sp is None or sp.state == "DEAD":
            raise SkillUnavailable(qualified_name)
        if sp.state == "DRAINING":
            raise SkillDraining(qualified_name)
        schema = sp.tools.get(tool)
        if schema is None:
            raise SkillUnavailable(f"no tool {tool!r} in skill {ns!r}")
        # jsonschema gate: validates BEFORE any subprocess round-trip.
        jsonschema.validate(instance=arguments, schema=schema)
        sp.inflight += 1
        try:
            return await asyncio.wait_for(
                sp.session.call_tool(tool, arguments),
                timeout=self._call_timeout,
            )
        except jsonschema.ValidationError:
            raise
        except Exception as exc:
            log.error("call %s failed: %r", qualified_name, exc)   # -> stderr only
            asyncio.create_task(self._maybe_restart(sp))
            raise
        finally:
            sp.inflight -= 1
```

> The jsonschema gate runs before `inflight` is incremented, so a validation failure raises without entering the try/finally and without counting as in-flight. The `except jsonschema.ValidationError: raise` clause is defensive in case a future change moves validation inside the block; it prevents a validation error from triggering a restart.

### Task 6.4 — Run, observe PASS

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_registry.py -k "call_tool" -x -q
```

Expected: all call_tool tests pass.

### Task 6.5 — Failing test: timeout schedules restart and re-raises

- [ ] Append:

```python
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_timeout_triggers_restart(monkeypatch):
    reg = SkillRegistry(call_timeout=0.05)

    async def slow_call(tool, args):
        await asyncio.sleep(10)

    session = AsyncMock()
    session.call_tool.side_effect = slow_call
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})], session=session)
    await reg.register(SkillManifest(name="img", command="python", args=["s.py"]))

    restart_called = asyncio.Event()

    async def fake_restart(sp):
        restart_called.set()

    monkeypatch.setattr(reg, "_maybe_restart", fake_restart)

    with pytest.raises(asyncio.TimeoutError):
        await reg.call_tool("img.a", {})

    await asyncio.wait_for(restart_called.wait(), timeout=1.0)
    assert reg._skills["img"].inflight == 0
```

### Task 6.6 — Run, observe PASS

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_registry.py::test_call_tool_timeout_triggers_restart -x -q
```

Expected: passes (the `asyncio.wait_for` raises `TimeoutError`, which is caught by the generic `except Exception`, schedules `_maybe_restart`, and re-raises).

### Task 6.7 — Commit

- [ ] Commit.

```bash
cd /Users/zachstallbohm/Work/gemma
git add services/skill_runner/skill_registry.py tests/services/skill_runner/test_skill_registry.py
git commit -m "SkillRegistry: call_tool jsonschema gate + timeout + restart scheduling"
```

---

## Phase 7 — SkillRegistry: supervision + exponential backoff restart

### Task 7.1 — Failing test: _maybe_restart marks DEAD, clears tools, backs off, respawns

- [ ] Append:

```python
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_maybe_restart_dead_then_respawn_with_backoff(monkeypatch):
    reg = SkillRegistry()
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})])
    await reg.register(SkillManifest(name="embed", command="python", args=["s.py"]))
    sp = reg._skills["embed"]

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(skill_registry.asyncio, "sleep", fake_sleep)

    # Track that _spawn restores state; re-patch to a tracking spawn.
    respawned = {"count": 0}
    original_tools = [_tool("a", {"type": "object"})]

    async def tracking_spawn(s):
        respawned["count"] += 1
        s.session = AsyncMock()
        s.stack = AsyncMock()
        s.tools = {t.name: t.inputSchema for t in original_tools}
        s.state = "READY"
        for t in original_tools:
            reg._tool_index[f"{s.manifest.name}.{t.name}"] = s.manifest.name

    monkeypatch.setattr(reg, "_spawn", tracking_spawn)

    await reg._maybe_restart(sp)

    assert respawned["count"] == 1
    assert sp.state == "READY"
    assert sp.restarts == 1
    assert sleeps == [1]          # backoff = min(2**0, 30) = 1
    assert reg._tool_index["embed.a"] == "embed"


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_maybe_restart_idempotent_when_already_dead(monkeypatch):
    reg = SkillRegistry()
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})])
    await reg.register(SkillManifest(name="embed", command="python", args=["s.py"]))
    sp = reg._skills["embed"]
    sp.state = "DEAD"

    spawn_calls = {"n": 0}

    async def counting_spawn(s):
        spawn_calls["n"] += 1

    monkeypatch.setattr(reg, "_spawn", counting_spawn)
    await reg._maybe_restart(sp)
    assert spawn_calls["n"] == 0   # early return, no respawn
```

### Task 7.2 — Failing test: supervise loop detects dead process and restarts

- [ ] Append:

```python
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_supervise_detects_dead_and_restarts(monkeypatch):
    reg = SkillRegistry()
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})])
    await reg.register(SkillManifest(name="embed", command="python", args=["s.py"]))
    sp = reg._skills["embed"]

    # Force the liveness probe to report the process as dead.
    monkeypatch.setattr(skill_registry, "_process_alive", lambda s: False)

    restarted = asyncio.Event()

    async def fake_restart(s):
        restarted.set()

    monkeypatch.setattr(reg, "_maybe_restart", fake_restart)

    task = asyncio.create_task(reg.supervise(interval=0.01))
    try:
        await asyncio.wait_for(restarted.wait(), timeout=1.0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
```

### Task 7.3 — Run the Phase 7 tests, observe AttributeError

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_registry.py -k "maybe_restart or supervise" -x -q
```

Expected failure: `AttributeError: ... '_maybe_restart'`.

### Task 7.4 — Implement _maybe_restart, supervise, _process_alive

- [ ] In `/Users/zachstallbohm/Work/gemma/services/skill_runner/skill_registry.py`, add to `SkillRegistry` after `call_tool`:

```python
    async def _maybe_restart(self, sp: SkillProcess) -> None:
        async with self._lock:
            if sp.state == "DEAD":
                return
            sp.state = "DEAD"
            # Remove this skill's tools from the routing index.
            dead_keys = [k for k, v in self._tool_index.items()
                         if v == sp.manifest.name]
            for k in dead_keys:
                self._tool_index.pop(k, None)
            if sp.stack:
                try:
                    await sp.stack.aclose()
                except Exception:
                    pass
            backoff = min(2 ** sp.restarts, 30)
            sp.restarts += 1
            log.warning("restarting skill %s in %ds (attempt %d)",
                        sp.manifest.name, backoff, sp.restarts)
            await asyncio.sleep(backoff)
            await self._spawn(sp)

    async def supervise(self, interval: float = 5.0) -> None:
        """Background health loop. Run as an asyncio.Task at harness startup."""
        while True:
            await asyncio.sleep(interval)
            for sp in list(self._skills.values()):
                if sp.state == "READY" and not _process_alive(sp):
                    log.warning("skill %s died unexpectedly", sp.manifest.name)
                    await self._maybe_restart(sp)
```

- [ ] Add the module-level helper at the end of `/Users/zachstallbohm/Work/gemma/services/skill_runner/skill_registry.py`:

```python
def _process_alive(sp: SkillProcess) -> bool:
    """Check if the subprocess underlying the MCP session is still running.

    The exact handle depends on mcp SDK internals; this conservative check
    treats a process as alive while it holds a session and is not DEAD/INIT.
    The supervise() loop overrides this in tests via monkeypatch.
    """
    return sp.session is not None and sp.state not in ("DEAD", "INIT")
```

### Task 7.5 — Run the Phase 7 tests, observe PASS

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_registry.py -k "maybe_restart or supervise" -x -q
```

Expected: all Phase 7 tests pass.

### Task 7.6 — Commit

- [ ] Commit.

```bash
cd /Users/zachstallbohm/Work/gemma
git add services/skill_runner/skill_registry.py tests/services/skill_runner/test_skill_registry.py
git commit -m "SkillRegistry: supervision loop + exponential-backoff restart"
```

---

## Phase 8 — SkillRegistry: hot-reload (drain then swap)

### Task 8.1 — Failing test: reload drains in-flight calls before swapping

- [ ] Append:

```python
@pytest.mark.mocked
@pytest.mark.asyncio
async def test_reload_drains_inflight_before_swap(monkeypatch):
    reg = SkillRegistry()
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})])
    await reg.register(SkillManifest(name="summarizer", command="python", args=["s.py"]))
    sp = reg._skills["summarizer"]
    old_stack = sp.stack

    # Two calls in flight.
    sp.inflight = 2

    spawn_calls = {"n": 0}

    async def tracking_spawn(s):
        spawn_calls["n"] += 1
        s.stack = AsyncMock()
        s.state = "READY"

    monkeypatch.setattr(reg, "_spawn", tracking_spawn)

    reload_task = asyncio.create_task(reg.reload("summarizer"))

    # Give the loop a chance: reload must NOT have spawned yet (still draining).
    await asyncio.sleep(0.15)
    assert sp.state == "DRAINING"
    assert spawn_calls["n"] == 0

    # Drain the in-flight calls.
    sp.inflight = 0
    await asyncio.wait_for(reload_task, timeout=1.0)

    assert spawn_calls["n"] == 1
    assert sp.state == "READY"
    old_stack.aclose.assert_awaited_once()   # old process shut down after swap
```

### Task 8.2 — Run, observe AttributeError on reload

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_registry.py::test_reload_drains_inflight_before_swap -x -q
```

Expected failure: `AttributeError: ... 'reload'`.

### Task 8.3 — Implement reload

- [ ] In `/Users/zachstallbohm/Work/gemma/services/skill_runner/skill_registry.py`, add to `SkillRegistry` after `_maybe_restart` (before `supervise` is fine too):

```python
    async def reload(self, name: str) -> None:
        """Hot-reload: drain in-flight calls, then swap to a new process."""
        sp = self._skills[name]
        old_stack = sp.stack
        sp.state = "DRAINING"               # router stops accepting new calls
        while sp.inflight > 0:
            await asyncio.sleep(0.05)       # let in-flight calls finish
        await self._spawn(sp)               # new process is now READY
        if old_stack:
            await old_stack.aclose()        # shut down old process after swap
```

### Task 8.4 — Run, observe PASS

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_registry.py::test_reload_drains_inflight_before_swap -x -q
```

Expected: passes.

> The drain loop uses 0.05s polling; the spec stub uses the same interval. New calls during DRAINING raise `SkillDraining` (covered by `test_call_tool_draining_raises` in Phase 6), so no in-flight count grows during the drain.

### Task 8.5 — Commit

- [ ] Commit.

```bash
cd /Users/zachstallbohm/Work/gemma
git add services/skill_runner/skill_registry.py tests/services/skill_runner/test_skill_registry.py
git commit -m "SkillRegistry: hot-reload drain-then-swap"
```

---

## Phase 9 — Package exports + optional hot-reload watcher

### Task 9.1 — Failing test: package `__init__` exports the public names

- [ ] Append a new test to `/Users/zachstallbohm/Work/gemma/tests/services/skill_runner/test_skill_runner.py`:

```python
@pytest.mark.mocked
def test_package_exports():
    pkg_dir = Path(__file__).resolve().parents[3] / "services" / "skill_runner"
    init_path = pkg_dir / "__init__.py"
    assert init_path.exists()
    text = init_path.read_text(encoding="utf-8")
    for name in ("SkillRunner", "SkillRegistry", "SkillMeta", "SkillManifest", "SkillProcess"):
        assert name in text
```

### Task 9.2 — Run, observe failure

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py::test_package_exports -x -q
```

Expected failure: `__init__.py` does not exist.

### Task 9.3 — Implement `services/skill_runner/__init__.py`

- [ ] Create `/Users/zachstallbohm/Work/gemma/services/skill_runner/__init__.py`:

```python
"""Labmate skills layer: SkillRunner (instruction skills) + SkillRegistry (MCP subprocesses)."""

from .skill_registry import (
    SkillDraining,
    SkillManifest,
    SkillProcess,
    SkillRegistry,
    SkillUnavailable,
)
from .skill_runner import SkillMeta, SkillRunner

__all__ = [
    "SkillRunner",
    "SkillMeta",
    "SkillRegistry",
    "SkillManifest",
    "SkillProcess",
    "SkillUnavailable",
    "SkillDraining",
]
```

> Note: the test only inspects the source text (the directory is hyphenated and not importable as a normal package without path tricks), so relative imports here document the intended public surface without needing the dir to be import-resolvable in tests.

### Task 9.4 — Run, observe PASS

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py::test_package_exports -x -q
```

Expected: passes.

### Task 9.5 — Failing test: optional hot-reload re-runs discover on a watch event

- [ ] Append to `/Users/zachstallbohm/Work/gemma/tests/services/skill_runner/test_skill_runner.py`:

```python
@pytest.mark.mocked
def test_reload_catalog_rescans(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()
    assert set(runner.catalog) == {"deploy"}

    # Add a new skill on disk, then re-scan.
    write_skill("project", "newskill", VALID_SKILL.format(name="newskill", desc="Brand new."))
    runner.reload_catalog()
    assert set(runner.catalog) == {"deploy", "newskill"}
```

### Task 9.6 — Run, observe AttributeError

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py::test_reload_catalog_rescans -x -q
```

Expected failure: `AttributeError: ... 'reload_catalog'`.

### Task 9.7 — Implement `reload_catalog` (sync re-scan)

- [ ] In `/Users/zachstallbohm/Work/gemma/services/skill_runner/skill_runner.py`, add to `SkillRunner` after `discover`:

```python
    def reload_catalog(self) -> None:
        """Re-run discovery. Safe to call on a filesystem change event.

        Preserves the activation cache (self.loaded) and the activation counter;
        only the metadata catalog is rebuilt.
        """
        self.discover()
```

### Task 9.8 — Run, observe PASS

- [ ] Run:

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/test_skill_runner.py::test_reload_catalog_rescans -x -q
```

Expected: passes.

### Task 9.9 — Commit

- [ ] Commit.

```bash
cd /Users/zachstallbohm/Work/gemma
git add services/skill_runner/__init__.py services/skill_runner/skill_runner.py tests/services/skill_runner/test_skill_runner.py
git commit -m "skill_runner: package exports + reload_catalog re-scan"
```

---

## Phase 10 — Full suite + final review

### Task 10.1 — Run the entire suite

- [ ] Run every test for the skills layer.

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skill_runner/ -q
```

Expected: all tests pass.

### Task 10.2 — Grep for stdout violations

- [ ] Confirm no `print(` and no direct `sys.stdout` writes exist in production code.

```bash
cd /Users/zachstallbohm/Work/gemma
grep -rnE 'print\(|sys\.stdout' services/skill_runner/ || echo "clean: no stdout writes"
```

Expected: `clean: no stdout writes`.

### Task 10.3 — Grep for forbidden yaml.load / tiktoken

- [ ] Confirm no unsafe loaders or tiktoken.

```bash
cd /Users/zachstallbohm/Work/gemma
grep -rnE 'yaml\.load|FullLoader|tiktoken' services/skill_runner/ || echo "clean: safe parsing only"
```

Expected: `clean: safe parsing only`.

### Task 10.4 — Final commit

- [ ] Commit any remaining changes.

```bash
cd /Users/zachstallbohm/Work/gemma
git add -A services/skill_runner/ tests/services/skill_runner/
git commit -m "skill_runner: full suite green; stdout/yaml safety verified" || echo "nothing to commit"
```

---

## Spec coverage map (self-review)

**Section 4 — SkillRunner:**

| Spec requirement | Task |
|---|---|
| 4.1 glob `rglob("SKILL.md")` in priority order | 1.3 (`discover`) |
| 4.1 resolve real path + within-root symlink guard | 1.3 (`_within` in `_index`) |
| 4.1 parse frontmatter ONLY, no body read at discovery | 1.1/1.3 |
| 4.1 validate required `name`/`description` | 2.1 (test) / 1.3 (impl) |
| 4.1 name collision: highest tier wins + shadow warning | 2.3 (test) / 1.3 (impl) |
| 4.1 `python-frontmatter` / `yaml.safe_load`, never override | 2.5 (test) / 1.3 (impl) |
| 4.1 hot-reload re-run discover | 9.5 (test) / 9.7 (`reload_catalog`) |
| 4.2 `catalog_prompt()` compact block | 3.1 / 3.4 |
| 4.2 `tool_schema()` load_skill with enum of names | 3.2 / 3.4 |
| 4.3 `dispatch` extracts name, JSON-string args | 4.3 / 4.5 |
| 4.3 unknown name → structured error with available list | 4.1 / 4.5 |
| 4.3 re-validate confinement before read | 4.3 / 4.5 |
| 4.3 dedup `already_loaded`, no re-append | 4.1 / 4.5 |
| 4.3 read+parse body, store in `self.loaded`, return `loaded` | 4.1 / 4.5 |
| 4.5 chain limit `max_chain` (default 8) | 4.2 / 4.5 |
| `SkillMeta` dataclass (name, description, path, tier, frontmatter) | 1.1 / 1.3 |
| `_err` helper | 1.3 |

**Section 5 — SkillRegistry:**

| Spec requirement | Task |
|---|---|
| 5.1 `register` → `_spawn` one persistent subprocess | 5.1 / 5.3 |
| 5.1 `StdioServerParameters` + `stdio_client` + `ClientSession` | 5.3 |
| 5.1 `initialize` handshake once; `list_tools` cached | 5.3 |
| 5.1 namespace `{name}.{tool}` in `_tool_index` | 5.1 / 5.3 |
| 5.1 state set READY | 5.1 / 5.3 |
| 5.2 parse `ns.tool`; `SkillUnavailable` if missing/DEAD | 6.1 / 6.3 |
| 5.2 jsonschema gate before dispatch | 6.1 / 6.3 |
| 5.2 increment inflight | 6.1 / 6.3 |
| 5.2 `asyncio.wait_for` per-call timeout | 6.5 / 6.3 |
| 5.2 on failure: log stderr, schedule `_maybe_restart`, re-raise | 6.5 / 6.3 |
| 5.2 decrement inflight in finally | 6.1 / 6.3 |
| 5.3 `_maybe_restart`: lock, early-return if DEAD | 7.1 / 7.4 |
| 5.3 set DEAD, remove tools from index | 7.1 / 7.4 |
| 5.3 `stack.aclose()` cleanup | 7.1 / 7.4 |
| 5.3 backoff `min(2**restarts, 30)`, increment, sleep, respawn | 7.1 / 7.4 |
| 5.3 `supervise` background loop with `_process_alive` | 7.2 / 7.4 |
| 5.4 `reload`: DRAINING, drain inflight, spawn new, close old | 8.1 / 8.3 |
| `SkillManifest` / `SkillProcess` dataclasses | 5.1 / 5.3 |
| `SkillUnavailable` / `SkillDraining` exceptions | 5.1 / 5.3 |
| `_process_alive` helper | 7.2 / 7.4 |

**Critical-rule coverage:** stdout-sacred (10.2 grep + named loggers throughout), anyio cancel-scope (registry docstring in 5.3: session entered/exited via the SkillProcess's own stack, never handed to another task), yaml.safe_load only (2.5 test + 10.3 grep), never tiktoken (10.3 grep).

**BDD scenario coverage (section 7):** discovery-frontmatter-only (1.1), malformed-skip-warn (2.1), project-overrides-personal (2.3), load-on-demand (4.1), unknown-rejected (4.1), already-loaded (4.1), chain-cap (4.2), directory-traversal/confinement (4.3 + note in 4.3), safe-loader (2.5), TS-skill registration/namespacing (5.1), bad-input-rejected-pre-dispatch (6.1), crash-restart-backoff (7.1/7.2), hot-reload-drain (8.1).

> AST tool scenarios (section 7, repo-map / tree-sitter / ts-morph / ast-grep) are out of scope for SkillRunner + SkillRegistry; they belong to the built-in skill servers (spec section 6) and are tracked separately.
