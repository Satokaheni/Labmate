import json

import pytest


@pytest.mark.mocked
def test_parse_returns_parse_result(parser_with_fake_docling, sample_pdf):
    result = parser_with_fake_docling.parse(sample_pdf, mode="docling")
    assert result.markdown.strip()                 # non-empty markdown
    assert "Transformer" in result.markdown
    assert result.metadata["page_count"] == 1      # from PyMuPDF on the real fixture
    assert result.path == sample_pdf


@pytest.mark.mocked
def test_parse_extracts_figures(parser_with_fake_docling, sample_pdf):
    result = parser_with_fake_docling.parse(sample_pdf)
    assert len(result.figures) == 1
    fig = result.figures[0]
    assert set(fig) == {"path", "caption", "page"}
    assert fig["caption"] == "Figure 1: architecture"
    assert fig["page"] == 3
    from pathlib import Path
    assert Path(fig["path"]).is_file()             # image actually written


@pytest.mark.mocked
def test_parse_extracts_tables(parser_with_fake_docling, sample_pdf):
    result = parser_with_fake_docling.parse(sample_pdf)
    assert len(result.tables) == 1
    tbl = result.tables[0]
    assert set(tbl) == {"html", "caption", "page"}
    assert tbl["html"].startswith("<table")
    assert tbl["caption"] == "Table 1: results"
    assert tbl["page"] == 5


@pytest.mark.mocked
def test_extract_figures_returns_captioned_list(parser_with_fake_docling, sample_pdf):
    figures = parser_with_fake_docling.extract_figures(sample_pdf)
    assert isinstance(figures, list) and len(figures) == 1
    assert "caption" in figures[0]
    assert figures[0]["caption"] == "Figure 1: architecture"


@pytest.mark.mocked
def test_parse_batch_one_result_per_path(parser_with_fake_docling, sample_pdf, tmp_path):
    import shutil
    second = str(tmp_path / "second.pdf")
    shutil.copy(sample_pdf, second)
    results = parser_with_fake_docling.parse_batch([sample_pdf, second])
    assert len(results) == 2
    assert results[0].path == sample_pdf
    assert results[1].path == second
    assert all(r.markdown for r in results)


@pytest.mark.mocked
def test_parse_batch_isolates_failures(parser_with_fake_docling, sample_pdf):
    results = parser_with_fake_docling.parse_batch([sample_pdf, "/no/such/file.pdf"])
    assert len(results) == 2
    assert results[0].markdown                      # good file succeeded
    assert results[1].markdown == ""                # bad file degraded
    assert "error" in results[1].metadata


@pytest.mark.mocked
def test_mineru_mode_raises_helpful_error(parser_with_fake_docling, sample_pdf):
    import builtins
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "mineru" or name.startswith("mineru."):
            raise ImportError("No module named 'mineru'")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = blocked_import
    try:
        with pytest.raises(ImportError) as exc:
            parser_with_fake_docling.parse(sample_pdf, mode="mineru")
    finally:
        builtins.__import__ = real_import
    msg = str(exc.value).lower()
    assert "mineru" in msg and ("gpu" in msg or "install" in msg)


@pytest.mark.mocked
def test_unknown_mode_raises(parser_with_fake_docling, sample_pdf):
    with pytest.raises(ValueError):
        parser_with_fake_docling.parse(sample_pdf, mode="bogus")


@pytest.mark.mocked
def test_parse_writes_nothing_to_stdout(parser_with_fake_docling, sample_pdf, capsys):
    parser_with_fake_docling.parse(sample_pdf)
    captured = capsys.readouterr()
    assert captured.out == ""                       # stdout is sacred
