"""AstSearcher: polyglot structural code search and rewrite via ast-grep-py.

stdout is sacred: this module logs ONLY to sys.stderr via the logging module.
Never call print() here — stdout carries JSON-RPC 2.0.
"""

import difflib
import logging
import re
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

    @staticmethod
    def _collect_meta_vars(node, pattern: str) -> dict:
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
            # ast-grep-py 0.30+ accepts rule fields as keyword args to find_all.
            for found in node.find_all(**rule):
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
