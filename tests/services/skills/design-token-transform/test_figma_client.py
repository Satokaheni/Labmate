import pytest

from conftest import _FakeAsyncClient, _FakeResponse

pytestmark = [pytest.mark.mocked, pytest.mark.asyncio]


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("FIGMA_ACCESS_TOKEN", raising=False)
    import importlib
    import figma_client
    importlib.reload(figma_client)
    from figma_client import FigmaClient
    with pytest.raises(RuntimeError, match="FIGMA_ACCESS_TOKEN"):
        FigmaClient()


async def test_get_file_tokens_extracts(monkeypatch, figma_file_response):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    import figma_client as fc
    monkeypatch.setattr(
        fc.httpx, "AsyncClient",
        lambda *a, **k: _FakeAsyncClient(figma_file_response),
    )

    client = fc.FigmaClient()
    token_set = await client.get_file_tokens("abc123")

    by_cat = {(t.category, t.name): t.value for t in token_set.tokens}
    assert token_set.source == "abc123"
    assert by_cat[("color", "Primary")] == "#FF5733"
    assert by_cat[("typography", "Heading")] == "16px"
    assert by_cat[("radius", "Card")] == "8px"
    assert by_cat[("spacing", "Card")] == "12px"


def test_rgba_to_hex_opaque(monkeypatch):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    from figma_client import FigmaClient
    assert FigmaClient._rgba_to_hex({"r": 1.0, "g": 0.341, "b": 0.2}, None) == "#FF5733"


def test_rgba_to_hex_with_alpha(monkeypatch):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    from figma_client import FigmaClient
    out = FigmaClient._rgba_to_hex({"r": 0, "g": 0, "b": 0}, 0.5)
    assert out == "#00000080"


def test_empty_node_yields_no_tokens(monkeypatch):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    from figma_client import FigmaClient
    client = FigmaClient()
    assert client._extract_tokens_from_node({"id": "x", "name": "Frame"}) == []
