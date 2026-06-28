# Vision Endpoint — Design Spec (2026-06-28)

**Status:** Approved design. Built on `feat/vision-endpoint`; **deferred merge** until the vision model is needed.

## Problem

Two skills are image-in and cannot run on the text-only stack: `design-critique` (UX/visual critique of a UI screenshot) and `screenshot-to-component` (screenshot → React+Tailwind component). Both already encode the image to a base64 PNG `image_url` and call litellm — but they target `GEMMA_BASE` (`:8000`), which serves the text-only `gemma-4-31B` GGUF (no `--mmproj`). The live skill suite correctly gates them as skipped ("vision endpoint not configured"). This spec adds a real vision endpoint **without touching the text harness or its context window**.

## Constraints

- Single box, **two GPUs**: GPU 0 = 32GB (text), GPU 1 = 3070 Ti 8GB (vision).
- The A/B-validated `gemma-4-31B` text model on `:8000` and its **full context window** must be untouched (a co-resident vision model on GPU 0 would steal KV-cache VRAM → smaller context → harness regression; avoided by using GPU 1).
- Local/self-hosted; llama.cpp; OpenAI-compatible; no Docker.
- Additive & opt-in: with no vision endpoint configured, behavior is exactly as today (the two skills cleanly skip; text-only/single-GPU deploys unaffected).
- stdout-sacred in MCP servers; `api_key="not-needed"` + `thinking_budget_tokens` on every llama.cpp call; no tiktoken.

## Architecture

Two `llama-server` instances on one host, pinned to separate GPUs:

- **GPU 0 (32GB):** `gemma-4-31B` text model on `:8000` — unchanged (`GEMMA_BASE`).
- **GPU 1 (3070 Ti, 8GB):** vision model (GGUF + `mmproj`) on `:8001`, launched with `CUDA_VISIBLE_DEVICES=1`. Reached via `VISION_BASE=http://localhost:8001/v1`.

The two vision skills target `VISION_BASE` / `VISION_MODEL`; everything else stays on `GEMMA_BASE`. `VISION_BASE` unset → the skills return a clean "vision endpoint not configured" result (preserving today's gated-skip).

**Model:** Gemma 3 **4B** vision (instruct/QAT) Q4 + its `mmproj` (~4–6GB — comfortable on 8GB). `VISION_MODEL` is a knob; 12B-Q4 (~7GB) may be tried later if it fits. Gemma 3 has llama.cpp vision GGUFs+mmproj; Gemma 4 does not — acceptable since the vision model only serves the two design skills, not the harness.

## Components / units

1. **`infrastructure/local/serve-vision.sh`** — mirrors `serve-model.sh` (idempotent, health-wait on `:8001/health`), launches the 2nd server pinned to GPU 1:
   `CUDA_VISIBLE_DEVICES=1 llama-server -m $VISION_MODEL_GGUF --mmproj $VISION_MMPROJ --port 8001 ...`.
   Config via env: `VISION_MODEL_GGUF`, `VISION_MMPROJ`, `VISION_PORT` (default 8001), `VISION_NGL`, `VISION_CTX`.
2. **`infrastructure/local/status.sh`** — add a `:8001/health` check (reported, not required — green only when the vision server is up).
3. **`infrastructure/local/start.sh`** — start the vision server **only if** `VISION_MODEL_GGUF` exists (so single-GPU/text-only deploys never try). Otherwise no-op.
4. **`infrastructure/local/install.sh`** — download the Gemma 3 4B vision GGUF + mmproj into `models/` (guarded; skip if present).
5. **`infrastructure/local/local.env`** — add `VISION_BASE` (`http://localhost:8001/v1` on the dual-GPU host; **unset/empty by default elsewhere**), `VISION_MODEL`, and the serve-vision knobs.
6. **`services/skills/design-critique/critic.py`** — read `VISION_BASE`/`VISION_MODEL` (was `GEMMA_BASE`/`GEMMA_MODEL`). If `VISION_BASE` is empty → raise/return a clear "vision endpoint not configured (set VISION_BASE)" error instead of hitting the text model.
7. **`services/skills/screenshot-to-component/llm.py`** — same switch (it hardcodes `GEMMA_BASE`); the "SINGLE-GPU: every call targets GEMMA_BASE" comment becomes vision-specific.
8. **Shared resolution helper** — a tiny pure function `resolve_vision_endpoint() -> (base, model) | None` (returns None when unconfigured) so both skills share identical disabled-when-unset semantics and it's unit-testable. Lives per-skill or in a shared util mirrored in each (skills are independently packaged — duplicate a 6-line helper rather than introduce a cross-skill import).

## Data flow

agent goal (UI image) → skill `call_tool(image_path)` → re-encode to base64 PNG → litellm `image_url` content part → `VISION_BASE` (`:8001`, GPU 1 vision model) → JSON result (critique checklist / component code) → back to the loop. Unchanged from today except the endpoint the call targets.

## Error handling

- `VISION_BASE` unset/empty → skill returns `{"error": "vision endpoint not configured (set VISION_BASE)"}` (no model call). Live test `require_service`-skips.
- `VISION_BASE` set but unreachable → litellm/connection error surfaces as the skill's normal error result; live test `require_service`-skips on the health probe.
- Image path missing/unreadable → existing per-skill error (unchanged).

## Testing

- **Unit (model-free):** `resolve_vision_endpoint()` — returns `(base, model)` when set, `None` when unset; the two skills' "disabled when unset" path returns the clean error without a model call (mock litellm; assert it is never called when unconfigured).
- **Live (`LIVE_TESTS=1`, gated):** flip the two skills' execution-smoke from "vision unavailable → skip" to `require_service(<VISION_BASE>/health reachable)`. Add a tiny committed PNG fixture (`tests/live/fixtures/ui_sample.png`). With both servers up: `critique(fixture)` returns a checklist; `generate(fixture)` returns component code. Without `VISION_BASE`: skip (today's behavior).
- **No GPU needed for the unit layer**; the live layer is validated on the dual-GPU host (like the rest of `tests/live/`).

## Docs / memory

- `CLAUDE.md`: document the dual-GPU vision endpoint (`VISION_BASE`/`VISION_MODEL`, `serve-vision.sh`, GPU pinning, opt-in) under the serving + live-test sections.
- Mark the `vision-endpoint-gap` memory resolved once the endpoint exists.

## Success criteria

- With both servers up on the dual-GPU host, `LIVE_TESTS=1` execution-smoke for `design-critique` + `screenshot-to-component` **passes** (real image → critique / component code).
- The text A/B harness and its context window are **byte-for-byte unaffected** (no change to `:8000`, `GEMMA_BASE`, or the orchestrator).
- Single-GPU / `VISION_BASE`-unset deploys still run text-only with the two skills cleanly skipping.

## Out of scope (YAGNI)

- Hosted/cloud VLM fallback (the design is endpoint-agnostic via `VISION_BASE`, so a cloud URL works without code change, but we don't build provider-specific wiring).
- Co-resident vision on GPU 0 / context-sharing (explicitly rejected — would shrink text context).
- Any new vision skills beyond the existing two.
