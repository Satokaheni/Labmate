---
name: rebuttal-response
description: >-
  Builds a structured author response to peer-review comments. parse_reviews
  decomposes reviewer text into an itemized concern matrix (severity, type,
  target section); draft_response generates point-by-point replies grounded in
  the paper via paper-rag and validated by citation-check; coverage_audit
  confirms every reviewer point is addressed and flags unaddressed ones. Use when
  responding to reviewer comments, writing an ARR or conference rebuttal, or
  planning a revision from a decision letter. Requires the paper (via pdf-parse)
  and the review text. Pairs with critique for self-review of the drafted response.
version: "0.1.0"
license: MIT
requires: ["pdf-parse", "paper-rag", "citation-check"]
---

# rebuttal-response Skill

Turns reviewer comments into a structured, audited author response.

## When to use

- Responding to reviewer comments for a conference or ARR rebuttal.
- Planning a revision from a decision letter.

## Tools

- `parse_reviews(review_text)` — `{concerns: [{id, severity, type, target_section, text}]}`.
- `draft_response(concerns, paper_context)` — `{responses: [{concern_id, response}]}`
  (one Gemma call per concern).
- `coverage_audit(concerns, responses)` — `{covered, gaps, coverage_pct}`.

## Constraints

- Replies must be grounded in the provided paper context; never invent results.
- Pair with `critique` to self-review the drafted response before sending.
