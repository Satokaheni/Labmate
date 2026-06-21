---
name: arxiv-prep
description: >-
  Prepares a finished LaTeX paper for arXiv or conference submission: flattens
  \input/\include, strips comments and unused assets via arxiv-latex-cleaner,
  verifies the source compiles with tectonic, optionally scrubs author identity
  for double-blind review, and emits an upload-ready tarball plus a
  submission-metadata summary. Use when packaging a completed paper for
  submission, anonymizing a manuscript for blind review, or checking that LaTeX
  source compiles cleanly. Distinct from academic-writing (which drafts the
  content) and paper-to-slides (which builds a talk) — this operates on a
  finished .tex project. Deterministic apart from the optional anonymization check.
version: "0.1.0"
license: MIT
requires: []
---

# arxiv-prep Skill

Packages a finished LaTeX project for arXiv or conference submission.

## When to use

- Packaging a completed paper into an upload-ready tarball.
- Anonymizing a manuscript for double-blind review.
- Verifying that LaTeX source compiles cleanly before submission.

## Tools

- `clean_source(project_dir)` — runs `arxiv-latex-cleaner`; returns `{ok, log}`.
- `verify_compile(project_dir)` — compiles the main `.tex` with `tectonic`;
  returns `{ok, errors}`.
- `anonymize(project_dir)` — returns a unified `{diff, changes}` for approval;
  never edits files in place.
- `package_tarball(project_dir, output_path=None)` — writes `submission.tar.gz`;
  returns `{ok, path}`.
- `extract_metadata(project_dir)` — returns `{title, authors, abstract, category}`.

## Constraints

- `tectonic` is an external binary, not a pip dependency.
- Anonymization returns a diff for human approval; it does not mutate the source.
