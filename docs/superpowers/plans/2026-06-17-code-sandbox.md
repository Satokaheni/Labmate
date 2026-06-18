# code-sandbox MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the code-sandbox Python MCP server providing Docker-isolated code execution with resource limits — a prerequisite for test-gen and the critique skill's test-running.

**Architecture:** DockerExecutor uses the Docker Python SDK to spin up ephemeral containers with security defaults (network disabled, 512MB memory limit, 50% CPU, read-only filesystem + /tmp tmpfs, non-root user). The MCP server exposes run_python, run_shell, run_tests, and install_packages tools. Each execution is in a fresh container that is removed after completion. All logging goes to stderr.

**Tech Stack:** Python 3.11+, `mcp` SDK, `docker>=7.0` (Docker SDK), `pydantic>=2`, `pytest`

---

## Phase 0: Scaffolding

### Task 1: Create directory structure and requirements.txt

- [ ] Create `services/skills/code-sandbox/` and `tests/services/skills/code-sandbox/`
- [ ] Create `services/skills/code-sandbox/requirements.txt`:

```
mcp>=1.0
docker>=7.0
pydantic>=2
```

### Task 2: Write sandbox_config.py

- [ ] Create `services/skills/code-sandbox/sandbox_config.py`:

```python
"""Resource limits, allowed images, and timeout defaults for the code sandbox.

All values are conservative security defaults. Override SANDBOX_IMAGE via env.
"""
import os

# Base image for sandbox containers. Must contain a Python interpreter.
SANDBOX_IMAGE: str = os.getenv("SANDBOX_IMAGE", "python:3.11-slim")

# Memory ceiling per container. Docker kills the container if exceeded (OOM).
MEM_LIMIT: str = "512m"

# CPU quota in microseconds per 100ms period (cpu_period default 100000).
# 50000 = 50% of a single core.
CPU_QUOTA: int = 50000
CPU_PERIOD: int = 100000

# Non-root user inside the container. "nobody" exists in python:3.11-slim.
CONTAINER_USER: str = "nobody"

# Writable tmpfs mount so read-only rootfs can still scratch to /tmp.
# 64MB cap prevents tmpfs from consuming host memory.
TMPFS: dict[str, str] = {"/tmp": "rw,size=64m,mode=1777"}

# Default timeouts (seconds).
DEFAULT_TIMEOUT: int = 30
DEFAULT_TEST_TIMEOUT: int = 120

# Process count limit (pids) to block fork bombs.
PIDS_LIMIT: int = 128

# Working directory inside the container (writable via tmpfs).
WORKDIR: str = "/tmp"
```

---

## Phase 1: DockerExecutor (TDD)

> Write the test first for each method, watch it fail, then implement. Use superpowers:test-driven-development.

### Task 3: Write conftest.py with docker SDK mock fixtures

- [ ] Create `tests/services/skills/code-sandbox/conftest.py`:

```python
"""Shared fixtures: a mocked Docker SDK so tests need no Docker daemon."""
from unittest.mock import MagicMock
import pytest


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
    from services.skills.code_sandbox.executor import DockerExecutor
    return DockerExecutor(), mock_docker_client, mock_docker_client.containers.create.return_value
```

Note: import path uses the package layout under test. If the repo runs tests with `rootdir` at project root and `services/skills/code-sandbox/` is not an importable package (hyphen in name), add the `tests/.../conftest.py` `sys.path` shim below or import the module by file path. Confirm the existing skill test convention before finalizing — check how `ast-repo-map` tests import their module.

### Task 4: Write test_executor.py — security options test (failing)

- [ ] Create `tests/services/skills/code-sandbox/test_executor.py` with the first test:

