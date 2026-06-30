"""
Test client_context.py — per-task client capability manifest context variable.

Tests the set/get/reset round-trip mirroring call_counter.py.
"""

from __future__ import annotations

from services.orchestrator import client_context
from services.orchestrator.tool_manifest import ClientManifest


def test_get_manifest_default_is_none() -> None:
    """When no manifest is set, get_manifest() returns None."""
    # Defensive: reset to ensure isolation
    manifest = client_context.get_manifest()
    assert manifest is None


def test_set_and_get_manifest() -> None:
    """set_manifest() stores a manifest; get_manifest() retrieves it."""
    manifest_data: ClientManifest = {
        "protocol_version": 1,
        "tools": [
            {"name": "read_file", "source": "builtin"},
            {"name": "write_file", "source": "builtin"},
        ],
    }
    token = client_context.set_manifest(manifest_data)
    assert token is not None

    retrieved = client_context.get_manifest()
    assert retrieved is not None
    assert retrieved["protocol_version"] == 1
    assert len(retrieved["tools"]) == 2
    assert retrieved["tools"][0]["name"] == "read_file"

    # Clean up
    client_context.reset_manifest(token)


def test_set_manifest_none() -> None:
    """set_manifest(None) stores None; get_manifest() returns None."""
    token = client_context.set_manifest(None)
    assert token is not None

    manifest = client_context.get_manifest()
    assert manifest is None

    client_context.reset_manifest(token)


def test_reset_manifest_restores_previous_state() -> None:
    """reset_manifest() restores the ContextVar to its previous state."""
    # Set first manifest
    manifest1: ClientManifest = {
        "protocol_version": 1,
        "tools": [{"name": "read_file", "source": "builtin"}],
    }
    token1 = client_context.set_manifest(manifest1)

    # Verify it's set
    retrieved1 = client_context.get_manifest()
    assert retrieved1 is not None
    assert len(retrieved1["tools"]) == 1

    # Set second manifest (nested context)
    manifest2: ClientManifest = {
        "protocol_version": 1,
        "tools": [{"name": "write_file", "source": "builtin"}],
    }
    token2 = client_context.set_manifest(manifest2)
    retrieved2 = client_context.get_manifest()
    assert retrieved2 is not None
    assert len(retrieved2["tools"]) == 1
    assert retrieved2["tools"][0]["name"] == "write_file"

    # Reset back to first manifest
    client_context.reset_manifest(token2)
    retrieved1_again = client_context.get_manifest()
    assert retrieved1_again is not None
    assert len(retrieved1_again["tools"]) == 1
    assert retrieved1_again["tools"][0]["name"] == "read_file"

    # Final reset back to None
    client_context.reset_manifest(token1)
    final = client_context.get_manifest()
    assert final is None


def test_reset_manifest_with_none_token_is_safe() -> None:
    """reset_manifest(None) is a safe no-op (matches call_counter pattern)."""
    # This should not raise
    client_context.reset_manifest(None)
    assert client_context.get_manifest() is None


def test_reset_manifest_is_idempotent() -> None:
    """Calling reset_manifest twice on the same token is safe (no-op on second call)."""
    manifest: ClientManifest = {
        "protocol_version": 1,
        "tools": [{"name": "read_file", "source": "builtin"}],
    }
    token = client_context.set_manifest(manifest)

    # First reset should restore to None
    client_context.reset_manifest(token)
    assert client_context.get_manifest() is None

    # Second reset on the same token should be safe (no-op or cached exception)
    # This exercises the defensive try/except in reset_manifest
    client_context.reset_manifest(token)
    assert client_context.get_manifest() is None


def test_get_workspace_root_default_is_none() -> None:
    """When no workspace root is set, get_workspace_root() returns None."""
    # Defensive: reset to ensure isolation
    root = client_context.get_workspace_root()
    assert root is None


def test_set_and_get_workspace_root() -> None:
    """set_workspace_root() stores a root; get_workspace_root() retrieves it."""
    root_path = "/Users/zach/Work/myproject"
    token = client_context.set_workspace_root(root_path)
    assert token is not None

    retrieved = client_context.get_workspace_root()
    assert retrieved == root_path

    # Clean up
    client_context.reset_workspace_root(token)


def test_set_workspace_root_none() -> None:
    """set_workspace_root(None) stores None; get_workspace_root() returns None."""
    token = client_context.set_workspace_root(None)
    assert token is not None

    root = client_context.get_workspace_root()
    assert root is None

    client_context.reset_workspace_root(token)


def test_reset_workspace_root_restores_previous_state() -> None:
    """reset_workspace_root() restores the ContextVar to its previous state."""
    # Set first root
    root1 = "/path/one"
    token1 = client_context.set_workspace_root(root1)

    # Verify it's set
    retrieved1 = client_context.get_workspace_root()
    assert retrieved1 == root1

    # Set second root (nested context)
    root2 = "/path/two"
    token2 = client_context.set_workspace_root(root2)
    retrieved2 = client_context.get_workspace_root()
    assert retrieved2 == root2

    # Reset back to first root
    client_context.reset_workspace_root(token2)
    retrieved1_again = client_context.get_workspace_root()
    assert retrieved1_again == root1

    # Final reset back to None
    client_context.reset_workspace_root(token1)
    final = client_context.get_workspace_root()
    assert final is None


def test_reset_workspace_root_with_none_token_is_safe() -> None:
    """reset_workspace_root(None) is a safe no-op (matches call_counter pattern)."""
    # This should not raise
    client_context.reset_workspace_root(None)
    assert client_context.get_workspace_root() is None


def test_reset_workspace_root_is_idempotent() -> None:
    """Calling reset_workspace_root twice on the same token is safe (no-op on second call)."""
    root_path = "/absolute/path"
    token = client_context.set_workspace_root(root_path)

    # First reset should restore to None
    client_context.reset_workspace_root(token)
    assert client_context.get_workspace_root() is None

    # Second reset on the same token should be safe (no-op or cached exception)
    # This exercises the defensive try/except in reset_workspace_root
    client_context.reset_workspace_root(token)
    assert client_context.get_workspace_root() is None
