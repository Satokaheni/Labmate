---
name: dataset-search
description: >-
  Searches public dataset repositories for an existing labeled CORPUS (a
  collection of training/evaluation data) matching a task, domain, or modality
  requirement. search_hf_hub queries the Hugging Face Hub by keyword, task
  category, and modality; search_papers_with_code queries the PapersWithCode
  dataset index; rank_candidates scores and ranks any candidate list by query
  term overlap. Use ONLY when you need to find an existing dataset for training,
  fine-tuning, or evaluation — not to create new data. Do NOT use this to locate
  a software tool, model, library, package, or source-code repository, or to
  answer a general "where can I find / download X" question — route those to
  web-search instead (e.g. "where can I find Whisper?" wants the project/GitHub
  repo, which is web-search, NOT a Whisper dataset). Distinct from web-search
  (general web pages, projects, and repos) and citation-graph (paper citations,
  not datasets). If no suitable public dataset exists and you need to create one,
  use dataset-generation instead. Deterministic; no LLM calls.
version: "0.1.0"
license: MIT
requires: []
---

# dataset-search Skill

Finds existing public datasets from Hugging Face Hub and PapersWithCode.

## When to use

- Finding a labeled corpus for training or fine-tuning.
- Locating an evaluation benchmark for a given task.
- Gathering candidate datasets to compare before committing to one.

## Tools

- `search_hf_hub(query, task=None, modality=None, max_results=10)` — searches
  HF Hub; returns `{results: [{id, downloads, likes, task_categories, description}]}`.
- `search_papers_with_code(query, max_results=5)` — searches PWC; returns
  `{results: [{name, full_name, description, tasks, url}]}`.
- `rank_candidates(candidates, query, criteria=None)` — lexical scoring; returns
  `{ranked: [{...original, score}]}`.

## Constraints

- Deterministic — no LLM. External HTTP calls require network access.
- Use `dataset-generation` if you need to create synthetic training data.
