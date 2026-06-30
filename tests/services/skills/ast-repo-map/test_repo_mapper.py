import json
import os
import time

import pytest


@pytest.mark.mocked
def test_extracts_definition_tags(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    tags = mapper._parse_file("util.py")
    names = {t.name for t in tags if t.kind == "def"}
    assert "helper" in names
    assert "Widget" in names
    assert any(t.kind == "ref" for t in tags)


@pytest.mark.mocked
def test_get_symbols_returns_defs_jsonl(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    out = mapper.get_symbols("util.py")
    records = [json.loads(line) for line in out.splitlines() if line]
    names = {r["name"] for r in records}
    assert "helper" in names and "Widget" in names
    for r in records:
        assert set(r) == {"name", "kind", "signature", "parent", "loc"}
        assert r["loc"].startswith("util.py:")


@pytest.mark.mocked
def test_pagerank_boosts_chat_files(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    all_tags = []
    for f in ("util.py", "service.py"):
        all_tags.extend(mapper._parse_file(f))
    graph = mapper.build_graph(all_tags)

    boosted = mapper.rank(graph, chat_files=["service.py"])
    neutral = mapper.rank(graph, chat_files=[])
    # boosting service.py raises its own rank relative to the unboosted run
    assert boosted["service.py"] > neutral["service.py"]


@pytest.mark.mocked
def test_output_within_token_budget(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    out = mapper.get_repo_map(chat_files=["service.py"], max_tokens=10)
    data_lines = [line for line in out.splitlines() if not line.startswith("// ...")]
    # budget is a HARD cap: emitted data tokens never exceed max_tokens
    emitted_cost = sum(mapper._count_tokens(line + "\n") for line in data_lines)
    assert emitted_cost <= 10


@pytest.mark.mocked
def test_truncation_marker_appears(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    out = mapper.get_repo_map(chat_files=["service.py"], max_tokens=1)
    assert any(line.startswith("// ...") and "symbols omitted" in line for line in out.splitlines())


@pytest.mark.mocked
def test_mtime_cache_parses_once(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    mapper._parse_file("util.py")
    first = mapper._parse_count
    mapper._parse_file("util.py")  # identical mtime -> cache hit
    assert mapper._parse_count == first

    # bump mtime -> cache miss -> one more parse
    p = sample_repo / "util.py"
    os.utime(p, (time.time() + 10, time.time() + 10))
    mapper._parse_file("util.py")
    assert mapper._parse_count == first + 1


@pytest.mark.mocked
def test_broken_code_is_error_tolerant(repo_mapper, tmp_path):
    (tmp_path / "broken.py").write_text("def good():\n    return 1\n\ndef bad(  :\n    oops\n")
    mapper = repo_mapper.RepoMapper(str(tmp_path))
    tags = mapper._parse_file("broken.py")  # must not raise
    names = {t.name for t in tags if t.kind == "def"}
    assert "good" in names


@pytest.mark.mocked
def test_javascript_file_does_not_crash_repo_map(repo_mapper, tmp_path):
    # Regression: JavaScript reused the TypeScript tree-sitter query, but
    # `type_identifier` is a TS-only node type, so any .js file raised
    # tree_sitter.QueryError and aborted the WHOLE repo map. JS class names are
    # (identifier), not (type_identifier) — JS needs its own query.
    (tmp_path / "widget.js").write_text(
        "function makeWidget() {\n  return new Widget();\n}\n\n"
        "class Widget {\n  build() {\n    return makeWidget();\n  }\n}\n"
    )
    mapper = repo_mapper.RepoMapper(str(tmp_path))
    tags = mapper._parse_file("widget.js")  # must not raise
    names = {t.name for t in tags if t.kind == "def"}
    assert "makeWidget" in names
    assert "Widget" in names


@pytest.mark.mocked
def test_unparseable_file_is_isolated(repo_mapper, sample_repo, monkeypatch):
    # Regression: a single file that fails to parse (e.g. a grammar/query
    # mismatch) used to abort the entire repo map. It must be skipped, not fatal.
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    real_extract = mapper._extract_tags

    def boom(language, tree, source, path):
        if path == "service.py":
            raise RuntimeError("simulated grammar/query mismatch")
        return real_extract(language, tree, source, path)

    monkeypatch.setattr(mapper, "_extract_tags", boom)

    # service.py blows up, but util.py still contributes — no exception escapes.
    out = mapper.get_repo_map(chat_files=[], max_tokens=1000)
    names = {
        json.loads(line)["name"]
        for line in out.splitlines()
        if line and not line.startswith("// ...")
    }
    assert "helper" in names  # from util.py
    assert mapper._parse_file("service.py") == []  # isolated -> empty (cached)


@pytest.mark.mocked
def test_count_tokens_char_fallback_when_no_tokenizer(repo_mapper, sample_repo):
    # Regression: an unavailable tokenizer must not crash the map; _count_tokens
    # falls back to a ~4-chars/token estimate (still no tiktoken).
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    repo_mapper.RepoMapper._tokenizer = None
    repo_mapper.RepoMapper._tokenizer_failed = True
    try:
        assert mapper._count_tokens("abcdefgh") == 2  # 8 // 4
        assert mapper._count_tokens("") == 1  # max(1, 0)
    finally:
        repo_mapper.RepoMapper._tokenizer_failed = False
