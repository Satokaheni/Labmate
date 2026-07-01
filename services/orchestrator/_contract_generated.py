# DO NOT EDIT — generated from contract/manifest.contract.json by scripts/gen_contract.py
from __future__ import annotations

from typing import TypedDict


class ToolDescriptor(TypedDict, total=False):
    name: str
    source: str
    namespace: str | None
    schema: dict | None
    description: str
    body: str


class ClientManifest(TypedDict, total=False):
    protocol_version: int
    tools: list[ToolDescriptor]


BUILTIN_TOOL_NAMES = ("read_file", "write_file", "list_dir", "search_files", "run_tests")
