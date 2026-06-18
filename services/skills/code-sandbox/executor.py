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
