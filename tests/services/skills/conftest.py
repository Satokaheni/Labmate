"""Central import isolation for the per-skill test suites.

Each skill in ``services/skills/<name>/`` is a standalone MCP server whose
modules import each other FLAT (``from models import ...``, ``import server``).
At runtime every skill runs in its own process with cwd = the skill dir, so the
flat names never clash. Inside ONE pytest process, however, every skill source
dir lands on ``sys.path`` and CPython caches modules by their bare name. The
first skill to import ``models`` then shadows the rest, so a sibling's
``from models import GenerationResult`` resolves to the wrong file ->
``ImportError`` / wrong class / ``AttributeError``. The per-skill test
``conftest`` modules collide the same way (``from conftest import _FakeResponse``
picks up whichever skill's conftest was cached first), and the hyphenated skill
dir names (``screenshot-to-component``) stop pytest from package-qualifying the
duplicate ``test_server.py`` files ("import file mismatch").

All of these are collection-order artifacts, not real bugs — every skill suite
passes in isolation. This conftest re-establishes that isolation for the whole
``tests/services/skills/`` tree without forcing ``--import-mode=importlib`` (which
breaks the ``from conftest import ...`` tests). Before pytest imports each skill
test module, and again before each of its tests runs (test bodies and fixtures
re-import these modules at run time — e.g. ``import server as srv``), we:

  * put that skill's source dir and its own test dir first on ``sys.path``;
  * evict only the AMBIGUOUS cached modules — the source basenames that exist in
    more than one skill (``models``/``schemas``/``server``) plus ``conftest`` —
    when they currently resolve to a DIFFERENT skill, so the flat names rebind to
    THIS skill. Skill-UNIQUE source modules are deliberately left cached: fixtures
    re-import them to monkeypatch, which only works if object identity is
    preserved; and
  * drop the bare test-module name to avoid prepend-mode basename clashes (the
    duplicate ``test_server.py`` files).
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

_TESTS_SKILLS_DIR = Path(__file__).resolve().parent
_SRC_SKILLS_DIR = (_TESTS_SKILLS_DIR.parents[2] / "services" / "skills").resolve()


def _shared_source_names() -> set[str]:
    """Top-level module basenames that exist in more than one skill (collide)."""
    counts: Counter[str] = Counter()
    for skill_dir in _SRC_SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        for py in skill_dir.glob("*.py"):
            if py.stem != "__init__":
                counts[py.stem] += 1
    return {name for name, n in counts.items() if n > 1}


# Ambiguous flat names that must be re-resolved per skill: shared source modules
# plus the per-skill test ``conftest`` (each skill ships its own).
_AMBIGUOUS_NAMES = _shared_source_names() | {"conftest"}


def _skill_of(path: Path) -> str | None:
    """Return the skill name for a test file under tests/services/skills/<skill>/."""
    try:
        rel = path.resolve().relative_to(_TESTS_SKILLS_DIR)
    except ValueError:
        return None
    return rel.parts[0] if len(rel.parts) >= 2 else None


def _isolate(test_path: Path) -> None:
    skill = _skill_of(test_path)
    if not skill:
        return
    src_dir = _SRC_SKILLS_DIR / skill
    test_dir = (_TESTS_SKILLS_DIR / skill).resolve()
    # This skill's dirs must win flat-name lookups: source dir for `import server`,
    # test dir for `import conftest`.
    allowed = []
    for d in (test_dir, src_dir):
        if d.is_dir():
            ds = str(d)
            if ds in sys.path:
                sys.path.remove(ds)
            sys.path.insert(0, ds)
            allowed.append(ds + "/")
    # Evict only the ambiguous cached modules left by a DIFFERENT skill so this
    # skill's flat imports rebind to its own files. Skill-unique modules stay
    # cached so fixtures' monkeypatching keeps its object identity.
    for name in _AMBIGUOUS_NAMES:
        mod = sys.modules.get(name)
        f = getattr(mod, "__file__", None) if mod is not None else None
        if f and not any(str(Path(f).resolve()).startswith(a) for a in allowed):
            del sys.modules[name]
    # Drop this test file's bare module name so prepend-mode does not trip over a
    # same-named test file from another skill (the duplicate test_server.py).
    sys.modules.pop(test_path.stem, None)


@pytest.hookimpl(tryfirst=True)
def pytest_collectstart(collector):
    if isinstance(collector, pytest.Module):
        _isolate(Path(str(collector.path)))


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _isolate(Path(str(item.path)))
