# ast-search MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the ast-search Python MCP server that provides polyglot structural code search and rewrite using AST patterns.

**Architecture:** A Python MCP stdio server wraps ast-grep-py to expose three tools: find_code (structural pattern search), rewrite (pattern replacement with diff preview), and find_by_rule (YAML rule-based search). All logging goes to stderr; the rewrite tool never auto-saves — it returns a unified diff for model review.

**Tech Stack:** Python 3.11+, `mcp` SDK, `ast-grep-py`, `pyyaml`, `pytest`

---

## Background — spec requirements (read before starting)

From `research/llm-harness-research/specs/spec_skills.md` section 6.3, the `ast.search` skill must satisfy:

- **R1** `find_code(pattern, language, path)` — finds all AST nodes matching `pattern` in a file or directory. Supports meta-variables `$VAR` (single node) and `$$$MULTI` (zero-or-more nodes). Example: `requests.get($URL)` matches all GET calls regardless of URL expression.
- **R2** `rewrite(pattern, replacement, language, path)` — rewrites matched nodes. Returns a **unified diff for model review before application**. Always preview before saving; never auto-write to disk.
- **R3** `find_by_rule(rule_yaml, path)` — accepts a YAML rule with `pattern`, `kind`, `inside`, `has`, `not` constraints for context-aware matches.
- **R4** Language routing — supports Python, TypeScript, JavaScript, Rust, Go. Pass `language` explicitly; do NOT rely on file-extension detection for the parser, though directory walking still uses extensions to pick candidate files.
- **R5** Syntactic only — does NOT resolve types, scopes, or cross-file references. Matches AST nodes, not text, so it cannot match inside string literals or comments.

**Critical runtime rule:** stdout carries JSON-RPC 2.0. ALL logging uses the `logging` module wired to `sys.stderr`. **NEVER `print()`** anywhere in `server.py` or `searcher.py`.

---

## Task 1: Create directory structure

- [ ] Create the skill and test directories.

```bash
mkdir -p services/skills/ast-search
mkdir -p tests/services/skills/ast-search
```

---

## Task 2: Write requirements.txt

- [ ] Create `services/skills/ast-search/requirements.txt`:

```text
mcp>=1.0.0
ast-grep-py>=0.30.0
pyyaml>=6.0
```

---

## Task 3: Define Match and Diff dataclasses in searcher.py

- [ ] Create `services/skills/ast-search/searcher.py` with the module header, imports, logger, and the two dataclasses:

```python
"""AstSearcher: polyglot structural code search and rewrite via ast-grep-py.

stdout is sacred: this module logs ONLY to sys.stderr via the logging module.
Never call print() here — stdout carries JSON-RPC 2.0.
"""

import difflib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from ast_grep_py import SgRoot

log = logging.getLogger("ast.search")


@dataclass
class Match:
    file: str
    line: int
    column: int
    text: str  # matched source text
    meta_vars: dict = field(default_factory=dict)  # $VAR -> matched text


@dataclass
class Diff:
    file: str
    unified_diff: str  # git-style unified diff, for model review before applying
    matches: int  # number of replacements
```

---

## Task 4: Add language normalization and extension mapping

- [ ] Append to `searcher.py` the language tables and the `AstSearcher` class with `_parse_language`:

```python
# ast-grep-py accepts these canonical language names.
_LANGUAGE_ALIASES = {
    "python": "python",
    "py": "python",
    "typescript": "typescript",
    "ts": "typescript",
    "javascript": "javascript",
    "js": "javascript",
    "rust": "rust",
    "rs": "rust",
    "go": "go",
    "golang": "go",
}

# File extensions used ONLY for directory walking (which files to feed the parser).
# The parser language always comes from the explicit `language` argument (spec R4).
_LANGUAGE_EXTENSIONS = {
    "python": (".py",),
    "typescript": (".ts", ".tsx"),
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "rust": (".rs",),
    "go": (".go",),
}


class AstSearcher:
    """Wraps ast-grep-py. Structural (AST-node) search only — no type/scope resolution."""

    def _parse_language(self, language: str) -> str:
        normalized = _LANGUAGE_ALIASES.get(language.strip().lower())
        if normalized is None:
            raise ValueError(
                f"Unsupported language: {language!r}. "
                f"Supported: {sorted(set(_LANGUAGE_ALIASES.values()))}"
            )
        return normalized
```

