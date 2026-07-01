# Seam-hardening — single-source the Python↔TS contract

**Goal:** kill the hand-duplication of the capability-manifest contract (the hottest seam) so a field is
declared ONCE, generated to both languages, and CI blocks drift. From the rewrite analysis
(`docs/rewrite-to-typescript-analysis.md`): don't rewrite — harden the seam.

**Scope (v1): the MANIFEST contract only** — `ToolDescriptor`, `ClientManifest`/`ClientCapabilities`,
the `ToolSource` union, and `BUILTIN_TOOL_NAMES`. (Events/session shapes are a documented follow-up —
they're lower-churn and use unions; do them once the pattern is proven here.)

## Current shapes (must be reproduced byte-for-byte in behavior)
- `ToolSource = 'builtin' | 'mcp' | 'skill'`
- `ToolDescriptor`: `name` (req), `source` (req), `namespace?`, `schema?`, `description?`, `body?`
- Manifest: TS `ClientCapabilities { protocolVersion: number; tools: ToolDescriptor[] }`;
  Python `ClientManifest { protocol_version: int; tools: list[ToolDescriptor] }` — the **one** camel↔snake
  field is `protocolVersion`↔`protocol_version`.
- `BUILTIN_TOOL_NAMES = [read_file, write_file, list_dir, search_files, run_tests]`

## Design — one JSON source, generated both sides
1. **Source of truth:** `contract/manifest.contract.json` — declares the shapes, each field with `{ ts, py,
   required }`, plus a per-field name override where TS≠Python (only `protocolVersion`/`protocol_version`),
   and `builtinToolNames`.
2. **Codegen:** `scripts/gen_contract.py` reads the JSON and emits TWO generated files (with a
   "DO NOT EDIT — generated from contract/manifest.contract.json" header):
   - `services/frontend/src/protocol/contract.generated.ts` — `ToolSource`, `ToolDescriptor`,
     `ClientCapabilities` interfaces + `BUILTIN_TOOL_NAMES`.
   - `services/orchestrator/_contract_generated.py` — `ToolDescriptor`, `ClientManifest` TypedDicts
     (`total=False`) + `BUILTIN_TOOL_NAMES`.
3. **Wire the definitions to the generated files (type-barrels):**
   - `capabilities.ts`: import `ToolSource`/`ToolDescriptor`/`ClientCapabilities`/`BUILTIN_TOOL_NAMES` from
     `./contract.generated.js` and re-export them; build `CLIENT_CAPABILITIES` from `BUILTIN_TOOL_NAMES`;
     keep `capabilitiesFrame` as-is. Existing importers of `capabilities` are unaffected (same surface).
   - `tool_manifest.py`: import `ToolDescriptor`, `ClientManifest` from `._contract_generated` (delete the
     hand-written TypedDicts). All existing logic (`parse_manifest`, tolerant camel/snake handling,
     `build_tool_list`) is UNCHANGED — it just uses the imported types.
   - These two generated files are the **single audited boundary** per language.
4. **CI dual-commit guard** (`.github/workflows/ci.yml`, new job `contract`): run `python
   scripts/gen_contract.py` then `git diff --exit-code -- <the two generated files>` → **fails if a
   generated file is stale** (i.e. someone edited the JSON but didn't regenerate, or hand-edited a
   generated file). This is the dual-commit enforcement: the contract can't change on one side only.
5. **Parity test** (`tests/...`): assert the generated Python TypedDicts' field names == the JSON
   contract, and that a round-tripped frontend-shaped manifest still parses (extend the existing
   `test_doc_skill_integration.py` idea). Frontend: a vitest asserting `contract.generated.ts` field names
   match (or just that `capabilities.ts` re-exports the generated types).

## Invariants
- **No behavior change.** The generated shapes must be equivalent to today's hand-written ones; all
  existing suites (orchestrator, frontend) stay green. `parse_manifest`'s tolerance is untouched.
- **Generated files are committed** (so the app builds without a codegen step at build time); the CI
  guard keeps them current.
- Adding a manifest field later = edit the JSON, run `gen_contract.py`, commit both generated files — CI
  enforces it.

## Follow-ups (not v1)
- Extend the JSON to the event/session shapes (StreamEvent union, ToolCall, Reasoning, Artifact, Turn,
  Session) — the higher-count but lower-churn half of the seam.
- Optionally fold the builtin tool *schemas* (`CANONICAL_BUILTIN_SCHEMAS`) into the contract too.
