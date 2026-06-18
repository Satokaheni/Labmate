"""Tests for services/skill_worker/manifest_loader.py"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from services.skill_worker.manifest_loader import discover_manifests


def _make_python_skill(root: Path, name: str) -> None:
    d = root / name
    d.mkdir()
    (d / "server.py").write_text("# mcp server stub")


def _make_ts_skill(root: Path, name: str) -> None:
    d = root / name
    d.mkdir()
    dist = d / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("// compiled entrypoint")


def _make_instruction_skill(root: Path, name: str) -> None:
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: test\ndescription: test\n---\n")


# ── discovery ──────────────────────────────────────────────────────────────────

def test_discovers_python_skill(tmp_path):
    _make_python_skill(tmp_path, "ast-repo-map")
    manifests = discover_manifests(tmp_path)
    assert len(manifests) == 1
    m = manifests[0]
    assert m.name == "ast-repo-map"
    assert m.command == sys.executable
    assert m.args[0].endswith("server.py")


def test_discovers_typescript_skill(tmp_path):
    _make_ts_skill(tmp_path, "web-search")
    manifests = discover_manifests(tmp_path)
    assert len(manifests) == 1
    m = manifests[0]
    assert m.name == "web-search"
    assert m.command == "node"
    assert m.args[0].endswith("dist/index.js")


def test_skips_instruction_only_skills(tmp_path):
    _make_instruction_skill(tmp_path, "critique")
    manifests = discover_manifests(tmp_path)
    assert manifests == []


def test_skips_empty_directories(tmp_path):
    (tmp_path / "empty").mkdir()
    manifests = discover_manifests(tmp_path)
    assert manifests == []


def test_python_wins_when_both_present(tmp_path):
    d = tmp_path / "hybrid"
    d.mkdir()
    (d / "server.py").write_text("# server")
    dist = d / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("// also here")
    manifests = discover_manifests(tmp_path)
    assert len(manifests) == 1
    assert manifests[0].command == sys.executable


def test_results_sorted_by_name(tmp_path):
    for name in ("zeta-skill", "alpha-skill", "beta-skill"):
        _make_python_skill(tmp_path, name)
    names = [m.name for m in discover_manifests(tmp_path)]
    assert names == sorted(names)


def test_multiple_mixed_skills(tmp_path):
    _make_python_skill(tmp_path, "ast-repo-map")
    _make_ts_skill(tmp_path, "web-search")
    _make_instruction_skill(tmp_path, "critique")
    manifests = discover_manifests(tmp_path)
    assert len(manifests) == 2
    names = {m.name for m in manifests}
    assert names == {"ast-repo-map", "web-search"}
    assert "critique" not in names


def test_server_py_path_is_absolute(tmp_path):
    _make_python_skill(tmp_path, "my-skill")
    m = discover_manifests(tmp_path)[0]
    assert Path(m.args[0]).is_absolute()


def test_index_js_path_is_absolute(tmp_path):
    _make_ts_skill(tmp_path, "my-ts-skill")
    m = discover_manifests(tmp_path)[0]
    assert Path(m.args[0]).is_absolute()