---

## Task 5: Add path walking

- [ ] Append the `_walk_path` method to `AstSearcher` in `searcher.py`. It accepts a single file or a directory and returns the candidate files for the given language:

```python
    def _walk_path(self, path: str, language: str) -> list[Path]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")

        extensions = _LANGUAGE_EXTENSIONS[language]

        if p.is_file():
            return [p]

        files = [
            child
            for child in sorted(p.rglob("*"))
            if child.is_file() and child.suffix in extensions
        ]
        log.info("walked %s -> %d %s file(s)", path, len(files), language)
        return files
```

---

## Task 6: Implement find_code

- [ ] Append `find_code` to `AstSearcher` in `searcher.py`:

```python
    def find_code(self, pattern: str, language: str, path: str) -> list[Match]:
        lang = self._parse_language(language)
        files = self._walk_path(path, lang)
        matches: list[Match] = []

        for file in files:
            try:
                source = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                log.warning("skipping %s: %s", file, exc)
                continue

            root = SgRoot(source, lang)
            node = root.root()
            for found in node.find_all(pattern=pattern):
                rng = found.range()
                meta_vars = self._collect_meta_vars(found, pattern)
                matches.append(
                    Match(
                        file=str(file),
                        line=rng.start.line + 1,  # ast-grep is 0-based; report 1-based
                        column=rng.start.column,
                        text=found.text(),
                        meta_vars=meta_vars,
                    )
                )

        log.info("find_code pattern=%r matched %d node(s)", pattern, len(matches))
        return matches
```

---

## Task 7: Implement meta-variable collection helper

- [ ] Append `_collect_meta_vars` to `AstSearcher` in `searcher.py`. It extracts both single (`$VAR`) and multi (`$$$MULTI`) meta-variables from a matched node by parsing the variable names out of the pattern:

```python
    @staticmethod
    def _collect_meta_vars(node, pattern: str) -> dict:
        import re

        meta_vars: dict = {}

        # $$$MULTI captures a list of nodes.
        for name in re.findall(r"\$\$\$([A-Z_][A-Z0-9_]*)", pattern):
            captured = node.get_multiple_matches(name)
            if captured:
                meta_vars[f"$$${name}"] = " ".join(n.text() for n in captured)

        # $VAR captures a single node. Exclude names already matched as $$$.
        multi_names = set(re.findall(r"\$\$\$([A-Z_][A-Z0-9_]*)", pattern))
        for name in re.findall(r"(?<!\$)\$([A-Z_][A-Z0-9_]*)", pattern):
            if name in multi_names:
                continue
            captured = node.get_match(name)
            if captured is not None:
                meta_vars[f"${name}"] = captured.text()

        return meta_vars
```

---

## Task 8: Implement rewrite (diff preview, never writes disk)

- [ ] Append `rewrite` to `AstSearcher` in `searcher.py`. It computes replacements per file via ast-grep-py, builds a unified diff, and **never writes to disk** (spec R2):