```python
import json
import pytest
from unittest.mock import MagicMock

pytestmark = pytest.mark.mocked


def test_run_python_passes_security_options(patched_executor):
    executor, client, container = patched_executor
    executor.run_python("print('hi')")

    _, kwargs = client.containers.create.call_args
    assert kwargs["network_disabled"] is True
    assert kwargs["mem_limit"] == "512m"
    assert kwargs["cpu_quota"] == 50000
    assert kwargs["read_only"] is True
    assert kwargs["user"] == "nobody"
    assert "/tmp" in kwargs["tmpfs"]
    assert kwargs["pids_limit"] == 128
```

- [ ] Run it, confirm it fails (no executor module yet).

### Task 5: Implement executor.py to pass the security test

- [ ] Create `services/skills/code-sandbox/executor.py`:

```python
"""DockerExecutor: run agent-generated code in locked-down ephemeral containers.

Security model:
  - network disabled by default
  - memory + CPU + pids capped
  - read-only rootfs, writable /tmp tmpfs only
  - runs as non-root (nobody)
  - container always removed in finally
"""
import sys
import time
import logging

import docker
from docker.errors import NotFound
from pydantic import BaseModel

from . import sandbox_config as cfg

# Logger wired to stderr — stdout is reserved for JSON-RPC.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("code-sandbox.executor")


class ExecutionResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool = False


class TestResult(BaseModel):
    passed: int
    failed: int
    errors: int
    duration_ms: int
    output: str
    timed_out: bool = False


class DockerExecutor:
    def __init__(self, image: str = cfg.SANDBOX_IMAGE):
        self.image = image
        self.client = docker.from_env()

    def _create_kwargs(self, command: list[str], network_disabled: bool) -> dict:
        return {
            "image": self.image,
            "command": command,
            "network_disabled": network_disabled,
            "mem_limit": cfg.MEM_LIMIT,
            "cpu_quota": cfg.CPU_QUOTA,
            "cpu_period": cfg.CPU_PERIOD,
            "pids_limit": cfg.PIDS_LIMIT,
            "read_only": True,
            "tmpfs": cfg.TMPFS,
            "user": cfg.CONTAINER_USER,
            "working_dir": cfg.WORKDIR,
            "stdin_open": False,
            "tty": False,
            "detach": True,
        }

    def _run_in_container(
        self,
        cmd: list[str],
        code_or_script: str,
        timeout: int,
        network_disabled: bool = True,
    ) -> ExecutionResult:
        """Create, start, wait (with timeout), collect output, always remove."""
        start = time.monotonic()
        container = None
        timed_out = False
        try:
            container = self.client.containers.create(
                **self._create_kwargs(cmd, network_disabled)
            )
            container.start()
            try:
                result = container.wait(timeout=timeout)
                exit_code = result.get("StatusCode", -1)
            except Exception:  # docker raises requests ReadTimeout on wait timeout
                timed_out = True
                exit_code = -1
                try:
                    container.kill()
                except Exception:
                    pass

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except NotFound:
                    pass
                except Exception as e:  # never let cleanup failure mask the result
                    logger.error("container removal failed: %s", e)

        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    def run_python(
        self, code: str, timeout: int = cfg.DEFAULT_TIMEOUT, packages: list[str] = []
    ) -> ExecutionResult:
        # Pass code via -c to avoid writing files to the read-only rootfs.
        script = code
        if packages:
            # pip install needs network; only enable when packages requested.
            pip = "import subprocess,sys;subprocess.run([sys.executable,'-m','pip','install','--quiet',%r],check=True)" % packages
            script = (
                "import subprocess,sys\n"
                f"subprocess.run([sys.executable,'-m','pip','install','--quiet',*{packages!r}],check=True)\n"
                + code
            )
            return self._run_in_container(
                ["python", "-c", script], script, timeout, network_disabled=False
            )
        return self._run_in_container(["python", "-c", script], script, timeout)

    def run_shell(self, cmd: str, timeout: int = cfg.DEFAULT_TIMEOUT) -> ExecutionResult:
        # Shell mode keeps network disabled by default (override only if a future
        # config flag is added). Use sh -c to evaluate the command string.
        return self._run_in_container(["sh", "-c", cmd], cmd, timeout)

    def run_tests(
        self,
        test_path: str,
        framework: str = "pytest",
        timeout: int = cfg.DEFAULT_TEST_TIMEOUT,
    ) -> TestResult:
        if framework != "pytest":
            raise ValueError(f"unsupported framework: {framework}")
        cmd = ["python", "-m", "pytest", test_path, "-q", "--no-header"]
        exec_result = self._run_in_container(cmd, "", timeout)
        passed, failed, errors = _parse_pytest(exec_result.stdout + exec_result.stderr)
        return TestResult(
            passed=passed,
            failed=failed,
            errors=errors,
            duration_ms=exec_result.duration_ms,
            output=exec_result.stdout + exec_result.stderr,
            timed_out=exec_result.timed_out,
        )


def _parse_pytest(output: str) -> tuple[int, int, int]:
    """Parse the pytest summary line, e.g. '2 passed, 1 failed, 1 error in 0.1s'."""
    import re

    def grab(word: str) -> int:
        m = re.search(rf"(\d+)\s+{word}", output)
        return int(m.group(1)) if m else 0

    return grab("passed"), grab("failed"), grab("error")
```

