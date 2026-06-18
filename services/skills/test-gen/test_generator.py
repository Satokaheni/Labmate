"""Generates and improves pytest suites via Qwen2.5-Coder-32B (litellm).

CRITICAL: stderr-only logging. NEVER print() — stdout is JSON-RPC.
Qwen is the specialist test writer; Gemma never writes tests.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys

import litellm

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("test-gen.generator")

MODEL = os.getenv("QWEN_MODEL", "openai/Qwen2.5-Coder-32B-Instruct")


class TestGenerator:
    def __init__(self) -> None:
        _gemma = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
        self._base_url = os.getenv("QWEN_BASE", _gemma)  # defaults to Gemma on single-GPU
        self._api_key = os.getenv("QWEN_API_KEY", "not-needed")

    def _read_source(self, path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            log.error("cannot read source %s: %r", path, exc)
            raise

    def _call_llm(self, prompt: str) -> str:
        log.info("calling Qwen at %s (model=%s)", self._base_url, MODEL)
        resp = litellm.completion(
            model=MODEL,
            api_base=self._base_url,
            api_key=self._api_key,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a meticulous Python test engineer. You write "
                        "pytest unit tests that maximize mutation kill rate. "
                        "Return only the requested fields."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        return resp["choices"][0]["message"]["content"]

    @staticmethod
    def _extract_code(reply: str) -> str:
        m = re.search(r"```(?:python)?\s*\n(.*?)```", reply, re.DOTALL)
        return (m.group(1) if m else reply).strip()

    def generate(self, source_file: str, existing_tests: str = "") -> dict:
        source = self._read_source(source_file)
        existing_block = (
            f"\n\nExisting tests (do not duplicate; extend coverage):\n"
            f"```python\n{existing_tests}\n```"
            if existing_tests.strip()
            else ""
        )
        prompt = (
            f"Write a thorough pytest suite for this module. Cover edge cases, "
            f"boundary values, and error paths so that mutation testing finds "
            f"few survivors.\n\n"
            f"Source file `{source_file}`:\n```python\n{source}\n```"
            f"{existing_block}\n\n"
            f"Respond with a single ```python code block containing the full "
            f"test module, followed by a one-paragraph explanation."
        )
        reply = self._call_llm(prompt)
        code = self._extract_code(reply)
        explanation = reply.split("```")[-1].strip() if "```" in reply else ""
        log.info("generated %d chars of test code", len(code))
        return {"test_code": code, "explanation": explanation}

    def improve(
        self,
        source_file: str,
        test_file: str,
        surviving_mutants: list[str],
    ) -> dict:
        source = self._read_source(source_file)
        existing = self._read_source(test_file)
        mutants_block = "\n\n".join(
            f"Surviving mutant {i + 1}:\n```diff\n{m}\n```"
            for i, m in enumerate(surviving_mutants)
        )
        prompt = (
            f"The current test suite for `{source_file}` failed to kill the "
            f"following mutants. Each diff shows a code change that the tests "
            f"did NOT detect. Write ADDITIONAL pytest tests that would fail "
            f"against each mutated version (and pass against the original).\n\n"
            f"Source:\n```python\n{source}\n```\n\n"
            f"Current tests:\n```python\n{existing}\n```\n\n"
            f"{mutants_block}\n\n"
            f"Respond with a single ```python code block containing ONLY the "
            f"new test functions to append."
        )
        reply = self._call_llm(prompt)
        code = self._extract_code(reply)
        log.info(
            "improve: %d surviving mutants -> %d chars of new tests",
            len(surviving_mutants), len(code),
        )
        return {"additional_test_code": code}