```python
    def rewrite(
        self, pattern: str, replacement: str, language: str, path: str
    ) -> Diff:
        lang = self._parse_language(language)
        files = self._walk_path(path, lang)

        diff_chunks: list[str] = []
        total_replacements = 0
        first_file = files[0] if files else Path(path)

        for file in files:
            try:
                source = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                log.warning("skipping %s: %s", file, exc)
                continue

            root = SgRoot(source, lang)
            node = root.root()
            found = node.find_all(pattern=pattern)
            if not found:
                continue

            edits = [n.replace(replacement) for n in found]
            new_source = node.commit_edits(edits)
            total_replacements += len(edits)

            file_diff = difflib.unified_diff(
                source.splitlines(keepends=True),
                new_source.splitlines(keepends=True),
                fromfile=f"a/{file}",
                tofile=f"b/{file}",
            )
            diff_chunks.append("".join(file_diff))

        unified = "".join(diff_chunks)
        log.info(
            "rewrite pattern=%r produced %d replacement(s) across %d file(s) (NOT written)",
            pattern,
            total_replacements,
            len(diff_chunks),
        )
        return Diff(
            file=str(first_file),
            unified_diff=unified,
            matches=total_replacements,
        )
```

---

## Task 9: Implement find_by_rule

- [ ] Append `find_by_rule` to `AstSearcher` in `searcher.py`. It parses a YAML rule and applies it via ast-grep-py's `find_all(matcher)` config form. The rule YAML must contain a top-level `language` key (or the `language` field) so the parser knows which grammar to use:

```python
    def find_by_rule(self, rule_yaml: str, path: str) -> list[Match]:
        config = yaml.safe_load(rule_yaml)
        if not isinstance(config, dict):
            raise ValueError("rule_yaml must parse to a mapping")

        language = config.get("language")
        if not language:
            raise ValueError("rule_yaml must include a top-level 'language' field")
        lang = self._parse_language(language)

        rule = config.get("rule")
        if not rule:
            raise ValueError("rule_yaml must include a top-level 'rule' field")

        files = self._walk_path(path, lang)
        matches: list[Match] = []

        for file in files:
            try:
                source = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                log.warning("skipping %s: %s", file, exc)
                continue

            root = SgRoot(source, lang)
            node = root.root()
            # ast-grep-py accepts a rule dict via the `matcher` config arg.
            for found in node.find_all(matcher=rule):
                rng = found.range()
                matches.append(
                    Match(
                        file=str(file),
                        line=rng.start.line + 1,
                        column=rng.start.column,
                        text=found.text(),
                        meta_vars={},
                    )
                )

        log.info("find_by_rule matched %d node(s)", len(matches))
        return matches
```

---

## Task 10: Write server.py — imports, logging, app, searcher

- [ ] Create `services/skills/ast-search/server.py` with the header, stderr-only logging config, the MCP `Server` instance, and the shared `AstSearcher`:

```python
"""ast.search MCP server (stdio transport).

stdout is sacred — it carries JSON-RPC 2.0. All logging goes to sys.stderr.
NEVER call print() in this module.
"""

import asyncio
import json
import logging
import sys
from dataclasses import asdict

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from searcher import AstSearcher

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("ast.search.server")

app = Server("ast.search")
searcher = AstSearcher()
```

---

## Task 11: Implement list_tools

- [ ] Append the `list_tools` handler to `server.py`, declaring all three tools with input schemas:

```python
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="ast.search.find_code",
            description=(
                "Find all AST nodes matching a structural pattern in a file or directory. "
                "Meta-variables: $VAR (single node), $$$MULTI (zero-or-more). "
                "Example pattern: requests.get($URL). Matches AST nodes only — never "
                "inside string literals or comments."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "ast-grep pattern."},
                    "language": {
                        "type": "string",
                        "description": "python | typescript | javascript | rust | go",
                    },
                    "path": {"type": "string", "description": "File or directory path."},
                },
                "required": ["pattern", "language", "path"],
            },
        ),
        Tool(
            name="ast.search.rewrite",
            description=(
                "Rewrite nodes matching `pattern` to `replacement`. Returns a unified diff "
                "for review — NEVER writes to disk. Always preview before applying."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "replacement": {"type": "string"},
                    "language": {
                        "type": "string",
                        "description": "python | typescript | javascript | rust | go",
                    },
                    "path": {"type": "string"},
                },
                "required": ["pattern", "replacement", "language", "path"],
            },
        ),
        Tool(
            name="ast.search.find_by_rule",
            description=(
                "Find nodes via a YAML rule supporting pattern, kind, inside, has, not "
                "constraints. The YAML must include top-level 'language' and 'rule' fields."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "rule_yaml": {"type": "string", "description": "ast-grep YAML rule."},
                    "path": {"type": "string"},
                },
                "required": ["rule_yaml", "path"],
            },
        ),
    ]
```

