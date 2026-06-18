"""DocumentParser: Docling-default PDF -> Markdown with figures/tables/metadata.

CRITICAL: this module is loaded inside an MCP stdio child process.
NEVER print() or write to stdout. All logging goes to sys.stderr.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("pdf-parse")  # handlers configured to stderr in server.py


@dataclass
class ParseResult:
    path: str
    markdown: str
    figures: list[dict] = field(default_factory=list)   # [{path, caption, page}]
    tables: list[dict] = field(default_factory=list)     # [{html, caption, page}]
    metadata: dict = field(default_factory=dict)         # title, authors, doi, page_count

    def to_dict(self) -> dict:
        return asdict(self)


class DocumentParser:
    """Converts PDFs to Markdown. Docling default; MinerU opt-in."""

    def __init__(self, output_dir: str = "/tmp/pdf-parse-assets") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Lazily-built Docling converter, shared across calls (model load is slow).
        self._docling_converter = None

    @property
    def docling_converter(self):
        if self._docling_converter is None:
            # Local import: Docling pulls heavy model backends. Keep module-load cheap.
            from docling.document_converter import DocumentConverter
            log.info("initializing Docling DocumentConverter")
            self._docling_converter = DocumentConverter()
        return self._docling_converter

    def _parse_docling(self, path: str) -> ParseResult:
        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(f"PDF not found: {path}")

        result = self.docling_converter.convert(str(src))
        doc = result.document

        markdown = doc.export_to_markdown()
        metadata = self._extract_metadata(src, doc)
        figures = self._extract_docling_figures(src, doc)
        tables = self._extract_docling_tables(doc)

        return ParseResult(
            path=str(src),
            markdown=markdown,
            figures=figures,
            tables=tables,
            metadata=metadata,
        )

    def _extract_metadata(self, src: Path, doc) -> dict:
        meta: dict = {
            "title": None,
            "authors": [],
            "doi": None,
            "page_count": None,
        }
        try:
            import fitz  # PyMuPDF
            with fitz.open(str(src)) as pdf:
                meta["page_count"] = pdf.page_count
                info = pdf.metadata or {}
                if info.get("title"):
                    meta["title"] = info["title"].strip()
                if info.get("author"):
                    # PDF author field is a single string; split on common separators
                    authors = [a.strip() for a in info["author"].replace(";", ",").split(",")]
                    meta["authors"] = [a for a in authors if a]
        except Exception as exc:  # metadata is best-effort, never fatal
            log.warning("metadata extraction failed for %s: %s", src, exc)

        if not meta["title"]:
            meta["title"] = src.stem
        return meta

    def _extract_docling_figures(self, src: Path, doc) -> list[dict]:
        figures: list[dict] = []
        pictures = getattr(doc, "pictures", []) or []
        for idx, pic in enumerate(pictures):
            page = self._item_page(pic)
            caption = self._item_caption(doc, pic)
            img_path = self.output_dir / f"{src.stem}_fig{idx + 1}.png"
            try:
                pil_img = pic.get_image(doc)  # Docling PictureItem -> PIL image
                if pil_img is not None:
                    pil_img.save(img_path)
                    figures.append(
                        {"path": str(img_path), "caption": caption, "page": page}
                    )
            except Exception as exc:  # one bad image must not drop the rest
                log.warning("could not save figure %d from %s: %s", idx + 1, src, exc)
        return figures

    def _extract_docling_tables(self, doc) -> list[dict]:
        tables: list[dict] = []
        for tbl in getattr(doc, "tables", []) or []:
            try:
                html = tbl.export_to_html(doc=doc)
            except TypeError:
                # older docling signatures take no doc kwarg
                html = tbl.export_to_html()
            except Exception as exc:
                log.warning("table export failed: %s", exc)
                continue
            tables.append(
                {
                    "html": html,
                    "caption": self._item_caption(doc, tbl),
                    "page": self._item_page(tbl),
                }
            )
        return tables

    @staticmethod
    def _item_page(item) -> int | None:
        prov = getattr(item, "prov", None)
        if prov:
            first = prov[0]
            page = getattr(first, "page_no", None)
            if page is not None:
                return int(page)
        return None

    @staticmethod
    def _item_caption(doc, item) -> str:
        # Docling items expose caption_text(doc) in recent versions.
        getter = getattr(item, "caption_text", None)
        if callable(getter):
            try:
                text = getter(doc)
                if text:
                    return str(text).strip()
            except Exception:  # caption is best-effort
                pass
        return ""

    def _parse_mineru(self, path: str) -> ParseResult:
        try:
            import mineru  # noqa: F401  (opt-in, GPU-heavy, not in requirements.txt)
        except ImportError as exc:
            raise ImportError(
                "mode='mineru' requires the 'mineru' package and a GPU. "
                "Install it with `pip install mineru` on a GPU host, or use "
                "the default mode='docling' (CPU-friendly)."
            ) from exc

        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(f"PDF not found: {path}")

        # MinerU integration: produce markdown + assets under output_dir.
        # Kept minimal here; high-fidelity layout/formula handling is MinerU's job.
        from mineru.cli.common import do_parse  # API surface may vary by version

        log.info("parsing %s with MinerU (high-fidelity mode)", src)
        markdown, figures, tables = self._run_mineru(do_parse, src)
        metadata = self._extract_metadata(src, doc=None)
        return ParseResult(
            path=str(src),
            markdown=markdown,
            figures=figures,
            tables=tables,
            metadata=metadata,
        )

    def _run_mineru(self, do_parse, src: Path):
        # Thin adapter isolated for easy mocking/version-pinning.
        # Returns (markdown: str, figures: list[dict], tables: list[dict]).
        raise NotImplementedError(
            "MinerU adapter must be wired to the installed mineru version; "
            "see https://github.com/opendatalab/MinerU for the current API."
        )

    def parse(self, path: str, mode: str = "docling") -> ParseResult:
        if mode == "docling":
            return self._parse_docling(path)
        if mode == "mineru":
            return self._parse_mineru(path)
        raise ValueError(f"unknown mode: {mode!r} (expected 'docling' or 'mineru')")

    def parse_batch(self, paths: list[str], mode: str = "docling") -> list[ParseResult]:
        results: list[ParseResult] = []
        for p in paths:
            try:
                results.append(self.parse(p, mode=mode))
            except Exception as exc:
                log.exception("parse failed for %s", p)
                results.append(
                    ParseResult(
                        path=p,
                        markdown="",
                        figures=[],
                        tables=[],
                        metadata={"error": repr(exc)},
                    )
                )
        return results

    def extract_figures(self, path: str) -> list[dict]:
        # Figure extraction always uses the docling path (CPU-friendly, no GPU).
        result = self._parse_docling(path)
        return result.figures
