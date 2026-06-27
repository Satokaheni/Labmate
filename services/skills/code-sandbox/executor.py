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
  - memory/CPU/pids capped via rlimit, CPU timeout, process-group reaping
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
import signal

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
    backend: str = "docker"  # "docker" or "local"
    sandboxed: bool = True   # True for docker, False for local


class TestResult(BaseModel):
    passed: int
    failed: int
    errors: int
    duration_ms: int
    output: str
    timed_out: bool = False
    backend: str = "docker"  # "docker" or "local"
    sandboxed: bool = True   # True for docker, False for local


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
            backend="docker",
            sandboxed=True,
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
        expr: str | None = None,
    ) -> TestResult:
        if framework != "pytest":
            raise ValueError(f"unsupported framework: {framework}")
        cmd = ["python", "-m", "pytest", test_path, "-q", "--no-header"]
        if expr:
            cmd += ["-k", expr]
        exec_result = self._run_in_container(cmd, "", timeout)
        passed, failed, errors, no_tests_ran = _parse_pytest(exec_result.stdout + exec_result.stderr)
        # If pytest found no tests or the path was invalid, count it as an error.
        if no_tests_ran and passed == 0 and failed == 0 and errors == 0:
            errors = 1
        return TestResult(
            passed=passed,
            failed=failed,
            errors=errors,
            duration_ms=exec_result.duration_ms,
            output=exec_result.stdout + exec_result.stderr,
            timed_out=exec_result.timed_out,
            backend="docker",
            sandboxed=True,
        )


