import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[4] / "services" / "skills" / "ast-search"
sys.path.insert(0, str(SKILL_DIR))


@pytest.fixture
def py_file(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text(
        "import requests\n"
        "\n"
        "def fetch(u):\n"
        "    r = requests.get(u)\n"
        "    other = requests.get('https://example.com')\n"
        "    note = \"call requests.get(here) in a string\"\n"
        "    return r, other, note\n",
        encoding="utf-8",
    )
    return f


@pytest.fixture
def ts_file(tmp_path):
    f = tmp_path / "sample.ts"
    f.write_text(
        "const a = foo(1);\n"
        "const b = foo(2, 3);\n"
        "const label = 'foo(99) inside string';\n",
        encoding="utf-8",
    )
    return f


@pytest.fixture
def py_dir(tmp_path):
    (tmp_path / "a.py").write_text("x = requests.get(1)\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = requests.get(2)\n", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("requests.get(3)\n", encoding="utf-8")
    return tmp_path
