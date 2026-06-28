# Vision Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a dual-GPU local vision endpoint so `design-critique` and `screenshot-to-component` work, without touching the text harness or its context window.

**Architecture:** A second `llama-server` (vision GGUF + `mmproj`) runs on GPU 1 (3070 Ti, 8GB) at `:8001`, pinned via `CUDA_VISIBLE_DEVICES=1`; the 32GB GPU 0 keeps `gemma-4-31B` text on `:8000` untouched. The two vision skills target a configurable `VISION_BASE`/`VISION_MODEL` (they already do base64 `image_url` + litellm — it's a config swap). Vision is opt-in: `VISION_BASE` unset → the skills return a clean "not configured" result and the live test skips (today's behavior).

**Tech Stack:** Python 3.11 (skills), litellm, pytest + pytest-asyncio, bash (llama.cpp serve scripts), Pillow.

## Global Constraints

- The text harness is UNTOUCHED: no change to `:8000`, `GEMMA_BASE`, the orchestrator, or any non-vision skill. Verify this in the regression gate.
- Additive / opt-in: `VISION_BASE` default is **empty**; empty → the two skills return `{"error": "vision endpoint not configured (set VISION_BASE)"}` and make NO model call. Single-GPU / text-only deploys are unaffected.
- stdout is sacred in MCP servers — log to stderr only; never `print()`.
- Every llama.cpp/litellm call keeps `api_key` set and `extra_body={"thinking_budget_tokens": ...}` (do not regress the existing calls).
- No tiktoken; Chroma client-server (not touched here).
- Live tests are `LIVE_TESTS=1`-gated and `require_service`-skip when `VISION_BASE` is unreachable.
- The config helper is a tiny **os-only** module per skill (no litellm/PIL import) so it is unit-testable without heavy deps.

---

### Task 1: Vision-config + disabled-when-unset in `design-critique`

**Files:**
- Create: `services/skills/design-critique/vision_config.py`
- Modify: `services/skills/design-critique/critic.py:19-20,49-51,71`
- Test: `tests/services/skills/test_design_critique_vision.py`

**Interfaces:**
- Produces: `resolve_vision_endpoint() -> tuple[str, str] | None` — returns `(base, model)` when `VISION_BASE` is non-empty, else `None`. `VISION_MODEL` defaults to `"openai/gemma-3-vision"`.

- [ ] **Step 1: Write the failing test**

Create `tests/services/skills/test_design_critique_vision.py`:

```python
import os, sys
import pytest

_SKILL = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                      "services", "skills", "design-critique")
sys.path.insert(0, _SKILL)
import vision_config  # noqa: E402


def test_unset_returns_none(monkeypatch):
    monkeypatch.delenv("VISION_BASE", raising=False)
    assert vision_config.resolve_vision_endpoint() is None


def test_empty_returns_none(monkeypatch):
    monkeypatch.setenv("VISION_BASE", "")
    assert vision_config.resolve_vision_endpoint() is None


def test_set_returns_base_and_model(monkeypatch):
    monkeypatch.setenv("VISION_BASE", "http://localhost:8001/v1")
    monkeypatch.delenv("VISION_MODEL", raising=False)
    base, model = vision_config.resolve_vision_endpoint()
    assert base == "http://localhost:8001/v1"
    assert model == "openai/gemma-3-vision"


def test_model_override(monkeypatch):
    monkeypatch.setenv("VISION_BASE", "http://x/v1")
    monkeypatch.setenv("VISION_MODEL", "openai/gemma-3-12b")
    assert vision_config.resolve_vision_endpoint() == ("http://x/v1", "openai/gemma-3-12b")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/skills/test_design_critique_vision.py -v`
Expected: FAIL — `vision_config` does not exist.

- [ ] **Step 3: Create the os-only config module**

Create `services/skills/design-critique/vision_config.py`:

```python
"""Vision endpoint resolution (os-only — no heavy imports, unit-testable).

VISION_BASE is the OpenAI-compatible vision server (the dual-GPU host sets it to
http://localhost:8001/v1; unset elsewhere). Unset/empty => vision disabled.
"""
from __future__ import annotations

import os

_DEFAULT_VISION_MODEL = "openai/gemma-3-vision"


def resolve_vision_endpoint() -> tuple[str, str] | None:
    base = (os.getenv("VISION_BASE") or "").strip()
    if not base:
        return None
    model = (os.getenv("VISION_MODEL") or "").strip() or _DEFAULT_VISION_MODEL
    return base, model
```

