import json

import pytest


@pytest.mark.mocked
def test_no_chroma_or_vector_service_deps():
    """paper-rag uses PaperQA2's native local index — no Chroma / vector service."""
    import inspect

    import paper_store

    src = inspect.getsource(paper_store)
    assert "chromadb" not in src  # local harness has no Chroma
    assert "CHROMA_URL" not in src
    assert "PersistentClient" not in src
    assert "EphemeralClient" not in src
    assert "print(" not in src  # stdout is sacred


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_add_papers_calls_ingest_with_paths(tmp_path, mock_docs):
    import paper_store

    pdf = tmp_path / "a.pdf"
    pdf.write_text("dummy")
    store = paper_store.PaperStore()
    result = await store.add_papers([str(pdf)])
    mock_docs.aadd.assert_awaited_once()
    assert mock_docs.aadd.await_args.args[0] == str(pdf)
    assert result["count"] == 1


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_add_papers_reports_missing_file(mock_docs):
    import paper_store

    store = paper_store.PaperStore()
    result = await store.add_papers(["/does/not/exist.pdf"])
    assert result["count"] == 0
    assert result["errors"][0]["path"] == "/does/not/exist.pdf"


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_query_returns_answer_and_citations(mock_docs):
    import paper_store

    store = paper_store.PaperStore()
    result = await store.query("What is photosynthesis?", top_k=3)
    assert "answer" in result
    assert "citations" in result
    assert "Smith2020" in result["citations"]


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_search_returns_jsonl_parseable(mock_docs):
    import paper_store

    store = paper_store.PaperStore()
    matches = await store.search("energy", top_k=5)
    # Server joins these with newlines into JSONL; each must round-trip via json.
    line = "\n".join(json.dumps(m) for m in matches)
    for raw in line.splitlines():
        parsed = json.loads(raw)
        assert "text" in parsed and "citation" in parsed


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_list_papers_has_title_and_path(mock_docs):
    import paper_store

    store = paper_store.PaperStore()
    papers = await store.list_papers()
    assert papers[0]["title"] == "A Paper"
    assert papers[0]["path"] == "/tmp/a.pdf"
