"""Re-exports MCPClientManager from services/mcp-bridge/.

The implementation lives in mcp-bridge/ alongside the TypeScript server it
manages. The hyphenated directory name prevents direct Python import, so this
shim adds it to sys.path and re-exports the public API.
"""
from __future__ import annotations

import sys
from pathlib import Path

_bridge_root = Path(__file__).resolve().parent.parent / "mcp-bridge"
if str(_bridge_root) not in sys.path:
    sys.path.insert(0, str(_bridge_root))

from mcp_client_manager import MCPClientManager, CircuitOpenError  # noqa: F401

__all__ = ["MCPClientManager", "CircuitOpenError"]