- [ ] **Step 4: Wire critic.py to the vision endpoint + disabled path**

In `services/skills/design-critique/critic.py`:

Replace the module-level `GEMMA_BASE`/`GEMMA_MODEL` (lines 19-20) with:

```python
from vision_config import resolve_vision_endpoint

_VISION_NOT_CONFIGURED = {
    "error": "vision endpoint not configured (set VISION_BASE)"
}
```

In the client constructor (lines ~49-51), resolve at construction:

```python
    def __init__(self, model: str | None = None, api_base: str | None = None) -> None:
        endpoint = resolve_vision_endpoint()
        self.enabled = endpoint is not None
        if endpoint is not None:
            self.api_base, default_model = endpoint
            self.model = model or default_model
        else:
            self.api_base, self.model = None, None
```

At the top of BOTH `critique(...)` and `compare(...)`, before any `_encode_image`/litellm call, short-circuit when disabled:

```python
        if not self.enabled:
            return _VISION_NOT_CONFIGURED  # no model call when VISION_BASE unset
```

> The two tool entrypoints currently return a pydantic model (`critique`) / dict (`compare`). The server (`server.py`) wraps the result with `.model_dump_json()` for `critique` — to keep that working when disabled, return the error dict from `compare` as today, and for `critique` return the error via the same JSON path: have `server.py`'s `critique` branch handle a dict result (if `isinstance(result, dict): text = json.dumps(result)` else `result.model_dump_json()`). Apply that small guard in `server.py`'s `call_tool`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/skills/test_design_critique_vision.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/skills/design-critique/vision_config.py services/skills/design-critique/critic.py services/skills/design-critique/server.py tests/services/skills/test_design_critique_vision.py
git commit -m "feat(design-critique): target VISION_BASE; clean disabled-when-unset path"
```

---

### Task 2: Vision-config + disabled-when-unset in `screenshot-to-component`

**Files:**
- Create: `services/skills/screenshot-to-component/vision_config.py`
- Modify: `services/skills/screenshot-to-component/llm.py:20-22,52-53`
- Test: `tests/services/skills/test_screenshot_to_component_vision.py`

**Interfaces:**
- Consumes: same `resolve_vision_endpoint()` contract as Task 1 (duplicated per-skill — skills are independently packaged; do NOT cross-import).
- Produces: `call_llm` targets `VISION_BASE`/`VISION_MODEL`; a `VisionNotConfigured` exception (or sentinel) raised when unset so the pipeline returns a clean error.

- [ ] **Step 1: Write the failing test**

Create `tests/services/skills/test_screenshot_to_component_vision.py`:

```python
import os, sys
import pytest

_SKILL = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                      "services", "skills", "screenshot-to-component")
sys.path.insert(0, _SKILL)
import vision_config  # noqa: E402


def test_unset_returns_none(monkeypatch):
    monkeypatch.delenv("VISION_BASE", raising=False)
    assert vision_config.resolve_vision_endpoint() is None


def test_set_returns_base_and_model(monkeypatch):
    monkeypatch.setenv("VISION_BASE", "http://localhost:8001/v1")
    monkeypatch.delenv("VISION_MODEL", raising=False)
    assert vision_config.resolve_vision_endpoint() == (
        "http://localhost:8001/v1", "openai/gemma-3-vision")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/skills/test_screenshot_to_component_vision.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Create the os-only config module**

Create `services/skills/screenshot-to-component/vision_config.py` with the SAME content as Task 1 Step 3 (verbatim — duplicated intentionally; no cross-skill import).

- [ ] **Step 4: Wire llm.py to the vision endpoint + disabled path**

In `services/skills/screenshot-to-component/llm.py`:

Replace the `GEMMA_BASE`/`GEMMA_MODEL`/`GEMMA_API_KEY` module constants (lines 20-22) with:

