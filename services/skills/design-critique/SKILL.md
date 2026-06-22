---
name: design-critique
description: >
  Structured UX/visual critique of UI screenshots using Gemma 4 vision.
  Returns a per-area checklist (visual hierarchy, spacing, contrast, typography,
  layout, accessibility surface) with pass/fail/warning status per item.
  Use as a self-review step after generating UI components, or standalone to
  audit an existing design.
trigger: "Use when reviewing a UI design, screenshot, or rendered component for quality"
tools:
  - critique
  - compare
version: "0.1.0"
license: MIT
requires: []
---

# design-critique

Single-shot Gemma 4 vision critique of UI screenshots.

## Tools

### `critique(image_path, focus_areas?)`
Critique one UI screenshot. `focus_areas` is an optional subset of:
`visual_hierarchy`, `spacing_alignment`, `color_contrast`, `typography`,
`layout_balance`, `responsive_concerns`, `accessibility_surface`.
Returns a JSON `CritiqueResult` with a per-item checklist and an overall verdict.

### `compare(before_path, after_path)`
Send a before/after pair in one vision call and return a diff-focused critique
(what improved, what regressed, what is still unresolved).

## Notes
- Images are re-encoded to base64 PNG before being sent to Gemma 4.
- Output is JSON, never prose. Each item carries `status`, `severity`, and a `note`.
