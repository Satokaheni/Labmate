import pytest

from conftest import _FakeAsyncClient, _FakeResponse

pytestmark = pytest.mark.mocked


def test_to_css_vars(sample_token_set):
    from transformer import TokenTransformer
    out = TokenTransformer().transform(sample_token_set, "css-vars")
    assert out.startswith(":root {")
    assert out.rstrip().endswith("}")
    assert "--color-primary: #FF5733;" in out
    assert "--font-size-heading: 16px;" in out
    assert "--radius-card: 8px;" in out


def test_to_tailwind(sample_token_set):
    from transformer import TokenTransformer
    out = TokenTransformer().transform(sample_token_set, "tailwind")
    assert "module.exports" in out
    assert "theme: {" in out
    assert "extend: {" in out
    assert "colors: {" in out
    assert '"primary": "#FF5733"' in out
    assert "fontSize: {" in out
    assert '"heading": "16px"' in out
    assert "borderRadius: {" in out


def test_to_shadcn(sample_token_set):
    from transformer import TokenTransformer
    out = TokenTransformer().transform(sample_token_set, "shadcn")
    assert "@layer base {" in out
    assert ":root {" in out
    # #FF5733 -> H S% L%
    assert "--primary: 11 100% 60%;" in out
    assert "--radius: 8px;" in out


def test_hex_to_hsl_known_value():
    from transformer import TokenTransformer
    assert TokenTransformer._hex_to_hsl("#FF5733") == (11, 100, 60)


def test_unknown_format_raises(sample_token_set):
    from transformer import TokenTransformer
    with pytest.raises(ValueError, match="unknown format"):
        TokenTransformer().transform(sample_token_set, "scss")


@pytest.mark.asyncio
async def test_extract_and_transform(monkeypatch, tmp_path, figma_file_response):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")

    # Import server first (before monkeypatching httpx) so that mcp's
    # streamable_http module is fully loaded and its `AsyncClient | None`
    # type annotation is evaluated against the real httpx type.
    import server  # noqa: F401 — must import before patching httpx
    import figma_client as fc

    monkeypatch.setattr(
        fc.httpx, "AsyncClient",
        lambda *a, **k: _FakeAsyncClient(figma_file_response),
    )

    server._client = None  # reset any cached client

    out_file = tmp_path / "tokens.css"
    result = await server.call_tool(
        "design_token.extract_and_transform",
        {"figma_file_key": "abc123", "format": "css-vars",
         "output_path": str(out_file)},
    )
    text = result[0].text
    assert "--color-primary: #FF5733;" in text
    assert out_file.read_text() == text
