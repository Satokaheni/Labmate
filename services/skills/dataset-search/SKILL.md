---
name: dataset-search
description: >-
  Searches public dataset repositories for an existing corpus matching a task,
  domain, or modality requirement. search_hf_hub queries the Hugging Face Hub
  by keyword, task category, and modality; search_papers_with_code queries the
  PapersWithCode dataset index; rank_candidates scores and ranks any candidate
  list by query term overlap. Use when you need to find an existing labeled
  dataset for training, fine-tuning, or evaluation — not to create new data.
  Distinct from web-search (which searches general web pages and papers, not
  dataset-specific APIs) and citation-graph (which maps paper citations, not
  datasets). If no suitable public dataset exists and you need to create one,
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
