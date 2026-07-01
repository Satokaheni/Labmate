"""
Test that the generated contract files (Python and TypeScript) match the JSON source of truth.

This test suite enforces that:
1. The generated Python TypedDicts have fields matching the JSON contract
2. The generated Python BUILTIN_TOOL_NAMES matches the JSON
3. The parse_manifest function accepts a frontend-shaped frame (behavior preserved)
4. Running gen_contract.py produces byte-identical output (contract is current)
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from services.orchestrator._contract_generated import (
    BUILTIN_TOOL_NAMES,
    ClientManifest,
    ToolDescriptor,
)
from services.orchestrator.tool_manifest import parse_manifest


class TestContractDefinitions:
    """Verify generated Python types match the JSON source of truth."""

    @staticmethod
    def _load_contract() -> dict[str, Any]:
        """Load contract/manifest.contract.json."""
        contract_file = (
            Path(__file__).parent.parent.parent.parent / "contract" / "manifest.contract.json"
        )
        with open(contract_file) as f:
            return json.load(f)

    def test_tool_descriptor_fields_match_json(self) -> None:
        """Assert ToolDescriptor.__annotations__ keys == JSON ToolDescriptor fields."""
        contract = self._load_contract()
        json_fields = contract["shapes"]["ToolDescriptor"]["fields"]

        # ToolDescriptor is a TypedDict with total=False, so all keys are optional.
        # We check that the annotations match the JSON fields.
        assert set(ToolDescriptor.__annotations__.keys()) == set(json_fields.keys())

    def test_client_manifest_fields_match_json(self) -> None:
        """Assert ClientManifest.__annotations__ keys == JSON ClientManifest fields (with pyName mapping)."""
        contract = self._load_contract()
        json_fields = contract["shapes"]["ClientManifest"]["fields"]

        # Map JSON field names through pyName if present
        expected_keys = set()
        for field_name, field_def in json_fields.items():
            py_name = field_def.get("pyName", field_name)
            expected_keys.add(py_name)

        assert set(ClientManifest.__annotations__.keys()) == expected_keys

    def test_builtin_tool_names_matches_json(self) -> None:
        """Assert BUILTIN_TOOL_NAMES == JSON builtinToolNames."""
        contract = self._load_contract()
        json_builtins = contract["builtinToolNames"]

        assert BUILTIN_TOOL_NAMES == tuple(json_builtins)

    def test_parse_manifest_accepts_frontend_shaped_frame(self) -> None:
        """Assert parse_manifest still accepts a frontend-shaped manifest (camelCase)."""
        # Frontend sends protocolVersion (camelCase); parse_manifest must accept it
        frontend_frame = {
            "protocolVersion": 1,
            "tools": [
                {"name": "read_file", "source": "builtin"},
                {"name": "write_file", "source": "builtin"},
            ],
        }
        manifest = parse_manifest(frontend_frame)

        assert manifest is not None
        assert manifest.get("protocol_version") == 1  # normalized to snake_case
        assert len(manifest.get("tools", [])) == 2

    def test_parse_manifest_accepts_snake_case(self) -> None:
        """Assert parse_manifest also accepts snake_case (for backward compat)."""
        snake_case_frame = {
            "protocol_version": 1,
            "tools": [{"name": "read_file", "source": "builtin"}],
        }
        manifest = parse_manifest(snake_case_frame)

        assert manifest is not None
        assert manifest.get("protocol_version") == 1

    def test_parse_manifest_returns_none_for_empty_tools(self) -> None:
        """Assert parse_manifest returns None when tools list is empty."""
        manifest = parse_manifest({"protocolVersion": 1, "tools": []})
        assert manifest is None

    def test_parse_manifest_defaults_source_to_builtin(self) -> None:
        """Assert parse_manifest defaults source to 'builtin' if omitted."""
        frame = {
            "protocolVersion": 1,
            "tools": [{"name": "read_file"}],  # no source field
        }
        manifest = parse_manifest(frame)

        assert manifest is not None
        tools = manifest.get("tools", [])
        assert len(tools) == 1
        assert tools[0].get("source") == "builtin"


class TestCodegenDeterminism:
    """Verify running gen_contract.py produces byte-identical output."""

    @staticmethod
    def _get_repo_root() -> Path:
        """Get repo root from __file__."""
        return Path(__file__).parent.parent.parent.parent

    def test_gen_contract_py_produces_byte_identical_output(self) -> None:
        """Run gen_contract.py and assert the generated files are byte-identical to committed ones."""
        repo_root = self._get_repo_root()

        # Read the current committed generated files
        ts_file = repo_root / "services" / "frontend" / "src" / "protocol" / "contract.generated.ts"
        py_file = repo_root / "services" / "orchestrator" / "_contract_generated.py"

        ts_original = ts_file.read_text()
        py_original = py_file.read_text()

        # Run gen_contract.py from the repo root
        result = subprocess.run(
            ["python", "scripts/gen_contract.py"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"gen_contract.py failed: {result.stderr}"

        # Read regenerated files
        ts_regenerated = ts_file.read_text()
        py_regenerated = py_file.read_text()

        # Compare (the files were just regenerated in-place, so they should match the originals)
        assert ts_regenerated == ts_original, "TypeScript generated file differs from committed"
        assert py_regenerated == py_original, "Python generated file differs from committed"
