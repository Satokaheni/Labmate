from __future__ import annotations

import os

from services.skill_runner.skill_registry import _REPO_ROOT, _skill_env


def test_skill_env_inherits_gemma_base(monkeypatch):
    """Split-topology fix: skills must receive GEMMA_BASE/QWEN_BASE (the MCP SDK's
    minimal default env drops them, so skills hit a dead localhost:8000)."""
    monkeypatch.setenv("GEMMA_BASE", "https://pod-8000.proxy.runpod.net/v1")
    monkeypatch.setenv("QWEN_BASE", "https://pod-8000.proxy.runpod.net/v1")
    env = _skill_env(None)
    assert env["GEMMA_BASE"] == "https://pod-8000.proxy.runpod.net/v1"
    assert env["QWEN_BASE"] == "https://pod-8000.proxy.runpod.net/v1"


def test_skill_env_prepends_absolute_repo_root_to_pythonpath(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = _skill_env(None)
    assert env["PYTHONPATH"].split(os.pathsep)[0] == _REPO_ROOT
    assert os.path.isabs(_REPO_ROOT)


def test_skill_env_preserves_existing_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/some/existing")
    parts = _skill_env(None)["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == _REPO_ROOT
    assert "/some/existing" in parts


def test_skill_env_manifest_override_wins(monkeypatch):
    monkeypatch.setenv("GEMMA_BASE", "https://a/v1")
    env = _skill_env({"GEMMA_BASE": "https://override/v1"})
    assert env["GEMMA_BASE"] == "https://override/v1"
