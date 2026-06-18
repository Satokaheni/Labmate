import sys
from pathlib import Path

import pytest

SERVER_DIR = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "pdf-parse"
)
sys.path.insert(0, str(SERVER_DIR))


@pytest.fixture
def sample_pdf(tmp_path) -> str:
    """A tiny real one-page PDF generated with PyMuPDF (no extra deps)."""
    import fitz  # PyMuPDF
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Attention Is All You Need")
    page.insert_text((72, 100), "We propose the Transformer.")
    pdf_path = tmp_path / "sample.pdf"
    doc.set_metadata({"title": "Attention Is All You Need", "author": "Vaswani, Shazeer"})
    doc.save(str(pdf_path))
    doc.close()
    return str(pdf_path)


class _FakeProv:
    def __init__(self, page_no):
        self.page_no = page_no


class _FakePicture:
    def __init__(self, page_no, caption):
        self.prov = [_FakeProv(page_no)]
        self._caption = caption

    def caption_text(self, doc):
        return self._caption

    def get_image(self, doc):
        from PIL import Image
        return Image.new("RGB", (4, 4), color=(255, 0, 0))


class _FakeTable:
    def __init__(self, page_no, caption):
        self.prov = [_FakeProv(page_no)]
        self._caption = caption

    def caption_text(self, doc):
        return self._caption

    def export_to_html(self, doc=None):
        return "<table><tr><td>1</td></tr></table>"


class _FakeDoc:
    def __init__(self):
        self.pictures = [_FakePicture(3, "Figure 1: architecture")]
        self.tables = [_FakeTable(5, "Table 1: results")]

    def export_to_markdown(self):
        return "# Attention Is All You Need\n\nWe propose the Transformer."


class _FakeConvertResult:
    def __init__(self):
        self.document = _FakeDoc()


class _FakeConverter:
    def convert(self, path):
        return _FakeConvertResult()


@pytest.fixture
def parser_with_fake_docling(tmp_path):
    """A DocumentParser whose Docling converter is replaced by a fake."""
    import parser as pp
    p = pp.DocumentParser(output_dir=str(tmp_path / "assets"))
    p._docling_converter = _FakeConverter()
    return p
