"""Shared fixtures: a mocked Docker SDK so tests need no Docker daemon.

Since the skill directory is named 'code-sandbox' (hyphenated), it cannot be
imported as a Python package directly. We add the skill directory to sys.path
so modules can be imported by their bare names (e.g., `from executor import DockerExecutor`).
"""
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add the skill directory to sys.path for direct module imports.
SKILL_DIR = Path(__file__).parent.parent.parent.parent.parent / "services" / "skills" / "code-sandbox"
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))


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
