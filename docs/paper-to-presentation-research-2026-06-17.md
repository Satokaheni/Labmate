# Paper-to-Presentation Research for Labmate

**Date**: 2026-06-17

---

## Executive Summary

Paper-to-slides generation is a mature, well-researched task with a clear winning architecture for a local agent: **generate LaTeX Beamer source directly from the paper, then compile-and-self-correct in a loop.** This is the approach taken by nearly every strong 2024-2025 system (paper2slides, Paper2Video/PaperTalker, SlideBot, Auto-Slides) because it keeps everything in the LaTeX ecosystem, preserves math/citations/figures natively, and turns the hard layout problem into a text-summarization problem the LLM is good at. The dominant pattern is **multi-agent**: a planner produces a structured outline (IMRaD-mapped), a generator emits Beamer code per slide, and a verifier compiles and feeds errors back. Figures are handled two ways — cheap (infer from captions, no vision) or rich (Gemma 4 vision describes/places figures). For Labmate, the recommended build is a single `paper-to-slides` skill that consumes the existing pdf-parse output (text + figures + captions), produces a Beamer outline then full `.tex`, compiles with a self-correction loop, and optionally generates speaker notes — with Marp Markdown as a lighter-weight alternative output for non-LaTeX users. The whole pipeline runs locally with Qwen2.5-Coder for the LaTeX generation and Gemma 4 vision only for figure analysis.

---

## Recommended Architecture for Labmate

Pipeline (each stage is a node; fits the LangGraph orchestrator pattern):

```
pdf-parse skill (existing)
   → paper text (sections) + figures (PNG + captions) + tables
         │
         ▼
[1] Outline Planner  (Gemma 4 brain or Qwen text)
   - Map paper to IMRaD slide plan: title, outline, intro(1-2),
     methods(N), results(N), discussion(1-2), conclusion, refs
   - Emits structured JSON blueprint: per-slide title, source section,
     bullet candidates, which figures/tables to attach
         │
         ▼
[2] Figure Triage  (Gemma 4 VISION) — optional but high value
   - For each candidate figure: describe content, decide keep/drop,
     generate a one-line "what this shows" caption for the slide,
     suggest alt-text / speaker-note explanation
         │
         ▼
[3] Per-slide Content Generator  (Qwen2.5-Coder-32B)
   - For each blueprint slide, emit Beamer LaTeX:
     frame, bullets, \includegraphics for figures, tabular for tables,
     preserve equations verbatim from source
   - Build ADDITIONAL.tex with all \newcommand/\usepackage from paper
         │
         ▼
[4] Compile + Self-Correct loop  (Qwen + pdflatex/tectonic)
   - Run pdflatex (or tectonic, no system TeX needed), capture log
   - On error: feed faulty .tex + log back to LLM for targeted fix
   - Optional chktex lint pass
         │
         ▼
[5] Speaker Notes Generator  (Qwen or Gemma)
   - Separate pass: per-slide talk track, timed to target duration
         │
         ▼
OUTPUT: slides.tex + slides.pdf  (+ notes.md, + optional Marp .md / .pptx)
```

**Which model does what:**

| Stage | Model | Why |
|-------|-------|-----|
| Outline planning | Gemma 4 31B (brain) | Whole-paper reasoning, structure |
| Figure triage/description | **Gemma 4 vision** | Only stage that truly needs vision |
| Beamer/code generation | Qwen2.5-Coder-32B | Best at LaTeX/code, math fidelity |
| Compile self-correction | Qwen2.5-Coder-32B | Reads compiler logs, patches code |
| Speaker notes | Gemma 4 or Qwen | Prose generation |

**Key insight from the literature:** vision is *optional*. paper2slides (the most-cited OSS tool) deliberately uses **no vision** — it infers figure content from LaTeX captions and just `\includegraphics` the file. Gemma 4 vision is an *enhancement* (better figure selection, alt-text, describing figures that lack good captions), not a requirement. Build the caption-based path first; add vision triage second.

---

## High-Priority Skills/Components to Build

