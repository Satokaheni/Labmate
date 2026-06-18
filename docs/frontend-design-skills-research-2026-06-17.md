# Frontend Design Skills Research for Labmate

**Date**: 2026-06-17

---

## Executive Summary

The frontend design-to-code landscape has split into two clean halves that map perfectly onto Labmate's two-model architecture. **Visual tasks** (screenshot/mockup/Figma-screenshot → code, design critique, layout grounding) require a multimodal model and route to the **Gemma 4 31B brain**; the strongest research consensus (Design2Code, ScreenCoder, UI-TARS) is that monolithic VLM "image-in, code-out" fails on complex UIs, and that a **modular grounding → planning → generation pipeline** wins. **Text-only tasks** (static React analysis, accessibility linting, design-token transformation, component documentation) are deterministic or text-in/text-out and route to the **Qwen2.5-Coder-32B specialist** — or to plain Node/Rust subprocesses with no LLM at all.

The single most directly adaptable artifact is **react-doctor** (millionco): a deterministic TypeScript CLI that already ships an "agent skill install" mode. It is an almost drop-in MCP skill requiring zero vision and zero LLM inference — the highest value-to-effort item in this report. Beyond it, the highest-leverage builds are a **screenshot-to-component skill** (Gemma 4 vision, ScreenCoder-style decomposition), a **design-critique skill** (Gemma 4 vision), an **accessibility audit skill** (axe-core, text-only), and a **design-token transformation skill** (Style Dictionary / Figma REST API, text-only). The official **Figma MCP server** (GA since late 2025) means token/component extraction is now a connector-integration problem, not a build problem.

The recommended near-term path: ship the deterministic text-only skills first (react-doctor wrapper, axe-core audit, token transform) since they are low-complexity and need no GPU vision time, then build the Gemma 4 vision skills on the proven modular-agent pattern.

---

## react-doctor Analysis

**Repo**: https://github.com/millionco/react-doctor

**What it does specifically**:
- Deterministic **static analysis** CLI ("Your agent writes bad React. This catches it.") — rule-driven, *not* LLM/inference-driven.
- Scans React codebases across five categories: **State & Effects, Performance, Architecture, Security, Accessibility**.
- Framework-aware: Next.js, Vite, TanStack, React Native, Expo.
- Run via `npx react-doctor@latest` at project root; configurable through `doctor.config.ts`.
- Issues carry stable rule IDs (e.g. `react-doctor/no-array-index-as-key`).
- Has a **CI mode** (GitHub Actions) that reports only *newly introduced* issues vs. baseline.
- Ships an **"agent skill install"** mode that teaches coding agents (Claude Code, Cursor, Codex) to avoid the flagged patterns — i.e. the authors already designed it to be consumed by agents.
- Written ~99% TypeScript. Telemetry is anonymous metadata only (no file contents).