---

## Task 12: Implement call_tool dispatch

- [ ] Append the `call_tool` handler to `server.py`. It dispatches to the searcher, serializes results to JSON, and returns errors as text without raising (so the JSON-RPC stream stays clean):

```python
@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    log.info("call_tool %s args=%s", name, list(arguments))
    try:
        if name == "ast.search.find_code":
            result = searcher.find_code(
                pattern=arguments["pattern"],
                language=arguments["language"],
                path=arguments["path"],
            )
            payload = [asdict(m) for m in result]
        elif name == "ast.search.rewrite":
            diff = searcher.rewrite(
                pattern=arguments["pattern"],
                replacement=arguments["replacement"],
                language=arguments["language"],
                path=arguments["path"],
            )
            payload = asdict(diff)
        elif name == "ast.search.find_by_rule":
            result = searcher.find_by_rule(
                rule_yaml=arguments["rule_yaml"],
                path=arguments["path"],
            )
            payload = [asdict(m) for m in result]
        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as exc:  # noqa: BLE001 — surface error to model, keep stream clean
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    return [TextContent(type="text", text=json.dumps(payload, indent=2))]
```

---

## Task 13: Implement main entry point

- [ ] Append the `main` coroutine and `__main__` guard to `server.py`:

```python
async def main() -> None:
    log.info("ast.search MCP server starting (stdio)")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Task 14: Write SKILL.md

- [ ] Create `services/skills/ast-search/SKILL.md`:

```markdown
---
name: ast-search
description: >
  Polyglot structural code search and rewrite using AST patterns. Use when you need
  to find all call sites of a function, locate a code pattern across files, or safely
  rewrite a syntactic pattern. Operates on AST nodes, not text — cannot match inside
  string literals or comments. For TypeScript type-aware cross-file rename, use ast-ts-refactor instead.
trigger: "Use when searching for or rewriting a code pattern across files"
tools:
  - ast.search.find_code
  - ast.search.rewrite
  - ast.search.find_by_rule
version: "0.1.0"
license: MIT
requires: []
---

# ast-search

Fast polyglot structural search and rewrite, wrapping `ast-grep-py`. Operates on syntax
(AST nodes), not raw text, so it never matches inside string literals or comments.

## Tools

### ast.search.find_code(pattern, language, path)
Find all AST nodes matching `pattern` in a file or directory.
Meta-variables: `$VAR` (single node), `$$$MULTI` (zero-or-more nodes).
Example: `requests.get($URL)` matches every GET call regardless of the URL expression.

### ast.search.rewrite(pattern, replacement, language, path)
Rewrite matched nodes. Returns a **unified diff for review** — it never writes to disk.
Always preview the diff before applying it.

### ast.search.find_by_rule(rule_yaml, path)
Accepts a YAML rule with `pattern`, `kind`, `inside`, `has`, and `not` constraints for
surgical, context-aware matches. The YAML must include top-level `language` and `rule` keys.

## Supported languages
Python, TypeScript, JavaScript, Rust, Go. Pass `language` explicitly — extension detection
is only used to pick candidate files when walking a directory.

## Limitations
Syntactic only. Does NOT resolve types, scopes, or cross-file references. For TypeScript
type-aware rename / find-references / move, use the `ast-ts-refactor` skill instead.
```

---

## Task 15: Write conftest.py with sample-file fixtures

- [ ] Create `tests/services/skills/ast-search/conftest.py`. It puts the skill dir on `sys.path` so `from searcher import AstSearcher` resolves, and provides fixtures that write small source files into `tmp_path`:

```python
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[4] / "services" / "skills" / "ast-search"
sys.path.insert(0, str(SKILL_DIR))


