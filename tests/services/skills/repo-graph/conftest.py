import sys
from pathlib import Path

import pytest

# make the skill modules importable
sys.path.insert(0, str(Path(__file__).resolve().parents[4]
                       / "services" / "skills" / "repo-graph"))


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    (tmp_path / "helpers.py").write_text(
        "def helper():\n    return 1\n"
    )
    (tmp_path / "main.py").write_text(
        "from helpers import helper\n"
        "\n"
        "def run():\n"
        "    return helper()\n"
    )
    return tmp_path
