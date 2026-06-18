import pytest

from models import GroundingResult, LayoutPlan
from planner import LayoutPlanner


@pytest.mark.mocked
def test_plan_accepts_grounding_json_string(fake_llm):
    grounding_json = GroundingResult(
        elements=[], image_width=800, image_height=600
    ).model_dump_json()
    plan = LayoutPlanner().plan(grounding_json)  # string input (MCP plan tool path)
    assert isinstance(plan, LayoutPlan)
    assert plan.root_component == "flex flex-col min-h-screen"
    assert "#1d4ed8" in plan.color_palette


@pytest.mark.mocked
def test_plan_accepts_grounding_result_object(fake_llm):
    grounding = GroundingResult(elements=[], image_width=800, image_height=600)
    plan = LayoutPlanner().plan(grounding)  # object input (generate() path)
    assert isinstance(plan, LayoutPlan)
    assert plan.sections[0]["name"] == "header"


@pytest.mark.mocked
def test_plan_writes_nothing_to_stdout(fake_llm, capsys):
    LayoutPlanner().plan(GroundingResult(elements=[], image_width=10, image_height=10))
    assert capsys.readouterr().out == ""
