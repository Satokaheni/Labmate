import sys
from pathlib import Path

import pytest

SERVER_DIR = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "figma-to-component"
)
sys.path.insert(0, str(SERVER_DIR))


@pytest.fixture
def raw_node_document() -> dict:
    return {
        "id": "1:2",
        "name": "Primary Card",
        "type": "FRAME",
        "layoutMode": "VERTICAL",
        "itemSpacing": 8,
        "paddingTop": 16,
        "paddingRight": 16,
        "paddingBottom": 16,
        "paddingLeft": 16,
        "counterAxisAlignItems": "CENTER",
        "primaryAxisAlignItems": "MIN",
        "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}],
        "boundVariables": {"fills": {"id": "VariableID:9:9", "type": "VARIABLE_ALIAS"}},
        "children": [
            {
                "id": "1:3",
                "name": "Title",
                "type": "TEXT",
                "characters": "Hello",
                "style": {
                    "fontFamily": "Inter",
                    "fontSize": 18,
                    "fontWeight": 600,
                    "lineHeightPx": 24,
                    "textAlignHorizontal": "LEFT",
                },
                "fills": [{"type": "SOLID", "color": {"r": 0, "g": 0, "b": 0}}],
            },
            {
                "id": "1:4",
                "name": "Divider",
                "type": "RECTANGLE",
                "fills": [{"type": "SOLID", "color": {"r": 0.9, "g": 0.9, "b": 0.9}}],
            },
        ],
    }


@pytest.fixture
def nodes_response(raw_node_document) -> dict:
    return {"nodes": {"1:2": {"document": raw_node_document}}}


@pytest.fixture
def fake_synth_payload() -> str:
    import json
    return json.dumps({
        "component_code": (
            "export function PrimaryCard({ title }: PrimaryCardProps) {\n"
            "  return <div className=\"flex flex-col gap-2 p-4 items-center\">"
            "<span>{title}</span></div>;\n}"
        ),
        "props_interface": "export interface PrimaryCardProps { title: string; }",
    })
