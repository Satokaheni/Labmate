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
