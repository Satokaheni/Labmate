---
name: figma-to-component
description: >
  Converts a Figma component or frame into a React + Tailwind component using
  the Figma REST API for structured data extraction (not screenshot pixels).
  Higher fidelity than screenshot-to-component for well-structured Figma files
  with auto-layout and design variables. Requires FIGMA_ACCESS_TOKEN env var.
trigger: "Use when converting a specific Figma component or frame to React code"
tools:
  - figma_to_component.convert
  - figma_to_component.inspect
version: "0.1.0"
license: MIT
requires: [design-token-transform]
---
