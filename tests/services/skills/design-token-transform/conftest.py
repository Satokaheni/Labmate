import sys
from pathlib import Path

import pytest

SERVER_DIR = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "design-token-transform"
)
sys.path.insert(0, str(SERVER_DIR))


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        return _FakeResponse(self._payload)


@pytest.fixture
def figma_file_response() -> dict:
    """A minimal but realistic GET /files/:key response."""
    return {
        "document": {
            "id": "0:0",
            "name": "Document",
            "children": [
                {
                    "id": "1:1",
                    "name": "Primary",
                    "fills": [
                        {"type": "SOLID", "visible": True,
                         "color": {"r": 1.0, "g": 0.341, "b": 0.2, "a": 1.0}}
                    ],
                },
                {
                    "id": "1:2",
                    "name": "Heading",
                    "style": {"fontSize": 16, "fontFamily": "Inter", "fontWeight": 700},
                },
                {
                    "id": "1:3",
                    "name": "Card",
                    "cornerRadius": 8,
                    "itemSpacing": 12,
                },
            ],
        }
    }


@pytest.fixture
def sample_token_set():
    from figma_client import DesignToken, TokenSet
    return TokenSet(
        source="abc123",
        extracted_at="2026-06-17T00:00:00+00:00",
        tokens=[
            DesignToken(name="Primary", category="color", value="#FF5733"),
            DesignToken(name="Heading", category="typography", value="16px"),
            DesignToken(name="Card", category="radius", value="8px"),
            DesignToken(name="Card", category="spacing", value="12px"),
        ],
    )
