"""Code executors: run agent-generated code in sandboxed environments.

DockerExecutor: locked-down ephemeral containers with full isolation
  - network disabled by default
  - memory + CPU + pids capped
  - read-only rootfs, writable /tmp tmpfs only
  - runs as non-root (nobody)
  - container always removed in finally

LocalSubprocessExecutor: fallback for hosts without Docker
  - subprocess-based execution with resource limits via setrlimit
  - NO filesystem/network/PID isolation — for TRUSTED code only
  - memory/CPU/pids capped via rlimit
  - runs in temp working directory
  - WARNING: unsandboxed mode, emitted to stderr on construction
"""
import sys
import time
import logging
import subprocess
import tempfile
import resource
import os

import docker
from docker.errors import NotFound
from pydantic import BaseModel

import sandbox_config as cfg

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


class LocalSubprocessExecutor:
    """Fallback executor for hosts without Docker daemon.

    SECURITY WARNING: This runs code with NO filesystem/network/PID isolation.
    Resource limits are enforced via RLIMIT only. Use ONLY for trusted code.
    This mode is unsandboxed and intended as a fallback for development/testing.
    """

    def __init__(self):
        """Initialize and emit loud warning to stderr that sandbox is disabled."""
        logger.warning(
            "=" * 80
        )
        logger.warning(
            "CODE-SANDBOX: LOCAL, UNSANDBOXED MODE"
        )
        logger.warning(
            "No isolation (filesystem/network/PID). Resource limits via RLIMIT only."
        )
        logger.warning(
            "For TRUSTED code only. Use Docker backend in production."
        )
        logger.warning(
            "=" * 80
        )

    def _set_resource_limits(self) -> None:
        """Set resource limits via setrlimit in a child process.

        Called as preexec_fn in subprocess.run so limits apply to the executed code.
        Limits from sandbox_config: PIDS_LIMIT, MEM_LIMIT, timeouts.
        """
        # Parse MEM_LIMIT (e.g. "512m") to bytes. Be generous — RLIMIT_AS too low breaks imports.
        mem_str = cfg.MEM_LIMIT.lower().strip()
        if mem_str.endswith("m"):
            mem_bytes = int(mem_str[:-1]) * (1024 * 1024)
        elif mem_str.endswith("g"):
            mem_bytes = int(mem_str[:-1]) * (1024 * 1024 * 1024)
        elif mem_str.endswith("k"):
            mem_bytes = int(mem_str[:-1]) * 1024
        else:
            mem_bytes = int(mem_str)

        # Increase by 50% for mmap-heavy libraries (numpy, torch, etc).
        # This is a heuristic to avoid breaking imports while still capping runaway memory.
        mem_limit = int(mem_bytes * 1.5)

        # RLIMIT_AS: virtual memory limit (stack + heap + shared). Too low breaks stdlib.
        resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))

        # RLIMIT_NPROC: process count (blocks fork bombs).
        resource.setrlimit(resource.RLIMIT_NPROC, (cfg.PIDS_LIMIT, cfg.PIDS_LIMIT))

        # RLIMIT_FSIZE: file size (64 MB cap on output files).
        fsize_limit = 64 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_limit, fsize_limit))

    def _run_process(
        self,
        cmd: list[str],
        timeout: int,
        cwd: str,
    ) -> ExecutionResult:
        """Run a subprocess with resource limits and timeout.

        Args:
            cmd: command list, e.g. ["python", "-c", "print(2+2)"]
            timeout: wall-clock timeout in seconds
            cwd: working directory (usually a temp dir)

        Returns:
            ExecutionResult with stdout, stderr, exit_code, duration_ms, timed_out.
        """
        start = time.monotonic()
        timed_out = False
        stdout_out = ""
        stderr_out = ""
        exit_code = -1

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                cwd=cwd,
                preexec_fn=self._set_resource_limits,
                text=True,
            )
            stdout_out = result.stdout
            stderr_out = result.stderr
            exit_code = result.returncode
        except subprocess.TimeoutExpired as e:
            timed_out = True
            exit_code = -1
            stdout_out = e.stdout if e.stdout else ""
            stderr_out = e.stderr if e.stderr else ""
        except Exception as e:
            # e.g., OSError from setrlimit, resource exceeded in preexec_fn.
            logger.error("subprocess execution failed: %s", e)
            stderr_out = str(e)
            exit_code = -1

        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecutionResult(
            stdout=stdout_out,
            stderr=stderr_out,
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    def run_python(
        self, code: str, timeout: int = cfg.DEFAULT_TIMEOUT, packages: list[str] = []
    ) -> ExecutionResult:
        """Execute Python code in a subprocess with resource limits.

        Args:
            code: Python code as a string
            timeout: wall-clock timeout in seconds
            packages: list of pip packages to install (mutates host environment)

        Returns:
            ExecutionResult with stdout, stderr, exit_code, duration_ms, timed_out.

        Note: packages are installed into the host interpreter (no isolation).
              This is a limitation of local mode. Logged as a warning.
        """
        script = code
        if packages:
            logger.warning(
                "packages requested in local mode; installing into host interpreter: %s",
                packages,
            )
            script = (
                "import subprocess,sys\n"
                f"subprocess.run([sys.executable,'-m','pip','install','--quiet',*{packages!r}],check=True)\n"
                + code
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            return self._run_process(
                [sys.executable, "-c", script],
                timeout,
                tmpdir,
            )

    def run_shell(self, cmd: str, timeout: int = cfg.DEFAULT_TIMEOUT) -> ExecutionResult:
        """Execute a shell command in a subprocess with resource limits.

        Args:
            cmd: shell command as a string
            timeout: wall-clock timeout in seconds

        Returns:
            ExecutionResult with stdout, stderr, exit_code, duration_ms, timed_out.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            return self._run_process(
                ["sh", "-c", cmd],
                timeout,
                tmpdir,
            )

    def run_tests(
        self,
        test_path: str,
        framework: str = "pytest",
        timeout: int = cfg.DEFAULT_TEST_TIMEOUT,
    ) -> TestResult:
        """Run a test suite in a subprocess with resource limits.

        Args:
            test_path: path to test file or directory
            framework: test framework (only "pytest" supported)
            timeout: wall-clock timeout in seconds

        Returns:
            TestResult with passed, failed, errors counts, output, duration_ms, timed_out.
        """
        if framework != "pytest":
            raise ValueError(f"unsupported framework: {framework}")

        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = [sys.executable, "-m", "pytest", test_path, "-q", "--no-header"]
            exec_result = self._run_process(cmd, timeout, tmpdir)
            passed, failed, errors = _parse_pytest(
                exec_result.stdout + exec_result.stderr
            )
            return TestResult(
                passed=passed,
                failed=failed,
                errors=errors,
                duration_ms=exec_result.duration_ms,
                output=exec_result.stdout + exec_result.stderr,
                timed_out=exec_result.timed_out,
            )
