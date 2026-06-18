import sys
from pathlib import Path

import pytest

# make the skill modules importable
sys.path.insert(0, str(Path(__file__).resolve().parents[4]
                       / "services" / "skills" / "repo-fault-localize"))


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    (tmp_path / "config.py").write_text(
        "def parse_config(data):\n"
        "    # BUG: no None check before .get()\n"
        "    return data.get('key')\n"
        "\n"
        "def unrelated_helper():\n"
        "    return 42\n"
    )
    (tmp_path / "utils.py").write_text(
        "def format_path(p):\n"
        "    return str(p)\n"
    )
    return tmp_path


@pytest.fixture
def patch_gemma(monkeypatch):
    """Patch the single LLM entry point. Pass a dict mapping a prompt-substring
    to the canned JSON-array response; first match wins."""
    import localizer

    def install(responses: dict[str, str]):
        def fake(self, prompt: str) -> str:
            for needle, resp in responses.items():
                if needle in prompt:
                    return resp
            return "[]"
        monkeypatch.setattr(localizer.FaultLocalizer, "_call_gemma", fake)

    return install