- [ ] Run the security test, confirm green.

### Task 6: Test + verify timeout handling

- [ ] Add to `test_executor.py`:

```python
def test_run_python_times_out(patched_executor):
    executor, client, container = patched_executor
    container.wait.side_effect = Exception("ReadTimeout")
    result = executor.run_python("while True: pass", timeout=1)
    assert result.timed_out is True
    assert result.exit_code == -1
    container.kill.assert_called_once()
```

- [ ] Run, confirm green against the implementation above.

### Task 7: Test + verify exit_code capture

- [ ] Add:

```python
def test_run_python_captures_exit_code(patched_executor):
    executor, client, container = patched_executor
    container.wait.return_value = {"StatusCode": 3}
    result = executor.run_python("import sys; sys.exit(3)")
    assert result.exit_code == 3
    assert result.timed_out is False
```

### Task 8: Test + verify stdout/stderr separation

- [ ] Add:

```python
def test_run_python_separates_streams(patched_executor):
    executor, client, container = patched_executor
    def logs(stdout=True, stderr=False):
        return b"OUT" if stdout else b"ERR"
    container.logs.side_effect = logs
    result = executor.run_python("print('OUT')")
    assert result.stdout == "OUT"
    assert result.stderr == "ERR"
```

### Task 9: Test + verify container always removed

- [ ] Add:

```python
def test_container_removed_on_success(patched_executor):
    executor, client, container = patched_executor
    executor.run_python("print('hi')")
    container.remove.assert_called_once_with(force=True)


def test_container_removed_on_error(patched_executor):
    executor, client, container = patched_executor
    container.start.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        executor.run_python("print('hi')")
    container.remove.assert_called_once_with(force=True)
```

### Task 10: Test + verify run_tests parses pytest output

- [ ] Add:

```python
def test_run_tests_parses_counts(patched_executor):
    executor, client, container = patched_executor
    container.logs.side_effect = lambda stdout=True, stderr=False: (
        b"... 2 passed, 1 failed, 1 error in 0.12s" if stdout else b""
    )
    result = executor.run_tests("tests/")
    assert result.passed == 2
    assert result.failed == 1
    assert result.errors == 1


def test_run_tests_rejects_unknown_framework(patched_executor):
    executor, _, _ = patched_executor
    with pytest.raises(ValueError):
        executor.run_tests("tests/", framework="unittest")
```

### Task 11: Test + verify install-packages enables network

- [ ] Add:

```python
def test_run_python_with_packages_enables_network(patched_executor):
    executor, client, container = patched_executor
    executor.run_python("import requests", packages=["requests"])
    _, kwargs = client.containers.create.call_args
    assert kwargs["network_disabled"] is False


def test_run_python_no_packages_disables_network(patched_executor):
    executor, client, container = patched_executor
    executor.run_python("print('hi')")
    _, kwargs = client.containers.create.call_args
    assert kwargs["network_disabled"] is True
```

