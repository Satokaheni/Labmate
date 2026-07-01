#!/usr/bin/env python3
"""
Codegen: generate the Python<->TS contract from contract/manifest.contract.json.

Reads the single JSON source of truth and emits TWO generated files with
DO-NOT-EDIT headers:
  - services/frontend/src/protocol/contract.generated.ts
  - services/orchestrator/_contract_generated.py

Run from the repo root: python scripts/gen_contract.py
"""

import json
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).parent.parent
    contract_file = repo_root / "contract" / "manifest.contract.json"

    if not contract_file.exists():
        print(f"Error: {contract_file} not found", file=sys.stderr)
        return 1

    with open(contract_file) as f:
        contract = json.load(f)

    tool_sources = contract.get("toolSource", [])
    builtin_tool_names = contract.get("builtinToolNames", [])
    shapes = contract.get("shapes", {})

    # Generate TypeScript
    ts_content = _generate_typescript(tool_sources, builtin_tool_names, shapes)
    ts_file = repo_root / "services" / "frontend" / "src" / "protocol" / "contract.generated.ts"
    ts_file.write_text(ts_content)
    print(f"Generated: {ts_file}")

    # Generate Python
    py_content = _generate_python(shapes, builtin_tool_names)
    py_file = repo_root / "services" / "orchestrator" / "_contract_generated.py"
    py_file.write_text(py_content)
    print(f"Generated: {py_file}")

    return 0


def _generate_typescript(
    tool_sources: list[str], builtin_tool_names: list[str], shapes: dict
) -> str:
    """Generate contract.generated.ts."""
    lines = [
        "// DO NOT EDIT — generated from contract/manifest.contract.json by scripts/gen_contract.py",
        "export type ToolSource = " + " | ".join(f"'{s}'" for s in tool_sources) + ";",
        "",
    ]

    # ToolDescriptor interface
    tool_descriptor = shapes.get("ToolDescriptor", {})
    fields = tool_descriptor.get("fields", {})

    lines.append("export interface ToolDescriptor {")
    for field_name, field_def in fields.items():
        ts_type = field_def.get("ts", "unknown")
        required = field_def.get("required", False)
        optional_marker = "" if required else "?"
        lines.append(f"  {field_name}{optional_marker}: {ts_type};")
    lines.append("}")
    lines.append("")

    # ClientCapabilities interface (uses tsName override if present)
    client_manifest = shapes.get("ClientManifest", {})
    client_fields = client_manifest.get("fields", {})
    ts_name = client_manifest.get("tsName", "ClientManifest")

    lines.append(f"export interface {ts_name} {{")
    for field_name, field_def in client_fields.items():
        ts_type = field_def.get("ts", "unknown")
        required = field_def.get("required", False)
        optional_marker = "" if required else "?"
        lines.append(f"  {field_name}{optional_marker}: {ts_type};")
    lines.append("}")
    lines.append("")

    # BUILTIN_TOOL_NAMES constant
    builtin_names_str = ", ".join(f"'{name}'" for name in builtin_tool_names)
    lines.append(f"export const BUILTIN_TOOL_NAMES = [{builtin_names_str}] as const;")

    return "\n".join(lines) + "\n"


def _generate_python(shapes: dict, builtin_tool_names: list[str]) -> str:
    """Generate _contract_generated.py."""
    lines = [
        "# DO NOT EDIT — generated from contract/manifest.contract.json by scripts/gen_contract.py",
        "from __future__ import annotations",
        "",
        "from typing import TypedDict",
        "",
        "",
    ]

    # ToolDescriptor TypedDict
    tool_descriptor = shapes.get("ToolDescriptor", {})
    fields = tool_descriptor.get("fields", {})

    lines.append("class ToolDescriptor(TypedDict, total=False):")
    for field_name, field_def in fields.items():
        py_type = field_def.get("py", "unknown")
        lines.append(f"    {field_name}: {py_type}")
    lines.append("")
    lines.append("")

    # ClientManifest TypedDict
    client_manifest = shapes.get("ClientManifest", {})
    client_fields = client_manifest.get("fields", {})

    lines.append("class ClientManifest(TypedDict, total=False):")
    for field_name, field_def in client_fields.items():
        # Use pyName override if present, else use field_name
        py_field_name = field_def.get("pyName", field_name)
        py_type = field_def.get("py", "unknown")
        lines.append(f"    {py_field_name}: {py_type}")
    lines.append("")
    lines.append("")

    # BUILTIN_TOOL_NAMES constant
    builtin_names_str = ", ".join(f'"{name}"' for name in builtin_tool_names)
    lines.append(f"BUILTIN_TOOL_NAMES = ({builtin_names_str})")

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
