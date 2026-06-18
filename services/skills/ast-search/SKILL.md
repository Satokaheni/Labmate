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
