import pytest

from grounder import UIGrounder
from models import GroundingResult


@pytest.mark.mocked
def test_ground_encodes_image_as_base64_in_vision_message(sample_image, fake_llm):
    UIGrounder().ground(sample_image)
    # The grounding call must carry a base64 PNG data URL in the vision content.
    vision_call = fake_llm[0]
    content = vision_call["messages"][-1]["content"]
    assert isinstance(content, list)
    image_parts = [p for p in content if p.get("type") == "image_url"]
    assert image_parts, "no image_url part sent to the vision model"
    url = image_parts[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert len(url) > len("data:image/png;base64,")  # actual payload present


@pytest.mark.mocked
def test_ground_parses_llm_json_into_typed_element_tree(sample_image, fake_llm):
    result = UIGrounder().ground(sample_image)
    assert isinstance(result, GroundingResult)
    assert result.image_width == 1440 and result.image_height == 900  # measured, not guessed
    assert result.elements[0].label == "header"
    # nested child parsed into UIElement
    child = result.elements[0].children[0]
    assert child.label == "button"
    assert child.bounds.width == 120


@pytest.mark.mocked
def test_ground_raises_on_missing_image(fake_llm):
    with pytest.raises(FileNotFoundError):
        UIGrounder().ground("/does/not/exist.png")


@pytest.mark.mocked
def test_ground_writes_nothing_to_stdout(sample_image, fake_llm, capsys):
    UIGrounder().ground(sample_image)
    assert capsys.readouterr().out == ""