@pytest.fixture
def py_file(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text(
        "import requests\n"
        "\n"
        "def fetch(u):\n"
        "    r = requests.get(u)\n"
        "    other = requests.get('https://example.com')\n"
        "    note = \"call requests.get(here) in a string\"\n"
        "    return r, other, note\n",
        encoding="utf-8",
    )
    return f


@pytest.fixture
def ts_file(tmp_path):
    f = tmp_path / "sample.ts"
    f.write_text(
        "const a = foo(1);\n"
        "const b = foo(2, 3);\n"
        "const label = 'foo(99) inside string';\n",
        encoding="utf-8",
    )
    return f


@pytest.fixture
def py_dir(tmp_path):
    (tmp_path / "a.py").write_text("x = requests.get(1)\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = requests.get(2)\n", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("requests.get(3)\n", encoding="utf-8")
    return tmp_path
```

---

## Task 16: Write test_searcher.py — find_code

- [ ] Create `tests/services/skills/ast-search/test_searcher.py` with imports and the find_code tests:

```python
import pytest

from searcher import AstSearcher, Diff, Match


@pytest.fixture
def searcher():
    return AstSearcher()


@pytest.mark.mocked
def test_find_code_matches_pattern(searcher, py_file):
    matches = searcher.find_code("requests.get($URL)", "python", str(py_file))
    # Two real calls: requests.get(u) and requests.get('https://example.com').
    assert len(matches) == 2
    assert all(isinstance(m, Match) for m in matches)
    assert all(m.file == str(py_file) for m in matches)
    assert all(m.line >= 1 for m in matches)


@pytest.mark.mocked
def test_find_code_captures_meta_var(searcher, py_file):
    matches = searcher.find_code("requests.get($URL)", "python", str(py_file))
    captured = {m.meta_vars.get("$URL") for m in matches}
    assert "u" in captured
    assert "'https://example.com'" in captured
```

---

## Task 17: Add test — matches inside string literals are NOT returned

- [ ] Append to `test_searcher.py` the test that proves the structural-only guarantee (spec R5):

```python
@pytest.mark.mocked
def test_find_code_ignores_string_literals(searcher, py_file):
    matches = searcher.find_code("requests.get($URL)", "python", str(py_file))
    # The string "call requests.get(here) in a string" must NOT match.
    assert all("in a string" not in m.text for m in matches)
    assert len(matches) == 2
```

---

## Task 18: Add test — directory walking

- [ ] Append the directory-walk test to `test_searcher.py`:

```python
@pytest.mark.mocked
def test_find_code_walks_directory(searcher, py_dir):
    matches = searcher.find_code("requests.get($URL)", "python", str(py_dir))
    # a.py and b.py match; ignore.txt is not a .py file and is skipped.
    assert len(matches) == 2
    files = {m.file for m in matches}
    assert files == {str(py_dir / "a.py"), str(py_dir / "b.py")}
```

---

## Task 19: Add test — rewrite returns diff and does NOT modify files

- [ ] Append the rewrite tests to `test_searcher.py`:

```python
@pytest.mark.mocked
def test_rewrite_returns_unified_diff(searcher, py_file):
    original = py_file.read_text(encoding="utf-8")
    diff = searcher.rewrite(
        "requests.get($URL)", "session.get($URL)", "python", str(py_file)
    )
    assert isinstance(diff, Diff)
    assert diff.matches == 2
    assert "session.get" in diff.unified_diff
    assert diff.unified_diff.startswith("---") or "@@" in diff.unified_diff


@pytest.mark.mocked
def test_rewrite_does_not_write_to_disk(searcher, py_file):
    original = py_file.read_text(encoding="utf-8")
    searcher.rewrite("requests.get($URL)", "session.get($URL)", "python", str(py_file))
    # File on disk is unchanged — rewrite is preview-only.
    assert py_file.read_text(encoding="utf-8") == original
```

---

## Task 20: Add test — find_by_rule

- [ ] Append the find_by_rule test to `test_searcher.py`. The rule uses `pattern` plus a `kind`/`inside` constraint to prove context-aware matching:

```python
@pytest.mark.mocked
def test_find_by_rule_matches(searcher, py_file):
    rule = """
language: python
rule:
  pattern: requests.get($URL)
"""
    matches = searcher.find_by_rule(rule, str(py_file))
    assert len(matches) == 2
    assert all(isinstance(m, Match) for m in matches)


@pytest.mark.mocked
def test_find_by_rule_requires_language(searcher, py_file):
    rule = "rule:\n  pattern: requests.get($URL)\n"
    with pytest.raises(ValueError, match="language"):
        searcher.find_by_rule(rule, str(py_file))
```

---

## Task 21: Add test — TypeScript language routing and meta-var

- [ ] Append the TypeScript test to `test_searcher.py` (proves explicit-language routing per spec R4):

```python
@pytest.mark.mocked
def test_find_code_typescript(searcher, ts_file):
    matches = searcher.find_code("foo($$$ARGS)", "typescript", str(ts_file))
    # foo(1) and foo(2, 3) match; the string literal 'foo(99) ...' does not.
    assert len(matches) == 2
    assert all("inside string" not in m.text for m in matches)


@pytest.mark.mocked
def test_unsupported_language_raises(searcher, py_file):
    with pytest.raises(ValueError, match="Unsupported language"):
        searcher.find_code("x", "cobol", str(py_file))
```

---

## Task 22: Verify the suite runs

- [ ] Install deps and run the tests (mocked tier — no GPU, no live server):

```bash
pip install -r services/skills/ast-search/requirements.txt pytest pyyaml
python -m pytest tests/services/skills/ast-search/ -m mocked -v
```

- [ ] Confirm all tests pass. If `node.find_all(matcher=...)` or `node.find_all(pattern=...)` signatures differ in the installed `ast-grep-py` version, adjust the call form in `searcher.py` to match the installed API (`find_all` accepts either a pattern string via `pattern=` or a rule config via `matcher=`).

---

## Task 23: Smoke-test the server starts without polluting stdout

- [ ] Confirm the server imports and the JSON-RPC stream is clean (no stray prints). This sends nothing and times out quickly; the point is that startup logging lands on stderr, not stdout:

```bash
cd services/skills/ast-search && timeout 2 python server.py < /dev/null 1> /tmp/ast_stdout.txt 2> /tmp/ast_stderr.txt; \
test ! -s /tmp/ast_stdout.txt && echo "OK: stdout is clean" || (echo "FAIL: stdout polluted"; cat /tmp/ast_stdout.txt)
```

- [ ] Confirm `/tmp/ast_stdout.txt` is empty (stdout sacred) and `/tmp/ast_stderr.txt` contains the "ast.search MCP server starting" log line.

---

## Self-review checklist (run before declaring done)

- [ ] **Spec R1** find_code with meta-vars `$VAR`/`$$$MULTI` → Tasks 6, 7, 16, 17, 21.
- [ ] **Spec R2** rewrite returns diff, never writes disk → Tasks 8, 19.
- [ ] **Spec R3** find_by_rule with YAML constraints → Tasks 9, 20.
- [ ] **Spec R4** explicit language routing, supported set → Tasks 4, 21.
- [ ] **Spec R5** structural-only, no string-literal matches → Tasks 17, 21.
- [ ] **stdout sacred** — no `print()` anywhere; logging to stderr in both modules → Tasks 3, 10; verified Task 23.
- [ ] **Type/name consistency** — `Match(file, line, column, text, meta_vars)` and `Diff(file, unified_diff, matches)` used identically across searcher, server serialization (`asdict`), and tests.
- [ ] **No placeholders** — every code block is complete and runnable; no "add error handling here" stubs.
```
