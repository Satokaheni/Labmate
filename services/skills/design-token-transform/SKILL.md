---
name: design-token-transform
description: >
  Extracts design tokens (colors, typography, spacing, radii) from Figma via REST
  API and transforms them into CSS variables, Tailwind config, or shadcn/ui globals.
  Use when generating components that should match a Figma design system — makes
  screenshot-to-component output token-aware instead of using hardcoded values.
  Requires FIGMA_ACCESS_TOKEN env var.
trigger: "Use when syncing design tokens from Figma or converting a design system to code"
tools:
  - extract
  - transform
  - extract_and_transform
version: "0.1.0"
license: MIT
requires: []
---

# Design Token Transform Skill

You have access to the `design_token` MCP server. It pulls design tokens from a
Figma file via the Figma REST API and converts them into front-end formats so
generated components match an existing design system instead of using guessed,
hardcoded values.

## When to Use

- Generating a component that must match a Figma design system
- Syncing colors / typography / spacing / radii from Figma into code
- Producing a `tailwind.config.js` theme, CSS custom properties, or shadcn/ui
  HSL variables from a Figma file

## Setup

The server requires the `FIGMA_ACCESS_TOKEN` environment variable (a Figma
personal access token). Without it, every tool returns an error.

## Available Tools

### `extract`

Fetch raw tokens from a Figma file. Returns JSON: a `TokenSet`.

```json
{ "figma_file_key": "abc123", "node_id": "1:2" }
```

`node_id` is optional; omit it to scan the whole document.

### `transform`

Convert a raw `TokenSet` JSON string into a target format.

```json
{ "tokens_json": "{...}", "format": "tailwind" }
```

`format` is one of `tailwind` | `css-vars` | `shadcn`.

### `extract_and_transform`

Extract then transform in one call. Optionally write the result to a file.

```json
{ "figma_file_key": "abc123", "format": "css-vars", "output_path": "/tmp/tokens.css" }
```

## Output Formats

- `tailwind`: a `tailwind.config.js` theme-extension object string.
- `css-vars`: CSS custom properties under `:root { ... }`.
- `shadcn`: shadcn/ui-style HSL variables for `globals.css`.
