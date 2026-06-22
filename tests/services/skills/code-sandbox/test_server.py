import json
import pytest

pytestmark = pytest.mark.mocked


@pytest.mark.asyncio
async def test_call_tool_returns_json(monkeypatch):
    import server as srv
    from executor import ExecutionResult

    class FakeExec:
        def run_python(self, code, timeout=30, packages=[]):
            return ExecutionResult(
                stdout="hi", stderr="", exit_code=0, duration_ms=5
            )

    monkeypatch.setattr(srv, "get_executor", lambda: FakeExec())
    out = await srv.call_tool("run_python", {"code": "print('hi')"})
    payload = json.loads(out[0].text)
    assert payload["stdout"] == "hi"
    assert payload["exit_code"] == 0


@pytest.mark.asyncio
async def test_call_tool_unknown_tool_returns_error(monkeypatch):
    import server as srv
    from executor import ExecutionResult

    class FakeExec:
        pass

    monkeypatch.setattr(srv, "get_executor", lambda: FakeExec())
    out = await srv.call_tool("nonexistent", {"code": "x"})
    payload = json.loads(out[0].text)
    assert "error" in payload


# Auto-fallback tests


@pytest.mark.mocked
def test_get_executor_fallback_to_local_when_docker_unavailable(monkeypatch):
    """get_executor should fall back to LocalSubprocessExecutor when Docker daemon is down."""
    import os
    import server as srv
    from executor import LocalSubprocessExecutor

    # Reset the singleton
    srv._executor = None

    # Mock docker.from_env to raise
    def mock_from_env():
        raise Exception("Cannot connect to Docker daemon")

    monkeypatch.setattr("server.DockerExecutor", lambda: None)  # never called
    import docker
    monkeypatch.setattr(docker, "from_env", mock_from_env)

    # Ensure CODE_SANDBOX_BACKEND is not set (auto mode)
    monkeypatch.delenv("CODE_SANDBOX_BACKEND", raising=False)

    # Mock the LocalSubprocessExecutor.__init__ to not print warnings to stderr
    # Actually, let it print — that's the point. Just verify type.
    executor = srv.get_executor()
    assert isinstance(executor, LocalSubprocessExecutor)


@pytest.mark.mocked
def test_get_executor_docker_backend_env_var(monkeypatch, mock_docker_client):
    """get_executor with CODE_SANDBOX_BACKEND=docker should use DockerExecutor."""
    import server as srv
    from executor import DockerExecutor

    # Reset the singleton
    srv._executor = None

    # Set backend to docker
    monkeypatch.setenv("CODE_SANDBOX_BACKEND", "docker")

    # Mock docker.from_env
    import docker
    monkeypatch.setattr(docker, "from_env", lambda: mock_docker_client)

    executor = srv.get_executor()
    assert isinstance(executor, DockerExecutor)


@pytest.mark.mocked
def test_get_executor_local_backend_env_var(monkeypatch):
    """get_executor with CODE_SANDBOX_BACKEND=local should use LocalSubprocessExecutor."""
    import server as srv
    from executor import LocalSubprocessExecutor

    # Reset the singleton
    srv._executor = None

    # Set backend to local
    monkeypatch.setenv("CODE_SANDBOX_BACKEND", "local")

    executor = srv.get_executor()
    assert isinstance(executor, LocalSubprocessExecutor)


@pytest.mark.mocked
def test_get_executor_caches_singleton(monkeypatch):
    """get_executor should cache the executor and return the same instance."""
    import server as srv

    # Reset the singleton
    srv._executor = None

    monkeypatch.setenv("CODE_SANDBOX_BACKEND", "local")

    executor1 = srv.get_executor()
    executor2 = srv.get_executor()

    assert executor1 is executor2
