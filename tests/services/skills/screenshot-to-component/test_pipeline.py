import pytest

from models import GenerationResult, GroundingResult, LayoutPlan
from pipeline import Pipeline


class _SpyGrounder:
    def __init__(self, log): self.log = log
    def ground(self, image_path):
        self.log.append("ground")
        return GroundingResult(elements=[], image_width=1, image_height=1)


class _SpyPlanner:
    def __init__(self, log): self.log = log
    def plan(self, grounding):
        self.log.append("plan")
        return LayoutPlan()


class _SpyGenerator:
    def __init__(self, log): self.log = log
    def generate(self, plan, framework="react-tailwind", output_path=None):
        self.log.append("generate")
        return GenerationResult(
            component_code="CODE", framework=framework, output_path=output_path
        )


@pytest.mark.mocked
def test_generate_chains_stages_in_order():
    log: list[str] = []
    pipe = Pipeline(_SpyGrounder(log), _SpyPlanner(log), _SpyGenerator(log))
    result = pipe.generate("/x.png", framework="react-tailwind", output_path="/out.tsx")
    assert log == ["ground", "plan", "generate"]
    assert set(result) == {"component_code", "layout_plan", "output_path"}
    assert result["component_code"] == "CODE"
    assert result["output_path"] == "/out.tsx"
    assert isinstance(result["layout_plan"], dict)  # LayoutPlan dumped to dict


@pytest.mark.mocked
def test_generate_end_to_end_with_fake_llm(sample_image, fake_llm, tmp_path):
    out = tmp_path / "App.tsx"
    result = Pipeline().generate(sample_image, output_path=str(out))
    # ground -> plan -> generate produced three LLM calls, vision first.
    assert len(fake_llm) == 3
    assert isinstance(fake_llm[0]["messages"][-1]["content"], list)  # vision call first
    assert out.is_file()
    assert result["layout_plan"]["root_component"] == "flex flex-col min-h-screen"
