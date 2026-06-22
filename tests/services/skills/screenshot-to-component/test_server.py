import json

import pytest


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_list_tools_exposes_three_bare_tools():
    import server
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "generate",
        "ground",
        "plan",
    }


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_generate_returns_json(sample_image, fake_llm):
    import server
    server.pipeline = None  # force fresh construction
    out = await server.call_tool(
        "generate", {"image_path": sample_image}
    )
    payload = json.loads(out[0].text)
    assert set(payload) == {"component_code", "layout_plan", "output_path"}


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_unknown_returns_error_content_not_raise(fake_llm):
    import server
    out = await server.call_tool("bogus", {})
    assert "unknown tool" in out[0].text


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_writes_nothing_to_stdout(sample_image, fake_llm, capsys):
    import server
    server.pipeline = None
    await server.call_tool("ground", {"image_path": sample_image})
    assert capsys.readouterr().out == ""
