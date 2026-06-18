import pytest

from generator import ComponentGenerator
from models import GenerationResult, LayoutPlan


def _plan() -> LayoutPlan:
    return LayoutPlan(root_component="flex", sections=[], color_palette=[], typography_notes="")


@pytest.mark.mocked
def test_generate_returns_component_code(fake_llm):
    res = ComponentGenerator().generate(_plan(), framework="react-tailwind")
    assert isinstance(res, GenerationResult)
    assert "export default" in res.component_code
    assert res.framework == "react-tailwind"
    assert res.output_path is None


@pytest.mark.mocked
def test_generate_rejects_unknown_framework(fake_llm):
    with pytest.raises(ValueError):
        ComponentGenerator().generate(_plan(), framework="svelte-runes")


@pytest.mark.mocked
def test_generate_writes_output_path_when_provided(tmp_path, fake_llm):
    out = tmp_path / "nested" / "App.tsx"
    res = ComponentGenerator().generate(_plan(), output_path=str(out))
    assert out.is_file()
    assert out.read_text(encoding="utf-8") == res.component_code


@pytest.mark.mocked
def test_generate_strips_code_fences(fake_llm, monkeypatch):
    import generator
    monkeypatch.setattr(
        generator, "call_llm",
        lambda *a, **k: "```tsx\nexport default function A(){return null}\n```",
    )
    res = ComponentGenerator().generate(_plan())
    assert not res.component_code.startswith("```")
    assert res.component_code.startswith("export default")


@pytest.mark.mocked
def test_generate_writes_nothing_to_stdout(fake_llm, capsys):
    ComponentGenerator().generate(_plan())
    assert capsys.readouterr().out == ""
