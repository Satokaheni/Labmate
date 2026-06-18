import pytest


@pytest.mark.mocked
def test_missing_token_raises_clear_error(monkeypatch):
    monkeypatch.delenv("FIGMA_ACCESS_TOKEN", raising=False)
    from figma_fetcher import FigmaFetcher, FigmaTokenMissingError
    with pytest.raises(FigmaTokenMissingError) as exc:
        FigmaFetcher()
    msg = str(exc.value)
    assert "FIGMA_ACCESS_TOKEN" in msg
    assert "token" in msg.lower()


@pytest.mark.mocked
def test_parse_node_maps_types_and_layout(monkeypatch, raw_node_document):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    from figma_fetcher import FigmaFetcher
    fetcher = FigmaFetcher()
    node = fetcher._parse_node(raw_node_document)
    assert node.type == "FRAME"
    assert node.name == "Primary Card"
    assert node.layout["direction"] == "VERTICAL"
    assert node.layout["gap"] == 8
    assert node.layout["padding"]["top"] == 16
    assert [c.type for c in node.children] == ["TEXT", "RECTANGLE"]
    text_child = node.children[0]
    assert text_child.text_style is not None
    assert text_child.text_style["font_size"] == 18
    assert node.children[1].text_style is None
    assert node.fills and node.fills[0]["type"] == "SOLID"
    assert node.variables["fills"]["id"] == "VariableID:9:9"


@pytest.mark.mocked
def test_parse_node_component_type(monkeypatch):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    from figma_fetcher import FigmaFetcher
    node = FigmaFetcher()._parse_node({"id": "x", "name": "Btn", "type": "COMPONENT"})
    assert node.type == "COMPONENT"
    assert node.layout == {}


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_get_node_builds_spec(monkeypatch, nodes_response):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    import figma_fetcher

    class _FakeResp:
        def raise_for_status(self): pass
        def json(self): return nodes_response

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None, params=None):
            assert headers["X-Figma-Token"] == "tok"
            assert params["ids"] == "1:2"
            return _FakeResp()

    monkeypatch.setattr(figma_fetcher.httpx, "AsyncClient", _FakeClient)
    fetcher = figma_fetcher.FigmaFetcher()
    spec = await fetcher.get_node("FILEKEY", "1:2")
    assert spec.file_key == "FILEKEY"
    assert spec.node_id == "1:2"
    assert spec.node.type == "FRAME"
    assert "VariableID:9:9" in spec.tokens
