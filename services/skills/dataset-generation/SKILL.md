---
name: dataset-generation
description: >-
  Generates synthetic training, fine-tuning, or evaluation datasets by expanding
  seed prompts via Gemma 4 31B. generate_from_seeds produces instruction-response,
  preference, or chat examples from a list of seeds and a format template;
  format_as_jsonl writes the output to a JSONL file; validate_coverage checks that
  the generated set covers all stated requirements. Use when you need to create
  new labeled training data from scratch, expand a small seed set, or build a
  silver dataset for distillation. Distinct from dataset-search (which finds
  existing public datasets rather than creating new data) and web-search (which
  retrieves existing pages and documents). Outputs JSONL files ready for fine-tuning
  pipelines. LLM-assisted; quality depends on the seed quality and template.
version: "0.1.0"
license: MIT
requires: []
---

# dataset-generation Skill

Generates synthetic training data from seed prompts using Gemma 4 31B.

## When to use

- Building a silver dataset for model distillation.
- Expanding a small seed set of instructions into a full training corpus.
- Creating instruction-response, preference, or chat-format examples.
- Generating evaluation data for a new task where no public benchmark exists.

## Tools

- `generate_from_seeds(seeds, template, n_per_seed=3, output_format='instruction-response')`
  — `{examples: [{seed, generated: [{input, output}]}], total_generated}`.
- `format_as_jsonl(examples, output_path)` — `{ok, path, count}`.
- `validate_coverage(examples, requirements)` — `{covered, gaps, coverage_pct}`.

## Constraints

- Quality depends on seed quality and template; validate outputs before training.
- Use `dataset-search` first if a public dataset may already exist.
