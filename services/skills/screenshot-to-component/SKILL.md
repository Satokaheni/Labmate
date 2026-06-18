---
name: screenshot-to-component
description: >
  Converts a UI screenshot or mockup into a React + Tailwind component using a
  3-stage modular pipeline: Gemma 4 vision grounding (detect UI elements and layout),
  layout planning (hierarchical structure), and code generation. Produces shadcn/ui-
  compatible Tailwind components. Chain with react-doctor for immediate quality checking.
trigger: "Use when converting a UI screenshot, mockup, or design image into a React component"
tools:
  - screenshot_to_component.generate
  - screenshot_to_component.ground
  - screenshot_to_component.plan
version: "0.1.0"
license: MIT
requires: [react-doctor]
---

# Screenshot to Component Skill

You have access to the `screenshot_to_component` MCP server, which turns a UI
screenshot or mockup image into a React + Tailwind component. It uses a 3-stage
modular pipeline (grounding → planning → generation) rather than a single
image-in/code-out prompt, which produces more faithful layouts.

## When to Use

- Converting a screenshot, mockup, or design image into a React component
- Inspecting just the detected UI elements (grounding) or the layout plan before
  committing to full code generation

## Available Tools

### `screenshot_to_component.generate`

Run the full pipeline. Returns JSON: `component_code`, `layout_plan`, `output_path`.

```json
{ "image_path": "/mocks/dashboard.png", "framework": "react-tailwind", "output_path": "/out/Dashboard.tsx" }
```

### `screenshot_to_component.ground`

Grounding stage only. Returns JSON with bounding boxes + semantic labels. Useful
for inspecting what the vision model detected.

```json
{ "image_path": "/mocks/dashboard.png" }
```

### `screenshot_to_component.plan`

Planning stage only. Takes a grounding JSON string, returns a hierarchical layout plan.

```json
{ "grounding_result": "{\"elements\": [...], \"image_width\": 1440, \"image_height\": 900}" }
```

## Frameworks

- `react-tailwind` (default): shadcn/ui-compatible React + Tailwind component.
- `html-css`: a single HTML file with plain CSS.
- `vue-tailwind`: a Vue single-file component using Tailwind.

## Chaining

After generation, run `react-doctor` on the produced component to catch
accessibility, type, and lint issues immediately.

## Limitations

- Bounding boxes and colors are inferred by the vision model and are approximate.
- Pixel-perfect reproduction is not a goal; the output is a faithful, editable scaffold.
