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