---

## Phase 2: MCP server

### Task 12: Implement server.py

- [ ] Create `services/skills/code-sandbox/server.py`:

```python
"""MCP server entry point for the code-sandbox skill.

Exposes run_python, run_shell, run_tests, install_packages over stdio.
CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
import sys
import json
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .executor import DockerExecutor

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("code-sandbox.server")

app = Server("code-sandbox")
_executor: DockerExecutor | None = None


def get_executor() -> DockerExecutor:
    global _executor
    if _executor is None:
        _executor = DockerExecutor()
    return _executor


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="code_sandbox.run_python",
            description="Execute Python code in an isolated container. Returns JSON "
            "with stdout, stderr, exit_code, duration_ms, timed_out.",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="code_sandbox.run_shell",
            description="Execute a shell command in an isolated container. Returns JSON "
            "with stdout, stderr, exit_code, duration_ms, timed_out.",
            inputSchema={
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                },
                "required": ["cmd"],
            },
        ),
        Tool(
            name="code_sandbox.run_tests",
            description="Run a test suite in an isolated container. Returns JSON with "
            "passed, failed, errors, duration_ms, output, timed_out.",
            inputSchema={
                "type": "object",
                "properties": {
                    "test_path": {"type": "string"},
                    "framework": {"type": "string", "default": "pytest"},
                    "timeout": {"type": "integer", "default": 120},
                },
                "required": ["test_path"],
            },
        ),
        Tool(
            name="code_sandbox.install_packages",
            description="Install Python packages into a throwaway sandbox to verify "
            "they resolve. Returns JSON execution result.",
            inputSchema={
                "type": "object",
                "properties": {
                    "packages": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["packages"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    executor = get_executor()
    try:
        if name == "code_sandbox.run_python":
            result = executor.run_python(
                arguments["code"],
                timeout=arguments.get("timeout", 30),
                packages=arguments.get("packages", []),
            )
        elif name == "code_sandbox.run_shell":
            result = executor.run_shell(
                arguments["cmd"], timeout=arguments.get("timeout", 30)
            )
        elif name == "code_sandbox.run_tests":
            result = executor.run_tests(
                arguments["test_path"],
                framework=arguments.get("framework", "pytest"),
                timeout=arguments.get("timeout", 120),
            )
        elif name == "code_sandbox.install_packages":
            result = executor.run_python(
                "print('packages installed')",
                packages=arguments["packages"],
            )
        else:
            raise ValueError(f"unknown tool: {name}")
        return [TextContent(type="text", text=result.model_dump_json())]
    except Exception as e:
        logger.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
```

### Task 13: Add a server-level smoke test

- [ ] Add to `test_executor.py` (or a new `test_server.py`):

```python
@pytest.mark.asyncio
async def test_call_tool_returns_json(monkeypatch):
    import services.skills.code_sandbox.server as srv
    from services.skills.code_sandbox.executor import ExecutionResult

    class FakeExec:
        def run_python(self, code, timeout=30, packages=[]):
            return ExecutionResult(
                stdout="hi", stderr="", exit_code=0, duration_ms=5
            )

    monkeypatch.setattr(srv, "get_executor", lambda: FakeExec())
    out = await srv.call_tool("code_sandbox.run_python", {"code": "print('hi')"})
    payload = json.loads(out[0].text)
    assert payload["stdout"] == "hi"
    assert payload["exit_code"] == 0
```

---

## Phase 3: Skill metadata

### Task 14: Write SKILL.md

- [ ] Create `services/skills/code-sandbox/SKILL.md`:

