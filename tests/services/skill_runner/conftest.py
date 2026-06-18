import logging
import sys

import pytest


def pytest_configure(config):
    # Register the markers used across this suite.
    config.addinivalue_line("markers", "mocked: runs without real subprocesses or GPU")
    config.addinivalue_line("markers", "live: requires a real subprocess / inference server")


@pytest.fixture(autouse=True)
def _log_to_stderr():
    """Ensure any log output during tests goes to stderr, never stdout."""
    handler = logging.StreamHandler(sys.stderr)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield
    root.removeHandler(handler)


@pytest.fixture
def write_skill(tmp_path):
    """Helper: write a SKILL.md file at <root>/<subdir>/SKILL.md with the given text."""
    def _write(root_name: str, subdir: str, text: str):
        from pathlib import Path

        root = tmp_path / root_name
        skill_dir = root / subdir
        skill_dir.mkdir(parents=True, exist_ok=True)
        md = skill_dir / "SKILL.md"
        md.write_text(text, encoding="utf-8")
        return root, md

    return _write
