import pytest


def _spec_from_fixture(raw_node_document, monkeypatch):
    monkeypatch.setenv("FIGMA_ACCESS_TOKEN", "tok")
    import figma_fetcher
    from models import ComponentSpec
    node = figma_fetcher.FigmaFetcher()._parse_node(raw_node_document)
    return ComponentSpec(node=node, file_key="K", node_id="1:2", tokens={})


@pytest.mark.mocked
def test_prompt_includes_auto_layout(monkeypatch, raw_node_document):
    from component_synth import ComponentSynthesizer
    spec = _spec_from_fixture(raw_node_document, monkeypatch)
    prompt = ComponentSynthesizer()._build_prompt(spec, "PrimaryCard")
    assert "VERTICAL" in prompt
    assert "auto-layout" in prompt.lower()
    assert "gap" in prompt.lower()
    assert "PrimaryCard" in prompt


@pytest.mark.mocked
def test_synthesize_returns_result(monkeypatch, raw_node_document, fake_synth_payload):
    import component_synth
    monkeypatch.setattr(
        component_synth.litellm,
        "completion",
        lambda **k: {"choices": [{"message": {"content": fake_synth_payload}}]},
    )
    spec = _spec_from_fixture(raw_node_document, monkeypatch)
    result = component_synth.ComponentSynthesizer().synthesize(spec, "react-tailwind")
    assert result.framework == "react-tailwind"
    assert result.component_name == "PrimaryCard"
    assert "export function PrimaryCard" in result.component_code
    assert "PrimaryCardProps" in result.props_interface


@pytest.mark.mocked
def test_synthesize_rejects_unsupported_framework(monkeypatch, raw_node_document):
    from component_synth import ComponentSynthesizer
    spec = _spec_from_fixture(raw_node_document, monkeypatch)
    with pytest.raises(ValueError):
        ComponentSynthesizer().synthesize(spec, "vue")


@pytest.mark.mocked
def test_synthesize_uses_gemma_base(monkeypatch, raw_node_document, fake_synth_payload):
    import component_synth
    captured = {}
    def _fake(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": fake_synth_payload}}]}
    monkeypatch.setattr(component_synth.litellm, "completion", _fake)
    spec = _spec_from_fixture(raw_node_document, monkeypatch)
    component_synth.ComponentSynthesizer().synthesize(spec, "react-tailwind")
    assert captured["api_base"] == component_synth.GEMMA_BASE
    assert "gemma" in captured["model"].lower()


@pytest.mark.mocked
def test_no_stdout_during_synthesis(monkeypatch, raw_node_document, fake_synth_payload, capsys):
    import component_synth
    monkeypatch.setattr(
        component_synth.litellm,
        "completion",
        lambda **k: {"choices": [{"message": {"content": fake_synth_payload}}]},
    )
    spec = _spec_from_fixture(raw_node_document, monkeypatch)
    component_synth.ComponentSynthesizer().synthesize(spec, "react-tailwind")
    assert capsys.readouterr().out == ""