### paper-to-slides (Beamer generator)
**Source**: [takashiishida/paper2slides](https://github.com/takashiishida/paper2slides) (88 stars, MIT) is the cleanest reference implementation; [Auto-Slides](https://github.com/Westlake-AGI-Lab/Auto-Slides) (ICME 2026) and [SlideBot](https://arxiv.org/abs/2511.09804) for the multi-agent structure; [Paper2Video/PaperTalker](https://github.com/showlab/Paper2Video) (NeurIPS 2025) for the Beamer-from-LaTeX + MCTS layout refinement approach.
**What it does**: Consumes parsed paper, produces Beamer `.tex` and compiled PDF via an outline → generate → compile-and-fix loop. Handles math, citations, figures natively.
**Implementation path**: Python MCP server in `services/skills/paper-to-slides/`, with a SKILL.md. Calls inference via the vLLM HTTP API. Shells out to `tectonic` (preferred — self-contained, no system TeX install) or `pdflatex`.
**Output formats**: LaTeX Beamer → PDF (primary).
**Gemma 4 vision required**: No (caption-based). Vision is an opt-in enhancement.
**Complexity**: Medium. The self-correction compile loop is the main engineering; everything else is prompt + template.

### outline-planner (paper → slide blueprint)
**Source**: D2S/SciDuet two-step pattern ([D2S, NAACL 2021](https://aclanthology.org/2021.naacl-main.111/), [IBM/document2slides](https://github.com/IBM/document2slides)); DOC2PPT structure/layout prediction ([arXiv 2101.11796](https://arxiv.org/abs/2101.11796)).
**What it does**: Produces the structured JSON blueprint (slide titles, section mapping, bullet candidates, figure/table assignments, target slide counts) consumed by the generator. Decouples "what goes on each slide" from "how to render it" — this separation is what every strong system does.
**Implementation path**: A Python class/node inside the paper-to-slides skill (not a separate skill). Pure text, uses Gemma brain.
**Output formats**: JSON blueprint (intermediate).
**Gemma 4 vision required**: No.
**Complexity**: Low-Medium.

### figure-triage (Gemma 4 vision)
**Source**: PosterAgent Parser in [Paper2Poster](https://github.com/Paper2Poster/Paper2Poster) (NeurIPS 2025) — distills paper into a structured visual asset library with visual-in-the-loop feedback; SciDoc2Diagrammer ([arXiv 2409.19242](https://arxiv.org/pdf/2409.19242)).
**What it does**: Looks at each extracted figure image, decides which to include, writes a slide-ready one-liner and a speaker-note explanation, flags figures too dense for a slide.
**Implementation path**: Optional vision node in paper-to-slides; calls Gemma 4 multimodal endpoint with the figure PNG from pdf-parse.
**Output formats**: Annotations merged into the blueprint.
**Gemma 4 vision required**: **Yes** — this is the one component that uses it.
**Complexity**: Medium (only after the text path works).

### compile-fix loop
**Source**: Universal across paper2slides, SlideBot, Auto-Slides, PaperTalker — all use compile → capture log → LLM repair → recompile.
**What it does**: Robustness layer that makes LLM-generated LaTeX actually compile. The single highest-ROI reliability component.
**Implementation path**: Inner loop in the paper-to-slides skill, max N retries.
**Output formats**: Validated `.tex` + `.pdf`.
**Gemma 4 vision required**: No.
**Complexity**: Low (but essential).

---

## Medium-Priority / Nice to Have

- **Marp Markdown output** ([marp.app](https://marp.app/), [marp-team/marp-cli](https://github.com/marp-team/marp-cli)) — A second, lighter output backend. Marp CLI converts Markdown to PDF/HTML/**PPTX**. HTML comments become PowerPoint speaker notes; `--notes` exports notes as text. Much simpler than Beamer, no TeX dependency, but weaker math/layout. Good "give me an editable PPTX" path. Used by the [LLMy Way hackathon project](https://arxiv.org/pdf/2411.15221) (summarize → Markdown → Marp PDF). **Low complexity** if Beamer path already exists — reuse the blueprint, swap the renderer.
- **python-pptx direct PPTX** ([docs](https://python-pptx.readthedocs.io/)) — For users who need a native editable `.pptx` matching a corporate/conference template. More control than Marp's PPTX export (native shapes, placeholders, images via `slide.shapes.add_picture`), but you manage layout manually. Reference OSS: [barun-saha/slide-deck-ai](https://github.com/barun-saha/slide-deck-ai) (LLM→JSON→python-pptx, supports offline Ollama), [icip-cas/PPTAgent](https://github.com/icip-cas/PPTAgent) (4.4k stars, edit-based on a reference deck + PPTEval). **Medium-High complexity** — layout is fiddly. Defer unless template-matching is a hard requirement.
- **Speaker-notes / talk-script generator** — Separate pass producing a slide-by-slide talk track timed to a target duration. [Paper2Slides_Prompt](https://github.com/mokizzz/Paper2Slides_Prompt) does exactly this (deck + script with timing cues). Low complexity, high user value.
- **Personalization from example deck** — [SlideTailor, arXiv 2512.20292](https://arxiv.org/abs/2512.20292): condition generation on a user-provided paper+slides example pair to implicitly capture their style. Nice future feature; not needed for v1.
- **Template/style transfer** — PPTAgent's reference-deck analysis (extract slide functional types + schemas, then edit). Relevant only if matching an existing house style.

---

## Slide Structure Recommendation

Map IMRaD to slides; default to a ~20-minute conference talk (the most common case). Rule of thumb from presentation guidance: **~1 slide per 2 minutes, ~10 content slides for 20 min, ~5 for a 10-min slot.** Make the count a skill parameter driven by talk duration.

| Section | Slides (20-min talk) | Content |
|---------|---------------------|---------|
| Title | 1 | Paper title, authors, venue, affiliation |
| Outline | 0-1 | Optional roadmap (skip for short talks) |
| Introduction | 1-2 | Problem, motivation ("why care?"), contribution bullets |
| Methods / Approach | 2-3 | Core method; lead with the key figure/diagram |
| Results | 2-3 | Main results table/plot; one finding per slide |
| Discussion | 1 | Interpretation, limitations, "so what?" |
| Conclusion | 1 | Takeaways (not a repeat of discussion) |
| References / Thanks / Q&A | 1 | Acknowledgements, contact, key citations |

Design rules to bake into the generator prompt: **one idea per slide; minimal text (slides are not a teleprompter); lead with figures; preserve equations verbatim; high contrast.** IMRaD's hourglass shape (broad → narrow → broad) should guide pacing.

---

## Output Format Recommendation

For a local autonomous agent, **LaTeX Beamer (→ PDF) is the primary format; Marp Markdown (→ PPTX/PDF) is the lightweight secondary.**

| Format | Pros | Cons | Verdict for Labmate |
|--------|------|------|---------------------|
| **Beamer** | Native math/citations/figures; LLMs generate it well; pure-text artifact (git-friendly); compile-error self-correction works; matches academic norms; `tectonic` needs no system TeX | Output is PDF (not editable in PowerPoint); compile dependency | **Primary.** Best fidelity for academic papers; the whole field converged here. |
| **Marp Markdown** | Dead-simple; one Markdown file; exports PDF/HTML/**PPTX**; speaker notes via comments; no TeX | Weaker math/layout; PPTX export needs Chromium | **Secondary.** Offer when user wants editable PPTX or hates LaTeX. Reuse the same blueprint. |
| **python-pptx** | Native editable PPTX; template-matching; fine-grained control | Manual layout; most engineering; no math | **On-demand only.** Build if a user must match a specific .pptx template. |

Recommendation: ship Beamer first (covers the core academic use case), add Marp as a thin second renderer over the same outline blueprint, treat python-pptx as a later template-matching add-on. Use **tectonic** rather than a full TeX Live install to keep the skill container lean.

---

## Papers Worth Reading

1. **D2S: Document-to-Slide Generation via Query-Based Text Summarization** (NAACL 2021) — [arXiv 2105.03664](https://arxiv.org/abs/2105.03664) / [ACL](https://aclanthology.org/2021.naacl-main.111/). Foundational two-step approach (retrieve by slide title → long-form QA summarize). Introduces the **SciDuet** dataset ([GEM/SciDuet on HF](https://huggingface.co/datasets/GEM/SciDuet)). Start here.
2. **DOC2PPT: Automatic Presentation Slides Generation from Scientific Documents** ([arXiv 2101.11796](https://arxiv.org/abs/2101.11796)) — Defines the task: summarization + image/text retrieval + structure/layout prediction. ~6K paired docs+decks.
3. **PPTAgent: Generating and Evaluating Presentations Beyond Text-to-Slides** (EMNLP 2025, [arXiv 2501.03936](https://arxiv.org/html/2501.03936v3), [code 4.4k★](https://github.com/icip-cas/PPTAgent)) — Edit-based two-stage agent + **PPTEval** (Content/Design/Coherence). Best evaluation framework to borrow.
4. **Paper2Video / PaperTalker** (NeurIPS 2025 SEA, [arXiv 2510.05096](https://arxiv.org/html/2510.05096v2), [code](https://github.com/showlab/Paper2Video)) — Multi-agent, generates **Beamer directly from LaTeX**, MCTS layout refinement, 101-paper benchmark. Closest to Labmate's goal at the high end.
5. **Paper2Poster / PosterAgent** (NeurIPS 2025 D&B, [code](https://github.com/Paper2Poster/Paper2Poster), [site](https://paper2poster.github.io/)) — Parser→Planner pipeline for compressing 20K-token papers into structured visual assets; directly relevant to figure triage.
6. **SlideBot** ([arXiv 2511.09804](https://arxiv.org/abs/2511.09804)) — Multi-agent (Moderator/Retriever/Code Generator/Enhancer), Beamer, grounded in Cognitive Load Theory for slide design principles.
7. **Auto-Slides** (ICME 2026, [arXiv 2509.11062](https://arxiv.org/abs/2509.11062), [code](https://github.com/Westlake-AGI-Lab/Auto-Slides)) — Interactive multi-agent, JSON blueprint → Beamer, preserves formulas. Good blueprint-schema reference.
8. **SlideTailor** ([arXiv 2512.20292](https://arxiv.org/abs/2512.20292)) — Personalization from an example paper+slides pair. Future enhancement.
9. **SlideSpawn** ([arXiv 2411.17719](https://arxiv.org/abs/2411.17719)) — ILP-based salience selection; useful as a non-LLM extractive baseline for comparison.

---

## Sources

- [SlideSpawn (arXiv 2411.17719)](https://arxiv.org/abs/2411.17719)
- [SlideGen (arXiv 2512.04529)](https://arxiv.org/pdf/2512.04529)
- [SlideTailor (arXiv 2512.20292)](https://arxiv.org/abs/2512.20292)
- [DOC2PPT (arXiv 2101.11796)](https://arxiv.org/abs/2101.11796)
- [Paper2Video (arXiv 2510.05096)](https://arxiv.org/html/2510.05096v2) / [showlab/Paper2Video](https://github.com/showlab/Paper2Video)
- [D2S (arXiv 2105.03664)](https://arxiv.org/abs/2105.03664) / [ACL Anthology](https://aclanthology.org/2021.naacl-main.111/) / [IBM/document2slides](https://github.com/IBM/document2slides) / [GEM/SciDuet](https://huggingface.co/datasets/GEM/SciDuet)
- [PPTAgent (arXiv 2501.03936)](https://arxiv.org/html/2501.03936v3) / [icip-cas/PPTAgent](https://github.com/icip-cas/PPTAgent)
- [Paper2Poster](https://github.com/Paper2Poster/Paper2Poster) / [site](https://paper2poster.github.io/) / [NeurIPS proceedings PDF](https://proceedings.neurips.cc/paper_files/paper/2025/file/17337b1d5eeac8b59c80e025a552fa7a-Paper-Datasets_and_Benchmarks_Track.pdf)
- [SlideBot (arXiv 2511.09804)](https://arxiv.org/abs/2511.09804)
- [Auto-Slides (arXiv 2509.11062)](https://arxiv.org/abs/2509.11062) / [Westlake-AGI-Lab/Auto-Slides](https://github.com/Westlake-AGI-Lab/Auto-Slides) / [site](https://auto-slides.github.io/)
- [takashiishida/paper2slides](https://github.com/takashiishida/paper2slides)
- [mokizzz/Paper2Slides_Prompt](https://github.com/mokizzz/Paper2Slides_Prompt)
- [SciDoc2Diagrammer-MAF (arXiv 2409.19242)](https://arxiv.org/pdf/2409.19242)
- [Textual-to-Visual Iterative Self-Verification for Slide Generation (arXiv 2502.15412)](https://arxiv.org/pdf/2502.15412)
- [LLM Hackathon Materials Science / LLMy Way (arXiv 2411.15221)](https://arxiv.org/pdf/2411.15221)
- [Marp](https://marp.app/) / [marp-team/marp-cli](https://github.com/marp-team/marp-cli)
- [python-pptx docs](https://python-pptx.readthedocs.io/)
- [barun-saha/slide-deck-ai](https://github.com/barun-saha/slide-deck-ai)
- [OpenDCAI/Paper2Any](https://github.com/OpenDCAI/Paper2Any)
- [hugohe3/ppt-master](https://github.com/hugohe3/ppt-master)
- [presenton/presenton](https://github.com/presenton/presenton)
