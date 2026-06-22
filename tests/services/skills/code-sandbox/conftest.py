"""Shared fixtures: a mocked Docker SDK so tests need no Docker daemon.

Since the skill directory is named 'code-sandbox' (hyphenated), it cannot be
imported as a Python package directly. Other skills also ship a bare `server.py`
and `executor.py`, so a shared pytest session can leave a SIBLING skill's module
cached under the bare name `server`/`executor`, and a plain `import server` here
would then bind to the wrong skill.

To make this deterministic, we EXPLICITLY load THIS skill's executor.py and
server.py from their file paths into sys.modules (under the bare names) at conftest
import time — which runs immediately before this directory's test files are
collected. The test files' `import server` / `from executor import ...` then
resolve to the code-sandbox modules regardless of collection order.
"""
import sys
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SKILL_DIR = Path(__file__).parent.parent.parent.parent.parent / "services" / "skills" / "code-sandbox"
# Kept as a fallback for any intra-skill relative imports.
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))


def _load_skill_module(name: str):
    """Load <SKILL_DIR>/<name>.py into sys.modules[name], overwriting any stale
    sibling-skill module cached under the same bare name."""
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec so server.py's `from executor import ...` resolves to THIS one.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Order matters: executor first (server.py imports from it).
_load_skill_module("executor")
_load_skill_module("server")


@pytest.fixture
def mock_container():
    """A fake container that exits 0 with empty logs by default."""
    container = MagicMock()
    container.wait.return_value = {"StatusCode": 0}
    container.logs.return_value = b""
    return container


@pytest.fixture
def mock_docker_client(mock_container):
    """A fake docker client whose containers.create() returns mock_container."""
    client = MagicMock()
    client.containers.create.return_value = mock_container
    return client


@pytest.fixture
def patched_executor(mock_docker_client, monkeypatch):
    """DockerExecutor with docker.from_env() patched to the mock client."""
    import docker
    monkeypatch.setattr(docker, "from_env", lambda: mock_docker_client)
    from executor import DockerExecutor
    return DockerExecutor(), mock_docker_client, mock_docker_client.containers.create.return_value


@pytest.fixture
def local_executor():
    """LocalSubprocessExecutor instance for testing."""
    from executor import LocalSubprocessExecutor
    return LocalSubprocessExecutor()