```markdown
---
name: code-sandbox
description: >
  Isolated, ephemeral code execution environment for running agent-generated code safely.
  Use when you need to execute Python code, run shell commands, or run a test suite in
  a sandboxed environment with resource limits. Prerequisite for test-gen skill.
  Docker-based isolation with network disabled, memory limited, read-only filesystem.
trigger: "Use when executing code that has not been human-reviewed"
tools:
  - code_sandbox.run_python
  - code_sandbox.run_shell
  - code_sandbox.run_tests
  - code_sandbox.install_packages
version: "0.1.0"
license: MIT
requires: []
---

# code-sandbox

Docker-isolated, ephemeral code execution for agent-generated code.

## Security defaults

Every execution runs in a fresh container that is destroyed afterward, with:

- `network_disabled=True` (enabled only when `packages` are requested for pip install)
- `mem_limit="512m"`
- `cpu_quota=50000` / `cpu_period=100000` (50% of one core)
- `read_only=True` rootfs with a writable `/tmp` tmpfs (64MB)
- `user="nobody"` (never root)
- `pids_limit=128` (fork-bomb guard)

## Tools

- `run_python(code, timeout=30, packages=[])` -> JSON `{stdout, stderr, exit_code, duration_ms, timed_out}`
- `run_shell(cmd, timeout=30)` -> same shape
- `run_tests(test_path, framework="pytest", timeout=120)` -> JSON `{passed, failed, errors, duration_ms, output, timed_out}`
- `install_packages(packages)` -> JSON execution result (verifies packages resolve)

## Environment

- `SANDBOX_IMAGE` (default `python:3.11-slim`)

## Phase 2 (future, not implemented here)

MicroVM isolation via Daytona or self-hosted E2B for stronger kernel-level
isolation than Docker namespaces provide. See "MicroVM migration" below.
```

---

## Phase 4: Verification

### Task 15: Run the full mocked test suite

- [ ] `cd` to project root and run:

```bash
python -m pytest tests/services/skills/code-sandbox/ -v -m mocked
```

- [ ] Confirm all tests pass with no Docker daemon required.

### Task 16: Manual live smoke test (requires Docker)

- [ ] With Docker running, in a Python REPL:

```python
from services.skills.code_sandbox.executor import DockerExecutor
ex = DockerExecutor()
print(ex.run_python("print(2 + 2)").model_dump())  # exit_code 0, stdout "4\n"
print(ex.run_python("while True: pass", timeout=2).model_dump())  # timed_out True
print(ex.run_shell("echo hello").model_dump())  # stdout "hello\n"
```

- [ ] Confirm the timeout case returns `timed_out=True` and the container is gone (`docker ps -a` shows no leftover).

---

## MicroVM migration (Phase 2 — documented, NOT implemented in this plan)

When Docker namespace isolation is insufficient (running fully untrusted code, or
multi-tenant), migrate the isolation backend behind the same `DockerExecutor`
interface:

1. Define an `Executor` protocol matching the current `DockerExecutor` method
   signatures so callers (test-gen, critique) are unaffected.
2. Add `MicroVMExecutor` using one of:
   - **Daytona** sandboxes (managed/self-hostable, sub-second VM start)
   - **Self-hosted E2B** (Firecracker microVMs, requires KVM-capable host)
3. Select the backend via `SANDBOX_BACKEND=docker|microvm` env var in `sandbox_config.py`.
4. Keep the same `ExecutionResult` / `TestResult` return types — only the
   isolation mechanism changes.

This plan intentionally implements only the Docker backend.

---

## Notes / open items for the implementer

- Confirm the existing skill-test import convention (hyphenated dir `code-sandbox`
  is not a valid Python package name). Either add an `__init__.py`-friendly
  alias, import by file path, or follow whatever `ast-repo-map` tests do. Adjust
  the `from services.skills.code_sandbox...` imports accordingly.
- `container.wait(timeout=...)` raises a `requests` `ReadTimeout` (not a docker
  error) when the timeout elapses — the broad `except Exception` in
  `_run_in_container` handles this; tighten to the exact exception once confirmed
  against `docker>=7.0`.
- `install_packages` currently runs a no-op script with the packages to force a
  pip install; consider returning structured success/failure per package in a
  later iteration.