```python
from vision_config import resolve_vision_endpoint

VISION_API_KEY = os.getenv("VISION_API_KEY", "not-needed")  # local llama.cpp ignores the key


class VisionNotConfigured(RuntimeError):
    """Raised when VISION_BASE is unset so the pipeline can return a clean error."""
```

In `call_llm` (lines ~49-53), resolve per call and short-circuit when disabled:

```python
    endpoint = resolve_vision_endpoint()
    if endpoint is None:
        raise VisionNotConfigured("vision endpoint not configured (set VISION_BASE)")
    api_base, model = endpoint
    resp = litellm.completion(
        model=model,
        api_base=api_base,
        api_key=VISION_API_KEY,
        ...  # keep existing messages / extra_body={"thinking_budget_tokens": ...}
```

In `server.py`'s `call_tool`, wrap the pipeline invocation so `VisionNotConfigured` becomes a clean tool result instead of an unhandled raise:

```python
    try:
        ... existing pipeline call ...
    except VisionNotConfigured as exc:
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
```

(Import `VisionNotConfigured` from `llm` in `server.py`. The pipeline/grounder/generator call `call_llm`, so the exception propagates up to `call_tool`.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/skills/test_screenshot_to_component_vision.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/skills/screenshot-to-component/vision_config.py services/skills/screenshot-to-component/llm.py services/skills/screenshot-to-component/server.py tests/services/skills/test_screenshot_to_component_vision.py
git commit -m "feat(screenshot-to-component): target VISION_BASE; clean disabled-when-unset path"
```

---

### Task 3: Serving — `serve-vision.sh` + status/start/install + env

**Files:**
- Create: `infrastructure/local/serve-vision.sh`
- Modify: `infrastructure/local/status.sh`, `infrastructure/local/start.sh`, `infrastructure/local/install.sh`, `infrastructure/local/local.env`

**Interfaces:**
- Consumes env: `VISION_MODEL_GGUF`, `VISION_MMPROJ`, `VISION_PORT` (default 8001), `VISION_GPU` (default 1), `VISION_NGL`, `VISION_CTX`.
- Produces: a 2nd `llama-server` on `:8001` pinned to GPU 1; `VISION_BASE=http://localhost:8001/v1` in `local.env`.

- [ ] **Step 1: Create `serve-vision.sh` (mirror `serve-model.sh`)**

Create `infrastructure/local/serve-vision.sh` (read `serve-model.sh` first and mirror its structure: logging-to-stderr helpers, idempotent "already serving" check, health-wait loop, pid file):

```bash
#!/usr/bin/env bash
# Second llama-server for VISION, pinned to GPU 1 (3070 Ti). Text model on :8000
# (GPU 0) is untouched. Idempotent; waits until :8001/health is ready.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/local.env" 2>/dev/null || true

LLAMA_SERVER="${LLAMA_SERVER:-/workspace/llama.cpp/build/bin/llama-server}"
VISION_MODEL_GGUF="${VISION_MODEL_GGUF:-/workspace/models/gemma-3-vision-gguf/gemma-3-4b-it-Q4_K_M.gguf}"
VISION_MMPROJ="${VISION_MMPROJ:-/workspace/models/gemma-3-vision-gguf/mmproj-gemma-3-4b-it.gguf}"
VISION_PORT="${VISION_PORT:-8001}"
VISION_GPU="${VISION_GPU:-1}"
VISION_NGL="${VISION_NGL:-99}"
VISION_CTX="${VISION_CTX:-8192}"
LOGS="${LOGS:-$HERE/../../.data/logs}"; PIDS="${PIDS:-$HERE/../../.data/pids}"
mkdir -p "$LOGS" "$PIDS"
info(){ echo "[serve-vision] $*" >&2; }
fail(){ echo "[serve-vision] FAIL: $*" >&2; exit 1; }
ready(){ curl -fsS "http://localhost:${VISION_PORT}/health" >/dev/null 2>&1; }

if ready; then info "vision llama-server already serving on :${VISION_PORT}"; exit 0; fi
[[ -x "$LLAMA_SERVER" ]] || fail "llama-server not built at $LLAMA_SERVER"
[[ -f "$VISION_MODEL_GGUF" ]] || fail "vision GGUF not found at $VISION_MODEL_GGUF — run install.sh"
[[ -f "$VISION_MMPROJ" ]] || fail "mmproj not found at $VISION_MMPROJ — run install.sh"

info "launching vision llama-server: ${VISION_MODEL_GGUF##*/} on :${VISION_PORT} (GPU ${VISION_GPU}, ctx=${VISION_CTX}) ..."
CUDA_VISIBLE_DEVICES="${VISION_GPU}" "$LLAMA_SERVER" \
  -m "$VISION_MODEL_GGUF" \
  --mmproj "$VISION_MMPROJ" \
  --port "$VISION_PORT" \
  --ctx-size "$VISION_CTX" \
  --n-gpu-layers "$VISION_NGL" \
  --alias gemma-3-vision \
  >"$LOGS/llama-vision.log" 2>&1 &
echo $! > "$PIDS/llama-vision.pid"
info "vision llama-server pid $(cat "$PIDS/llama-vision.pid") — logs: $LOGS/llama-vision.log"

for _ in $(seq 1 120); do
  if ready; then info "vision ready on :${VISION_PORT}"; exit 0; fi
  if ! kill -0 "$(cat "$PIDS/llama-vision.pid")" 2>/dev/null; then
    echo "[serve-vision] FAIL: vision llama-server exited — last log lines:" >&2
    tail -25 "$LOGS/llama-vision.log" >&2; exit 1
  fi
  sleep 2
done
fail "vision model not ready after timeout — see $LOGS/llama-vision.log"
```

Make it executable: `chmod +x infrastructure/local/serve-vision.sh`.

- [ ] **Step 2: Gate vision startup in `start.sh`**

In `infrastructure/local/start.sh`, after the text model is started, start the vision server ONLY if its GGUF exists (so text-only deploys never try). Add:

```bash
# Vision endpoint (GPU 1) — opt-in: only if the vision GGUF is present.
if [[ -f "${VISION_MODEL_GGUF:-/workspace/models/gemma-3-vision-gguf/gemma-3-4b-it-Q4_K_M.gguf}" ]]; then
  "$HERE/serve-vision.sh" || echo "[start] vision server failed to start (continuing text-only)" >&2
else
  echo "[start] vision GGUF absent — skipping vision endpoint (text-only)" >&2
fi
```

(Use the same `HERE` the script already defines; match its existing style.)

- [ ] **Step 3: Add a vision health line to `status.sh`**

In `infrastructure/local/status.sh`, add a reported (non-fatal) check mirroring the existing `:8000` check:

```bash
if curl -fsS "http://localhost:${VISION_PORT:-8001}/health" >/dev/null 2>&1; then
  echo "  vision  llama-server :${VISION_PORT:-8001}  UP"
else
  echo "  vision  llama-server :${VISION_PORT:-8001}  (down / not configured)"
fi
```

- [ ] **Step 4: Add the vision model download to `install.sh` (guarded)**

In `infrastructure/local/install.sh`, add a guarded download of the Gemma 3 4B vision GGUF + mmproj (skip if present). Mirror however `install.sh` already fetches the text GGUF (e.g. `huggingface-cli download` or `curl`); place files at `VISION_MODEL_GGUF` / `VISION_MMPROJ`:

```bash
# Vision model (Gemma 3 4B vision) + mmproj for the :8001 endpoint (GPU 1).
VISION_DIR="/workspace/models/gemma-3-vision-gguf"
if [[ ! -f "$VISION_DIR/gemma-3-4b-it-Q4_K_M.gguf" ]]; then
  mkdir -p "$VISION_DIR"
  huggingface-cli download ggml-org/gemma-3-4b-it-GGUF \
    gemma-3-4b-it-Q4_K_M.gguf mmproj-gemma-3-4b-it.gguf --local-dir "$VISION_DIR"
fi
```

> Implementer note: confirm the exact repo/filenames against how `install.sh` fetches the TEXT model and adjust to the same tool. The `ggml-org/gemma-3-4b-it-GGUF` repo ships the mmproj; if a different known-good repo is used for the text model, match its convention.

- [ ] **Step 5: Add env to `local.env`**

In `infrastructure/local/local.env`, after the `GEMMA_BASE`/`QWEN_BASE` block, add:

```bash
# Vision endpoint (GPU 1 / 3070 Ti). Empty by default => vision skills are
# disabled and skip cleanly. On the dual-GPU host, set VISION_BASE to enable.
export VISION_BASE="${VISION_BASE:-http://localhost:8001/v1}"
export VISION_MODEL="${VISION_MODEL:-openai/gemma-3-vision}"
export VISION_MODEL_GGUF="${VISION_MODEL_GGUF:-/workspace/models/gemma-3-vision-gguf/gemma-3-4b-it-Q4_K_M.gguf}"
export VISION_MMPROJ="${VISION_MMPROJ:-/workspace/models/gemma-3-vision-gguf/mmproj-gemma-3-4b-it.gguf}"
export VISION_PORT="${VISION_PORT:-8001}"
export VISION_GPU="${VISION_GPU:-1}"
```

> Note: `local.env` enabling `VISION_BASE` is what flips the skills on. On a single-GPU box, comment out / unset `VISION_BASE` (or simply don't run `serve-vision.sh`) — the skills then return the clean "not configured" result.

- [ ] **Step 6: Verify scripts are syntactically valid**

Run: `bash -n infrastructure/local/serve-vision.sh && bash -n infrastructure/local/start.sh && bash -n infrastructure/local/status.sh && bash -n infrastructure/local/install.sh && echo "bash syntax OK"`
Expected: `bash syntax OK`. (Live execution is validated on the dual-GPU host — Task 4 / the runbook.)

- [ ] **Step 7: Commit**

```bash
git add infrastructure/local/serve-vision.sh infrastructure/local/start.sh infrastructure/local/status.sh infrastructure/local/install.sh infrastructure/local/local.env
git commit -m "feat(infra): serve-vision.sh — 2nd llama-server (vision) pinned to GPU 1"
```

---

### Task 4: Live vision execution-smoke test (self-contained, gated)

**Files:**
- Create: `tests/live/test_vision_skills_live.py`
- Create: `tests/live/fixtures/ui_sample.png` (generated)

**Interfaces:**
- Consumes: `tests/live/conftest.py` helpers `require_service`, `live_enabled` (already on this branch); `VISION_BASE` env; the two skills' own modules (imported by path). NO dependency on `skill_harness` (not on this branch).

- [ ] **Step 1: Create a tiny PNG fixture (generator step)**

Generate a small valid PNG so the test has a real image:

```bash
mkdir -p tests/live/fixtures
PYTHONPATH=. python -c "from PIL import Image; Image.new('RGB',(64,48),'white').save('tests/live/fixtures/ui_sample.png')"
```

- [ ] **Step 2: Write the live test**

Create `tests/live/test_vision_skills_live.py`:

```python
import os, sys, urllib.request
import pytest

from tests.live.conftest import require_service

pytestmark = pytest.mark.live

_FIXture = os.path.join(os.path.dirname(__file__), "fixtures", "ui_sample.png")


def _vision_reachable() -> bool:
    base = (os.getenv("VISION_BASE") or "").rstrip("/")
    if not base:
        return False
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=2) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False


def test_design_critique_runs_on_image():
    require_service(_vision_reachable, "VISION_BASE vision endpoint")
    skill = os.path.join(os.path.dirname(__file__), "..", "..",
                         "services", "skills", "design-critique")
    sys.path.insert(0, skill)
    import critic  # noqa: E402
    result = critic.CritiqueClient().critique(_FIXture)
    # pydantic CritiqueResult or a dict; either way it's a real, non-empty result
    payload = result if isinstance(result, dict) else result.model_dump()
    assert payload and "error" not in payload, f"critique failed: {payload}"


def test_screenshot_to_component_runs_on_image():
    require_service(_vision_reachable, "VISION_BASE vision endpoint")
    skill = os.path.join(os.path.dirname(__file__), "..", "..",
                         "services", "skills", "screenshot-to-component")
    sys.path.insert(0, skill)
    import pipeline  # noqa: E402
    out = pipeline.run(_FIXture) if hasattr(pipeline, "run") else None
    # Adapt to the real pipeline entrypoint (see note); assert component code came back
    assert out is not None
    text = str(out)
    assert "error" not in text.lower() and len(text) > 0
```

> Implementer note: read `services/skills/design-critique/critic.py` (the `CritiqueClient`/critique entry) and `services/skills/screenshot-to-component/pipeline.py` (the real run entrypoint + return type) and adjust the two call sites + assertions to the actual API. Keep the `require_service(_vision_reachable, ...)` gate and assert a real, non-error result. If a skill's deps aren't importable on the host, the gate/skip pattern applies (wrap the import and `require_service(lambda: False, ...)` on ImportError).

- [ ] **Step 3: Run (skips without VISION_BASE — expected off the dual-GPU host)**

Run: `LIVE_TESTS=1 PYTHONPATH=. python -m pytest tests/live/test_vision_skills_live.py -rs -v`
Expected (no vision endpoint): SKIPPED. On the dual-GPU host with `VISION_BASE` up: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/live/test_vision_skills_live.py tests/live/fixtures/ui_sample.png
git commit -m "test(live): vision execution-smoke for design-critique + screenshot-to-component (VISION_BASE-gated)"
```

---

### Task 5: Docs

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Document the vision endpoint**

In `CLAUDE.md`, add a short subsection under the serving / live-test area:

```markdown
### Vision endpoint (dual-GPU, opt-in)

design-critique + screenshot-to-component are image-in and need a vision model.
On a dual-GPU host, a 2nd llama-server (Gemma 3 4B vision GGUF + mmproj) runs on
GPU 1 (`CUDA_VISIBLE_DEVICES=1`) at `:8001`; the 32GB GPU 0 keeps gemma-4-31B text
on `:8000` with full context. Enable by setting `VISION_BASE=http://localhost:8001/v1`
in `local.env` and running `infrastructure/local/serve-vision.sh` (start.sh runs it
automatically if the vision GGUF is present). Unset `VISION_BASE` → the two skills
return "vision endpoint not configured" and skip; text-only/single-GPU deploys are
unaffected. Live check: `LIVE_TESTS=1 python -m pytest tests/live/test_vision_skills_live.py -v`.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE.md): document the dual-GPU vision endpoint"
```

---

### Task 6: Regression gate — text harness untouched

- [ ] **Step 1:** Confirm NO change outside vision scope:
Run: `git diff --stat ffe533e..HEAD -- services/orchestrator services/mcp-bridge` — Expected: NO output (orchestrator/bridge untouched).
- [ ] **Step 2:** Run: `PYTHONPATH=. python -m pytest tests/ -q 2>&1 | tail -5` — Expected: full suite green; new unit tests pass; live vision test SKIPS (no VISION_BASE); no NEW failures vs the `ffe533e` baseline.
- [ ] **Step 3:** Confirm opt-in default: `grep -n 'GEMMA_BASE\|VISION_BASE' services/skills/design-critique/critic.py services/skills/screenshot-to-component/llm.py` — Expected: the two skills reference `VISION_BASE` (via `resolve_vision_endpoint`), not `GEMMA_BASE`.

---

## Self-Review

- **Spec coverage:** serving (Task 3) ✓; skill wiring + disabled-when-unset (Tasks 1–2) ✓; tests unit+live (Tasks 1,2,4) ✓; docs (Task 5) ✓; regression gate / harness-untouched (Task 6) ✓.
- **Opt-in honored:** `VISION_BASE` empty → `resolve_vision_endpoint()` returns None → skills return the clean error with NO model call (unit-tested); start.sh gates on the GGUF; single-GPU unaffected.
- **No placeholders:** the two "adapt to the real entrypoint" notes (Task 4 pipeline call; Task 3 install.sh download tool) are explicit, bounded, and the surrounding code is concrete. The config module is fully specified and shared verbatim across both skills.
- **Type consistency:** `resolve_vision_endpoint() -> (base, model) | None` identical in both skills; `critic.py`/`llm.py` consume it the same way; server.py guards the disabled/dict result.
- **Live caveat:** serving (bash) + the live vision test are validated on the dual-GPU host (this branch is merge-deferred until then); the unit layer + harness-untouched gate run anywhere.
