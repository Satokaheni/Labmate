---
name: code-sandbox
description: >
  Isolated, ephemeral code execution environment for running agent-generated code safely.
  Use when you need to execute Python code, run shell commands, or run a test suite in
  a sandboxed environment with resource limits. Prerequisite for test-gen skill.
  Docker-based isolation with network disabled, memory limited, read-only filesystem.
trigger: "Use when executing code that has not been human-reviewed"
tools:
  - run_python
  - run_shell
  - run_tests
  - install_packages
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
