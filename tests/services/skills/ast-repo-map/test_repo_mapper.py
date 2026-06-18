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
    data_lines = [l for l in out.splitlines() if not l.startswith("// ...")]
    total = sum(len(l.split()) + 1 for l in data_lines)  # +1 for newline word? see note
    # budget is a HARD cap: emitted data tokens never exceed max_tokens
    emitted_cost = sum(mapper._count_tokens(l + "\n") for l in data_lines)
    assert emitted_cost <= 10


@pytest.mark.mocked
def test_truncation_marker_appears(repo_mapper, sample_repo):
    mapper = repo_mapper.RepoMapper(str(sample_repo))
    out = mapper.get_repo_map(chat_files=["service.py"], max_tokens=1)
    assert any(l.startswith("// ...") and "symbols omitted" in l
               for l in out.splitlines())


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
    (tmp_path / "broken.py").write_text(
        "def good():\n    return 1\n\ndef bad(  :\n    oops\n"
    )
    mapper = repo_mapper.RepoMapper(str(tmp_path))
    tags = mapper._parse_file("broken.py")  # must not raise
    names = {t.name for t in tags if t.kind == "def"}
    assert "good" in names
