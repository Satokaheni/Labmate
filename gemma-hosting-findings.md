# Gemma 4 31B Hosting Findings

**Date:** 2026-06-17  
**Research method:** 7 parallel Opus web-search agents via `/research-deep`  
**Hardware context:** RunPod A6000 48 GB, driver 570.195 (CUDA 12.8), host-native (no containers)  
**Scale:** 1 developer + max 10 concurrent users, will eventually move to personal hardware

---

## Executive Summary

Two engines are **RECOMMENDED**: the current **llama.cpp** (with a three-line serve-command fix) and **vLLM** (which is installable on the current pod today via a cu128 wheel — the earlier finding that "all vLLM wheels need CUDA 12.9+" was incorrect).

The core reasoning-token-waste problem is **already fixable on llama.cpp** without switching engines. The correct per-request parameter is `thinking_budget_tokens`, not `reasoning_budget`. It requires one change: remove `--reasoning-budget` from the server launch command.

| Engine | Verdict | Per-request reasoning budget | Tool calling | CUDA 12.8 wheel | Notes |
|---|---|---|---|---|---|
| **llama.cpp** | ✅ RECOMMENDED | ✅ `thinking_budget_tokens` | ✅ via `--jinja` | ✅ | Fix the serve command; speculative decoding pending PR |
| **vLLM** | ✅ RECOMMENDED | ✅ `reasoning_budget` (PR #37112 merged) | ✅ dedicated `gemma4` parser | ✅ cu128 wheel exists | Best long-term; heavier install; concurrent tool-parser bug to verify |
| **SGLang** | ⚠️ VIABLE | ❌ on/off only, no token cap | ✅ dedicated `gemma4` parser | ⚠️ cu128 exists but requires version pinning | Speculative decode broken; HF safetensors only |
| **ExLlamaV3 + tabbyAPI** | ⚠️ VIABLE (caveats) | ❌ on/off only | ✅ `gemma4` tool_format | ✅ cu128 wheel exists | Critical VRAM bug (#191): ~85 GB vs expected ~45 GB for 31B; wait for fix |
| **Ollama** | ❌ NOT_VIABLE | ❌ none | ❌ broken | ✅ | /v1 returns empty content; tool calls misparsed/dropped; worse than raw llama.cpp |
| **MLC-LLM** | ❌ NOT_VIABLE | ❌ none | ❌ no parser | ✅ wheel | Gemma 4 completely unsupported (issue #3477, open/unaddressed) |
| **RunPod driver upgrade** | ⚠️ VIABLE (situational) | n/a | n/a | n/a | Does NOT fix containers; vLLM cu128 wheels make it unnecessary |

---

## Part 1 — Immediate Fix: llama.cpp (No Engine Switch Required)

The token-waste problem comes from two bugs in the current serve command, both fixable today.

### Bug 1: `enable_thinking: false` is silently ignored for Gemma 4

`chat_template_kwargs: {"enable_thinking": false}` per request does not work on Gemma 4 in llama-server. The log still shows `thinking=1` and reasoning tokens are still generated. This was confirmed fixed in build b8738 via a different mechanism.

**Fix:** Use the global `--reasoning off` server flag to disable thinking entirely, OR use `thinking_budget_tokens: 0` per request (see Bug 2).

### Bug 2: `reasoning_budget` per-request is ignored; use `thinking_budget_tokens`

The server-side code gate (in `server-common.cpp`, commit `0fcb3760`) is:
```cpp
if (reasoning_budget == -1 && body.contains("thinking_budget_tokens"))
```

If `--reasoning-budget N` is set as a server flag, `reasoning_budget != -1` and the per-request field is **never read**. This is why per-request budget control appeared broken.

**Fix:** Remove `--reasoning-budget` from the serve command. Leave it at the default (`-1`). Then send `thinking_budget_tokens` per request:
- Planner node: `{"thinking_budget_tokens": 2048}` (or whatever thinking allowance)
- Tool-dispatch node: `{"thinking_budget_tokens": 0}`

Note: `thinking_budget_tokens: 0` may not fully suppress thinking on Gemma 4 the same way `--reasoning off` does. If you need a hard-off, use `--reasoning off` as a server flag (which disables thinking globally). If you need per-request control, drop `--reasoning-budget` and use `thinking_budget_tokens` per request as above.

### Updated serve command

```bash
# CURRENT (broken - reasoning budget shared globally, enable_thinking ignored)
llama-server \
  -m models/gemma-4-31B-it-UD-Q4_K_XL.gguf \
  --jinja \
  --n-gpu-layers 999 \
  --ctx-size 16384 \
  --parallel 2 \
  --host 127.0.0.1 --port 8000

# FIXED
llama-server \
  -m models/gemma-4-31B-it-UD-Q4_K_XL.gguf \
  --jinja \
  --n-gpu-layers 999 \
  --ctx-size 16384 \
  --parallel 2 \
  --host 127.0.0.1 --port 8000 \
  -fa on \
  --reasoning-format deepseek \
  --reasoning-budget-message "\n</think>\n"
  # DO NOT add --reasoning-budget N (that disables per-request thinking_budget_tokens)
  # DO NOT add --reasoning off (that disables thinking globally; useful only if you never want CoT)
```

**`-fa on`** enables flash attention: ~40% KV VRAM reduction, meaningful speed improvement, comparable quality on typical tasks.

**`--reasoning-format deepseek`** puts reasoning in `message.reasoning_content` (separate from `content`). Without this, reasoning leaks into `content`.

**`--reasoning-budget-message`** avoids an abrupt mid-sentence cutoff when `thinking_budget_tokens` is reached.

### Per-request usage in the orchestrator

```python
# Planner node - full thinking budget
response = await client.chat.completions.create(
    model="gemma-4-31B",
    messages=[...],
    extra_body={"thinking_budget_tokens": 2048}
)

# Tool-dispatch node - no thinking
response = await client.chat.completions.create(
    model="gemma-4-31B",
    messages=[...],
    extra_body={"thinking_budget_tokens": 0}
)
```

### Other llama.cpp findings to act on

| Finding | Action |
|---|---|
| Post-April-2026 builds default reasoning sampler to `INT_MAX` — can cause non-deterministic hangs | Set `thinking_budget_tokens` on every request, OR use `--reasoning-budget-message` to force exit |
| Single user: `-np 1` saves the ~3.6 GB SWA KV slot that would otherwise be reserved per-slot | Add `-np 1` to serve command if only one session at a time |
| Speculative decoding (MTP draft): NOT supported on upstream master | Track PR #23398; use `reffdev/llama.cpp` fork or `AtomicChat/gemma-4-assistant-gguf` for early access; expect ~1.7–2× speedup when merged |
| Use build ≥ b8738 | The `--reasoning off` fix landed in b8738; the `thinking_budget_tokens` sampler in ~b8850 |

---

## Part 2 — Migration Option: vLLM (RECOMMENDED if you want the best tool-calling parser)

### Correction to the previous finding

`model.md` stated "ALL pre-built vLLM wheels require CUDA 12.9+." **This is wrong.** vLLM publishes cu128 wheels and the current pod can install them today:

```bash
# Option A: uv (recommended — auto-selects cu128)
pip install uv
UV_TORCH_BACKEND=cu128 uv pip install 'vllm>=0.19.0'

# Option B: plain pip
pip install 'vllm>=0.19.0' --extra-index-url https://download.pytorch.org/whl/cu128
pip install 'transformers>=5.5.0'  # required for Gemma 4
```

The default PyPI wheel moved to cu130 in v0.20.0, so you **must** pass the cu128 backend explicitly or you'll get a driver mismatch. No source build required.

### Why vLLM is compelling for Labmate

1. **Per-request `reasoning_budget`** (PR #37112, merged): caps thinking tokens without disabling them. Pass `reasoning_budget: N` alongside `enable_thinking: true` per request. This is the exact feature llama.cpp's `thinking_budget_tokens` provides, but through vLLM's more hardened inference engine.

2. **Dedicated `gemma4` tool parser** AND **`gemma4` reasoning parser**: no relying on Jinja template alone.

3. **PagedAttention + continuous batching**: better for 4-8 parallel sub-agents than llama.cpp's slot batching.

### vLLM serve command

```bash
# Uses Google's official QAT W4A16 4-bit checkpoint (best quality at 4-bit, fits A6000 48GB)
vllm serve google/gemma-4-31B-it-qat-w4a16-ct \
  --host 0.0.0.0 \
  --port 8000 \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.85 \
  --enable-auto-tool-choice \
  --tool-call-parser gemma4 \
  --reasoning-parser gemma4 \
  --chat-template examples/tool_chat_template_gemma4.jinja
```

No `--quantization` flag needed — vLLM reads the quant config from the checkpoint automatically.

### Per-request usage in the orchestrator

```python
# Planner - thinking on with budget cap
response = await client.chat.completions.create(
    model="gemma-4-31B-it-qat-w4a16-ct",
    messages=[...],
    reasoning_effort="medium",           # or extra_body={"chat_template_kwargs": {"enable_thinking": True}}
    extra_body={"reasoning_budget": 1024}
)

# Tool-dispatch - thinking off
response = await client.chat.completions.create(
    model="gemma-4-31B-it-qat-w4a16-ct",
    messages=[...],
    reasoning_effort="none"              # maps to enable_thinking=false
)
```

### vLLM bugs to verify before relying on it

| Bug | Issue | Impact | Status |
|---|---|---|---|
| Concurrent tool calls produce `<pad>` tokens (non-thread-safe parser state) | #39392 | **HIGH** — directly affects parallel sub-agents | Check if fixed in your patch version |
| `--reasoning-parser gemma4` silently disables xgrammar (structured output) when thinking is off | #39130 | Medium — affects guided-decoding calls | Mitigation: `skip_special_tokens: false` |
| Reasoning leaks into `content` if `skip_special_tokens=True` strips thinking tokens before parser | #38855 | Medium | Check patch version |
| MTP speculative decoding drops first tool-call args in streaming multi-tool | #41967 | Medium | Do NOT combine MTP + tool calling |

**Recommendation:** Pin a late v0.19.x patch version, test concurrent tool calling before relying on it in the orchestrator fan-out.

### VRAM: both 4-bit options fit comfortably

| Checkpoint | Format | VRAM (weights) | +16k KV | Total | Headroom |
|---|---|---|---|---|---|
| `google/gemma-4-31B-it-qat-w4a16-ct` | QAT W4A16 | ~18–20 GB | ~5–8 GB | ~26 GB | ~22 GB free |
| `nvidia/Gemma-4-31B-IT-NVFP4` | FP4 | ~18–20 GB | ~5–8 GB | ~26 GB | ~22 GB free |
| Embedder (bge-small-en-v1.5) + Reranker (bge-reranker-v2-m3) | — | 2.4 GB total | — | — | Still ~20 GB free |

---

## Part 3 — Engines Ruled Out

### Ollama — NOT_VIABLE

Ollama is just llama.cpp with a wrapper, but the wrapper **breaks things** for Gemma 4:

- `/v1/chat/completions` returns **empty `content`** — all text is in a non-standard `reasoning` field. Breaks `openai-python` and any standard client.
- The `think: false` per-request parameter is **silently ignored** on the `/v1` endpoint. Only works on the native `/api/chat` endpoint.
- Tool calling is actively broken: parser crashes (#15241), streaming `tool_calls` dropped and misrouted into the reasoning field (#20995), `system prompt + think:false + tools` combination fails entirely (#15539, unresolved).
- No per-request reasoning **token budget** at all — only on/off.

**Verdict:** You are strictly better off running `llama-server` directly. Ollama adds a buggy OpenAI shim and removes the `thinking_budget_tokens` control.

### ExLlamaV3 + tabbyAPI — VIABLE but wait

Important correction: Gemma 4 requires **ExLlamaV3** (v0.0.43), not ExLlamaV2. They are different engines with different quantization formats (EXL3, not EXL2). Official EXL3 quants exist at `turboderp/gemma-4-31b-it-exl3`.

The decisive blocker: **open VRAM bug** (ExLlamaV3 issue #191) where the 31B dense model uses ~85 GB VRAM instead of the expected ~45 GB, making it effectively unusable on a 48 GB card at useful context lengths. Wait for this fix before considering ExLlamaV3.

The `gemma4` tool_format in tabbyAPI is genuinely good — first-class tool parsing aligned with the OpenAI standard. Worth revisiting once the VRAM bug is fixed.

### MLC-LLM — NOT_VIABLE

Gemma 4 is simply not supported (issue #3477, filed 2026-04-05, open, no maintainer response, zero commits through 2026-05-11). Even if it were: each new model requires a TVM kernel compilation pass targeting sm_86 (tens of minutes to hours), making it operationally impractical for a small deployment.

### SGLang — VIABLE, not RECOMMENDED

SGLang has good Gemma 4 support (PR #21952) and a dedicated `gemma4` reasoning + tool-call parser. The cu128 install is doable but requires version pinning to avoid newer releases pulling cuda-python 13.x.

The gap that holds it below RECOMMENDED: **no per-request reasoning token budget** (only on/off toggle). It shares the same limitation llama.cpp had before the `thinking_budget_tokens` fix. Speculative decoding is also currently broken for Gemma 4 (FROZEN_KV_MTP crash, issue #24912).

Use SGLang if RadixAttention prefix caching for concurrent sub-agents becomes a bottleneck and vLLM's concurrent tool-parser bug (#39392) is not yet fixed.

---

## Part 4 — RunPod Infrastructure

### Container limitation is NOT driver-related

The NET_ADMIN / seccomp / no-Docker limitation is **RunPod-wide and architectural** — standard Pods (Secure Cloud and Community Cloud) are themselves containers, so nested containers are blocked by the pod runtime, regardless of driver version. A driver upgrade will NOT fix this.

To get containers/privileged mode, you would need **RunPod Bare Metal** (full physical server, no container layer, root access) or a community provider's explicitly privileged offer.

### Driver upgrade is probably unnecessary

vLLM cu128 wheels work on the current pod (`driver 570.195`). A driver upgrade is only needed if you find a specific engine/wheel that has no cu128 build and genuinely requires CUDA 13. That has not been found.

If you do need a newer driver: in the RunPod web console go to **Deploy → Filters → Allowed CUDA Versions** and set 12.9 or 13.0. Same A6000 pricing (~$0.33–$0.49/hr). Your network volume can be re-attached within the same datacenter. The `runpodctl` CLI cannot filter by driver yet (issue #253).

---

## Part 5 — Recommended Action Plan

### Option A: Stay on llama.cpp (zero migration cost)

1. Pin build ≥ b8738 (check current: `llama-server --version`; download from GitHub releases if older)
2. Update `infrastructure/local/serve-model.sh` with the fixed serve command from Part 1
3. Update orchestrator to send `thinking_budget_tokens` per-request (no server flag)
4. Watch PR #23398 for MTP speculative decoding; update serve command when merged (~1.7–2× speedup)

**Estimated effort:** 30 minutes. All fixes are serve-command and client-call changes.

### Option B: Migrate to vLLM (best long-term engine)

1. `UV_TORCH_BACKEND=cu128 uv pip install 'vllm>=0.19.0' && pip install 'transformers>=5.5.0'`
2. Download `google/gemma-4-31B-it-qat-w4a16-ct` (official QAT W4A16 checkpoint)
3. Use the serve command from Part 2
4. Update orchestrator to use `reasoning_effort` / `reasoning_budget` fields
5. Run concurrent tool-call smoke test to verify bug #39392 is patched in your version
6. Update `infrastructure/local/serve-model.sh`

**Estimated effort:** 2–4 hours including model download. Provides per-request reasoning budget, dedicated parsers, and better concurrency.

### What the orchestrator code needs to change

Regardless of engine, the orchestrator (`services/orchestrator/`) needs to:

1. Remove any global `enable_thinking: false` from a shared client config
2. Pass `thinking_budget_tokens` (llama.cpp) or `reasoning_budget` (vLLM) on a **per-node basis**
3. Check that `message.reasoning_content` is separate from `message.content` (requires `--reasoning-format deepseek` on llama.cpp, automatic on vLLM with `--reasoning-parser gemma4`)
4. Never send `enable_thinking: false` via `chat_template_kwargs` — it is silently ignored on both llama.cpp and Ollama for Gemma 4

---

## Part 6 — File Changes Needed

| File | Change |
|---|---|
| `infrastructure/local/serve-model.sh` | Fix serve command: add `-fa on`, `--reasoning-format deepseek`, `--reasoning-budget-message`; remove `--reasoning-budget` if present |
| `CLAUDE.md` | Update vLLM serve command note; add llama.cpp `thinking_budget_tokens` per-request pattern |
| `model.md` | Update to reflect that vLLM cu128 wheels exist; `thinking_budget_tokens` fixes the budget problem; Ollama is not viable for agent use |
| `services/orchestrator/` (future) | Pass `thinking_budget_tokens` or `reasoning_budget` per node type; read `reasoning_content` from response |
| `spec_inference.md` | Update preferred engine section: vLLM is viable on CUDA 12.8 via cu128 wheel; note llama.cpp `thinking_budget_tokens` fix |

---

## Quick Reference: Per-Request Reasoning Control

| Engine | Toggle off | Budget cap | How |
|---|---|---|---|
| llama.cpp ≥ b8738 | `--reasoning off` (global) | `thinking_budget_tokens: N` in body | Do NOT set `--reasoning-budget` as server flag |
| vLLM ≥ 0.19.0 | `reasoning_effort: "none"` or `enable_thinking: false` | `reasoning_budget: N` in body | Requires `--reasoning-parser gemma4` |
| SGLang | `enable_thinking: false` in `chat_template_kwargs` | ❌ not supported | Requires `--reasoning-parser gemma4` |
| ExLlamaV3/tabbyAPI | `chat_template_kwargs: {enable_thinking: false}` | ❌ not supported | Requires `tool_format: gemma4` in config |
| Ollama | `think: false` on `/api/chat` only | ❌ not supported | ❌ Broken on `/v1`; do not use |