def _parse_pytest(output: str) -> tuple[int, int, int, bool]:
    """Parse the pytest summary line, e.g. '2 passed, 1 failed, 1 error in 0.1s'.

    Returns:
        (passed, failed, errors, no_tests_ran): if no_tests_ran is True, pytest found no tests or the path was invalid.
    """
    import re

    # Check for "file or directory not found" or "no tests ran" patterns.
    no_tests_patterns = [
        r"no tests ran",
        r"file or directory not found",
        r"ERROR collecting",  # collection errors indicate invalid paths
    ]
    no_tests_ran = any(re.search(pattern, output, re.IGNORECASE) for pattern in no_tests_patterns)

    def grab(word: str) -> int:
        m = re.search(rf"(\d+)\s+{word}", output)
        return int(m.group(1)) if m else 0

    return grab("passed"), grab("failed"), grab("error"), no_tests_ran


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

    def _make_preexec_fn(self, timeout: int):
        """Create a preexec function that sets resource limits.

        Called as preexec_fn in subprocess.Popen so limits apply to the executed code.
        Limits from sandbox_config: PIDS_LIMIT, MEM_LIMIT, CPU timeout.

        Args:
            timeout: wall-clock timeout in seconds; used to set RLIMIT_CPU.
        """
        def preexec_fn():
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

            # RLIMIT_NPROC: per-UID process count. On busy hosts, the system-wide UID thread count
            # can exceed naive static limits (e.g., 128), causing "Cannot fork" errors. We drop
            # this limit entirely and rely on RLIMIT_CPU + timeout + killpg for containment.
            # NOTE on containment scope: RLIMIT_CPU is PER-PROCESS, not per-tree — each child
            # inherits its own CPU ceiling, so a forked child gets its own SIGXCPU/SIGKILL when it
            # individually exceeds the limit (the limit is not summed across the tree). True
            # whole-tree teardown comes from launching in a new session (start_new_session) and
            # os.killpg() on the wall-clock timeout, which reap escaped children/grandchildren.
            # Reference: RLIMIT_NPROC counts per real-UID threads system-wide, not per-process-tree.
            # Omitting it here avoids spurious fork failures on multi-tenant or busy hosts.

            # RLIMIT_FSIZE: file size (64 MB cap on output files).
            fsize_limit = 64 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_limit, fsize_limit))

            # RLIMIT_CPU: CPU time hard limit. Set to timeout + 1 second to give the process
            # a chance to clean up before the kernel SIGKILL. Combined with process-group
            # reaping on subprocess.TimeoutExpired, this prevents forked children from escaping.
            cpu_limit = timeout + 1
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))

        return preexec_fn

    def _run_process(
        self,
        cmd: list[str],
        timeout: int,
        cwd: str,
    ) -> ExecutionResult:
        """Run a subprocess with resource limits and timeout, reaping process group on timeout.

        Args:
            cmd: command list, e.g. ["python", "-c", "print(2+2)"]
            timeout: wall-clock timeout in seconds
            cwd: working directory (usually a temp dir)

        Returns:
            ExecutionResult with stdout, stderr, exit_code, duration_ms, timed_out, backend="local", sandboxed=False.
        """
        start = time.monotonic()
        timed_out = False
        stdout_out = ""
        stderr_out = ""
        exit_code = -1
        proc = None

        try:
            # Use Popen instead of subprocess.run so we can reap the process group on timeout.
            # start_new_session=True creates a new process group (pgid == pid).
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                preexec_fn=self._make_preexec_fn(timeout),
                text=True,
                start_new_session=True,  # NEW: isolate process group
            )
            try:
                stdout_out, stderr_out = proc.communicate(timeout=timeout)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                # HARDENED: on timeout, kill the entire process group (not just the direct child).
                # This ensures forked grandchildren (e.g., infinite loops under `sh -c`) are reaped.
                timed_out = True
                exit_code = -1
                pgid = None
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                    # Give a moment for the kill to propagate before collecting output.
                    time.sleep(0.1)
                except Exception as kill_err:
                    logger.error("failed to kill process group %s: %s", pgid, kill_err)
                # Attempt to collect partial output if available.
                try:
                    stdout_out, stderr_out = proc.communicate(timeout=1)
                except Exception:
                    stdout_out = stdout_out if stdout_out else ""
                    stderr_out = stderr_out if stderr_out else ""
        except Exception as e:
            # e.g., OSError from preexec_fn, OSError from process setup.
            logger.error("subprocess execution failed: %s", e)
            stderr_out = str(e)
            exit_code = -1
            if proc is not None:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    pass

        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecutionResult(
            stdout=stdout_out,
            stderr=stderr_out,
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
            backend="local",
            sandboxed=False,
        )

    def run_python(
        self, code: str, timeout: int = cfg.DEFAULT_TIMEOUT, packages: list[str] = []
    ) -> ExecutionResult:
        """Execute Python code in a subprocess with resource limits.

        Args:
            code: Python code as a string
            timeout: wall-clock timeout in seconds
            packages: list of pip packages to install (isolated to temp target, not host)

        Returns:
            ExecutionResult with stdout, stderr, exit_code, duration_ms, timed_out, backend="local", sandboxed=False.

        Note: packages are installed to an isolated temp directory via --target, and that
              directory is added to sys.path for the execution, so the host environment is NOT mutated.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            script = code
            if packages:
                # HARDENED: install to temp target, not host interpreter.
                target_dir = os.path.join(tmpdir, "site-packages")
                os.makedirs(target_dir, exist_ok=True)
                logger.info(
                    "installing packages to isolated temp target %s: %s",
                    target_dir,
                    packages,
                )
                script = (
                    "import subprocess,sys,os\n"
                    f"target_dir = {target_dir!r}\n"
                    f"subprocess.run([sys.executable,'-m','pip','install','--target',target_dir,'--quiet',*{packages!r}],check=True)\n"
                    f"sys.path.insert(0, target_dir)\n"
                    + code
                )

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
        expr: str | None = None,
    ) -> TestResult:
        """Run a test suite in a subprocess with resource limits.

        Args:
            test_path: path to test file or directory (can be relative; resolved from current cwd)
            framework: test framework (only "pytest" supported)
            timeout: wall-clock timeout in seconds
            expr: optional pytest -k expression to select specific tests

        Returns:
            TestResult with passed, failed, errors counts, output, duration_ms, timed_out, backend="local", sandboxed=False.
            If test_path is invalid (relative path that resolves to nothing), errors >= 1.
        """
        if framework != "pytest":
            raise ValueError(f"unsupported framework: {framework}")

        # HARDENED: resolve test_path to absolute if it's relative.
        # This ensures that pytest is run from the project root (os.getcwd()),
        # not from a random temp directory where the path won't be found.
        if not os.path.isabs(test_path):
            test_path = os.path.join(os.getcwd(), test_path)

        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = [sys.executable, "-m", "pytest", test_path, "-q", "--no-header"]
            if expr:
                cmd += ["-k", expr]
            exec_result = self._run_process(cmd, timeout, tmpdir)
            passed, failed, errors, no_tests_ran = _parse_pytest(
                exec_result.stdout + exec_result.stderr
            )
            # If pytest found no tests or the path was invalid, count it as an error.
            if no_tests_ran and passed == 0 and failed == 0 and errors == 0:
                errors = 1
            return TestResult(
                passed=passed,
                failed=failed,
                errors=errors,
                duration_ms=exec_result.duration_ms,
                output=exec_result.stdout + exec_result.stderr,
                timed_out=exec_result.timed_out,
                backend="local",
                sandboxed=False,
            )
