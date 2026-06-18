"""SlideGenerator: PresentationBlueprint -> Beamer .tex or Marp .md."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from outline_planner import PresentationBlueprint, SlideBlueprint

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("paper-to-slides.slidegen")

_TPL_DIR = Path(__file__).parent / "templates"


def _latex_escape(s: str) -> str:
    repl = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
            "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
            "^": r"\textasciicircum{}"}
    return "".join(repl.get(c, c) for c in s)


class SlideGenerator:
    def to_beamer(self, bp: PresentationBlueprint) -> str:
        preamble = (_TPL_DIR / "beamer_preamble.tex").read_text() % {
            "title": _latex_escape(bp.paper_title),
            "authors": _latex_escape(", ".join(bp.authors)),
            "venue": _latex_escape(bp.venue),
        }
        body = ["\\begin{document}", "\\frame{\\titlepage}"]
        for sl in bp.slides:
            if sl.section == "title":
                continue
            body.append(self._beamer_frame(sl))
        body.append("\\end{document}")
        return preamble + "\n" + "\n".join(body) + "\n"

    def _beamer_frame(self, sl: SlideBlueprint) -> str:
        lines = [f"\\begin{{frame}}{{{_latex_escape(sl.title)}}}"]
        if sl.bullets:
            lines.append("\\begin{itemize}")
            lines += [f"  \\item {_latex_escape(b)}" for b in sl.bullets]
            lines.append("\\end{itemize}")
        for fp in sl.figure_paths:
            lines.append(
                f"\\begin{{center}}\\includegraphics[width=0.8\\textwidth]{{{fp}}}"
                f"\\end{{center}}"
            )
        lines.append("\\end{frame}")
        return "\n".join(lines)

    def to_marp(self, bp: PresentationBlueprint) -> str:
        header = (_TPL_DIR / "marp_header.md").read_text() % {
            "title": bp.paper_title,
            "authors": ", ".join(bp.authors),
            "venue": bp.venue,
        }
        out = [header]
        for sl in bp.slides:
            if sl.section == "title":
                continue
            out.append("---\n")
            out.append(f"## {sl.title}\n")
            for b in sl.bullets:
                out.append(f"- {b}")
            for fp in sl.figure_paths:
                out.append(f"\n![w:800]({fp})")
            out.append("")
        return "\n".join(out) + "\n"

    def generate(self, bp: PresentationBlueprint, out_dir: str,
                 output_format: str = "beamer") -> str:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        if output_format == "marp":
            path = out / "slides.md"
            path.write_text(self.to_marp(bp))
        else:
            path = out / "slides.tex"
            path.write_text(self.to_beamer(bp))
        log.info("wrote %s (%d slides)", path, len(bp.slides))
        return str(path)