**How to adapt it as a Labmate MCP skill**:
- This is the cleanest fit in the whole report. It is deterministic, TypeScript-native, and already agent-oriented.
- Build a thin **TypeScript MCP server** in `services/skills/react-doctor/` that wraps the CLI: spawn `react-doctor` as a child process, parse its JSON output, return structured `tools/call` results. (CLAUDE.md rule #1: the wrapper must emit JSON-RPC on stdout only — pipe react-doctor's own stdout through a parser and send all diagnostic logging to `console.error`.)
- Expose one tool, e.g. `react_doctor_audit({ path, rules?, ci_baseline? })`, returning the rule-ID list + severity + file/line.
- **No Gemma 4 vision required. No LLM inference at all** — this is pure deterministic analysis. The orchestrator can call it directly; if any natural-language summarization of results is wanted, route that text step to Qwen.
- Pairs naturally with the screenshot-to-component skill: generate a component with Gemma 4, then immediately lint it with react-doctor before returning to the user (generation → verification loop).

---

## High-Priority Skills to Build

### react-doctor audit (React static analysis)
**Source**: https://github.com/millionco/react-doctor
**What it does**: Wraps the react-doctor CLI; returns rule-ID-tagged issues across state/effects, performance, architecture, security, accessibility. CI-baseline mode flags only new issues.
**Why for Labmate**: Highest value-to-effort item. Deterministic, no GPU, already designed for agent consumption. Serves as the QA gate after any code generation.
**Implementation**: TypeScript MCP server in `services/skills/react-doctor/` spawning the CLI as a child process.
**Gemma 4 vision required**: No — deterministic, no inference.
**Complexity**: Low.

### screenshot-to-component (visual → React/Tailwind)
**Source**: ScreenCoder (https://arxiv.org/abs/2507.22827); abi/screenshot-to-code (https://github.com/abi/screenshot-to-code, 72k+ stars); Design2Code (https://arxiv.org/abs/2403.03163)
**What it does**: Takes a UI screenshot/mockup and produces a React + Tailwind (shadcn/ui-style) component. The research consensus is to NOT do this monolithically — decompose into **grounding** (VLM detects bounding boxes + semantic labels: header, nav, sidebar, card), **planning** (hierarchical layout), **generation** (synthesize code from detected semantics). ScreenCoder's agentic pipeline (0.755 block match) beats monolithic GPT-4o (0.730).
**Why for Labmate**: This is the marquee capability Gemma 4 multimodality unlocks. Local, private, no API keys (cf. OpenKombai using local Llama 3.2 Vision + Qwen 2.5 Vision for exactly this).
**Implementation**: Multi-stage skill. Grounding + planning stages call Gemma 4 (vision) via the orchestrator; the final code-synthesis stage can be routed to Qwen2.5-Coder (text-only, given the structured layout plan) for better code quality. Output should target shadcn/ui + Radix + Tailwind token conventions (see design-token skill). Chain into react-doctor audit before returning.
**Gemma 4 vision required**: Yes for grounding/planning (image input). The synthesis step is text-only and is a good Qwen handoff.
**Complexity**: High.

### design-critique (screenshot → actionable UX/visual feedback)
**Source**: Design2Code findings (models struggle with layout fidelity / element recall — the inverse skill is critiquing those gaps); general VLM-as-judge pattern.
**What it does**: Takes a screenshot of a rendered UI (or a Figma export) and returns structured critique: visual hierarchy, spacing/alignment consistency, contrast, layout balance, responsive concerns. Output is a checklist, not prose.
**Why for Labmate**: Pure consumption of Gemma 4 vision with no code-gen complexity — a fast win that demonstrates multimodal value. Useful both standalone and as a self-review step after screenshot-to-component.
**Implementation**: Single-shot Gemma 4 vision call behind a SKILL.md + structured-output prompt. Could render a component to a screenshot (headless browser via a sub-skill) then critique it — closing a generate → render → critique loop.
**Gemma 4 vision required**: Yes — image input is the entire point.
**Complexity**: Low–Medium.

### accessibility-audit (axe-core)
**Source**: https://github.com/dequelabs/axe-core; axe-playwright; Storybook a11y addon
**What it does**: Runs axe-core against rendered components (via Playwright/headless Chromium) and returns WCAG violations/passes/incompletes with rule IDs and DOM selectors. axe-core catches ~57% of WCAG issues automatically.
**Why for Labmate**: Accessibility is a category react-doctor only partially covers (static); axe-core covers the *rendered* DOM. Deterministic, no GPU, strong complement to both react-doctor and screenshot-to-component output.
**Implementation**: TypeScript MCP server in `services/skills/a11y-audit/` that boots Playwright, injects axe, calls `checkA11y`, returns JSON. Reuse the same headless-browser harness as design-critique's render step.
**Gemma 4 vision required**: No — deterministic DOM analysis. (Optionally, a Gemma 4 vision pass could catch *visual* a11y issues axe can't, e.g. perceived contrast in images — future enhancement.)
**Complexity**: Medium (Playwright/headless browser setup is the main cost).

### design-token-transform (Style Dictionary / Figma tokens)
**Source**: Style Dictionary; Figma REST API (https://developers.figma.com/docs/rest-api/); official Figma MCP server; Framelink MCP (open source)
**What it does**: Pulls design tokens (colors, typography, spacing, radii) from a Figma file via REST API and transforms them into CSS variables / Tailwind config / shadcn `--primary`-style tokens. Deterministic transformation pipeline.
**Why for Labmate**: Makes screenshot-to-component output *token-aware* instead of emitting hardcoded values — the difference between "90% there" and a throwaway. Also a standalone design-system utility.
**Implementation**: Python or TypeScript MCP server. Two parts: (a) Figma REST fetch (needs a Figma access token via env var per CLAUDE.md service-URL conventions), (b) Style-Dictionary-style transform to target format. Consider integrating the **official Figma MCP server** or **Framelink MCP** as the connector rather than re-implementing Figma fetch.
**Gemma 4 vision required**: No — pure data transformation, text/JSON in and out. Good Qwen-or-no-LLM task.
**Complexity**: Low–Medium.

---

## Medium-Priority Candidates

### figma-to-component (live Figma → React)
**Source**: Official Figma MCP server (GA late 2025; remote server, no desktop app needed); Framelink MCP; Locofy/Anima as references.
Distinct from screenshot-to-component: uses *structured* Figma data (variables, components, variants, auto-layout) instead of pixels, which yields far higher fidelity ("~90% there" with a well-structured file). Best implemented by wiring the official/Framelink Figma MCP server in as a connector and letting Qwen synthesize from the structured spec. **Vision: No** (structured data, not image). Complexity: Medium (mostly integration).

### component-doc-gen (Storybook / props documentation)
Auto-generate component documentation, prop tables, and Storybook stories from React source. Text-only, Qwen-suited (or AST-based, deterministic). Pairs with the existing `ast-repo-map` skill direction in CLAUDE.md. **Vision: No.** Complexity: Medium.

### bundle/dependency-analysis
Programmatic bundle-size and dependency analysis (e.g. wrapping existing analyzers) to flag heavy/duplicate deps in generated frontends. Deterministic, text-only. **Vision: No.** Complexity: Low–Medium.

### visual-regression (programmatic)
Render component → screenshot → diff against baseline (Chromatic-style, but local). Deterministic pixel diff for the core, but Gemma 4 vision could *explain* a diff ("the button shifted 8px and lost its shadow"). The diff itself needs no vision; the explanation step is an optional Gemma 4 enhancement. **Vision: Optional.** Complexity: Medium.

### sketch-to-prototype (low-fidelity wireframe → code)
Per Sketch2Code (FSE 2025): hand-drawn/low-fi wireframe → working prototype. Same modular grounding→generation pattern as screenshot-to-component but tolerant of rough input — good for early ideation. **Vision: Yes.** Complexity: High (overlaps heavily with screenshot-to-component; build that first).

---

## Papers Worth Reading

Ranked by direct implementation value to Labmate:

1. **ScreenCoder: Advancing Visual-to-Code Generation via Modular Multimodal Agents** (2025) — https://arxiv.org/abs/2507.22827. *Most actionable.* Defines the grounding → planning → generation multi-agent pipeline, with code released and a HuggingFace demo. Directly fine-tunes/reinforces **Qwen2.5-VL** for UI understanding — i.e. it validates the exact two-stage (VLM-grounds, code-model-generates) split that maps onto Gemma 4 + Qwen. Block-match 0.755 vs GPT-4o 0.730.

2. **Design2Code: Benchmarking Multimodal Code Generation for Automated Front-End Engineering** (NAACL 2025) — https://arxiv.org/abs/2403.03163. The reference benchmark (484 real webpages + Design2Code-Hard). Use its metrics (CLIP score, CW-SSIM, TreeBLEU, element-level position/text/color matching) to **evaluate** the screenshot-to-component skill. Key finding: models struggle with visual element recall and layout fidelity — informs the design-critique skill's checklist.

3. **UI-TARS: Pioneering Automated GUI Interaction with Native Agents** (2025) — https://arxiv.org/abs/2501.12326. Screenshot-only native GUI agent (ByteDance). Less directly about code-gen, but its **GUI grounding** approach (ScreenSpot-Pro SOTA) is the technique behind the grounding stage of screenshot-to-component, and its System-2 reasoning (decomposition, reflection, milestones) is a reusable orchestration pattern.

4. **CogAgent: A Visual Language Model for GUI Agents** (CVPR 2024) — high-resolution visual encoders for UI element localization from raw screenshots, no HTML tree. Background reading on why pixel-level grounding works and the resolution requirements (relevant to how Gemma 4 should be fed UI images).

5. **A Survey on Benchmarks of LLM-based GUI Agents** (2025) — techrxiv. Map of the eval landscape (Mind2Web, WebArena, ScreenSpot/-V2/-Pro, ScreenQA, OmniACT). Use to pick eval sets if Labmate's UI skills need benchmarking.

6. **Web2Code** (NeurIPS 2024 D&B) and **Sketch2Code** (FSE 2025) — secondary. Web2Code is a large webpage→code dataset (useful if fine-tuning is ever considered); Sketch2Code informs the low-fidelity wireframe candidate.

---

## Notes on the Gemma 4 / Qwen Routing Split

| Skill | Visual step (→ Gemma 4) | Text step (→ Qwen / deterministic) |
|-------|--------------------------|-------------------------------------|
| react-doctor audit | — | All (deterministic CLI) |
| accessibility-audit | — (optional visual a11y pass) | All (axe-core deterministic) |
| design-token-transform | — | All (data transform) |
| component-doc-gen | — | All (AST / Qwen) |
| design-critique | Image → critique (entire skill) | Optional prose formatting |
| screenshot-to-component | Grounding + layout planning | Code synthesis from structured plan |
| figma-to-component | — (structured data, not pixels) | Code synthesis |
| visual-regression | Optional: explain a pixel diff | Pixel diff (deterministic) |

The general rule: **anything that takes an image as input is the only thing that must touch Gemma 4.** Once an image has been turned into a structured representation (bounding boxes + semantics, or a layout plan, or extracted tokens), every downstream code/text step is a Qwen or deterministic task — which keeps scarce GPU vision time focused and lets the code specialist do what it's best at.

---

## Sources

- react-doctor: https://github.com/millionco/react-doctor
- Design2Code (paper): https://arxiv.org/abs/2403.03163
- Design2Code (repo): https://github.com/NoviScl/Design2Code
- ScreenCoder: https://arxiv.org/abs/2507.22827
- UI-TARS: https://arxiv.org/abs/2501.12326
- CogAgent / SeeClick / GUI grounding landscape: https://github.com/harpreetsahota204/gui_agent_research_landscape
- A Survey on Benchmarks of LLM-based GUI Agents: https://www.techrxiv.org/doi/pdf/10.36227/techrxiv.176591818.87526814/v1
- Universal Visual Grounding for GUI Agents: https://arxiv.org/pdf/2410.05243
- abi/screenshot-to-code (72k+ stars): https://github.com/abi/screenshot-to-code
- Mrxyy/screenshot-to-page (Qwen-VL): https://github.com/Mrxyy/screenshot-to-page
- axe-core: https://github.com/dequelabs/axe-core
- Storybook accessibility testing: https://storybook.js.org/docs/writing-tests/accessibility-testing
- Chromatic accessibility regression: https://www.chromatic.com/docs/accessibility/
- Figma REST API: https://developers.figma.com/docs/rest-api/
- Figma MCP / design-to-code workflows: https://blog.logrocket.com/ux-design/design-to-code-with-figma-mcp/
- Awesome-Multimodal-LLM-for-Code: https://github.com/xjywhu/Awesome-Multimodal-LLM-for-Code
- shadcn/ui (Radix + Tailwind reference): https://ui.shadcn.com/
- v0 / Locofy / Anima comparison: https://medium.com/@mehrnooshakbarizadeh/generative-ai-for-front-end-development-comparing-anima-locofy-ai-and-vercel-v0-c2feb4c2eeea
- Vercel v0.dev review: https://skywork.ai/blog/vercel-v0-dev-review-2025-ai-ui-react-tailwind/
