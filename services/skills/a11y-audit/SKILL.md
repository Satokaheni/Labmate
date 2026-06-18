---
name: a11y-audit
description: >
  WCAG accessibility audit of rendered HTML/React components using axe-core via
  Playwright. Returns violations with rule IDs, WCAG level, impact severity, and
  DOM selectors. Catches ~57% of WCAG issues automatically. Deterministic — no LLM.
  Use as a post-generation quality gate on any frontend code.
trigger: "Use when checking accessibility compliance of rendered HTML or React components"
tools:
  - a11y_audit.audit_file
  - a11y_audit.audit_url
  - a11y_audit.list_rules
version: "0.1.0"
license: MIT
requires: []
---

# a11y-audit

Runs [axe-core](https://github.com/dequelabs/axe-core) against a rendered DOM in headless
Chromium (Playwright) and reports WCAG violations. This is an automated gate, not a
guarantee — axe-core detects roughly 57% of WCAG issues; the rest need human review.

## Tools

### `a11y_audit.audit_file(html_or_component_path, rules?)`
Resolves the path to a `file://` URL, renders it, injects axe-core, and returns an
`AuditResult` JSON: `violations`, plus counts for `passes`, `incomplete`, `inapplicable`,
and `violation_count`. Pass `rules` (array of axe rule IDs) to restrict the run.

### `a11y_audit.audit_url(url, rules?)`
Same as above for an `http(s)` URL (e.g. a running dev server).

### `a11y_audit.list_rules()`
Returns every axe rule ID with its description and WCAG level (A / AA / AAA).

## Result shape

Each violation carries: `id` (rule), `impact` (critical | serious | moderate | minor),
`description`, `wcag_level`, and `nodes` (each with `html`, `target` CSS selectors, and
`failure_summary`).

## Interpreting results

- Triage by `impact`: fix `critical` and `serious` first.
- `incomplete` means axe could not decide — review those manually.
- A zero `violation_count` does NOT mean fully accessible; it means no automated failures.

## Environment

- `PLAYWRIGHT_BROWSER` — `chromium` (default), `firefox`, or `webkit`.
- The Playwright browser binary must be installed (`playwright install chromium`).
