# Model Hosting — Research Brief

**Purpose:** A self-contained brief for a deep-research pass on the *best way to host
the primary brain model* (Gemma 4 31B) for the **Labmate** agent harness. Written so a
research model with no prior context can pick it up and produce concrete, actionable
recommendations.

**Date of investigation:** 2026-06-17
**Author:** Claude Code (during infra bring-up)

---

## 1. What Labmate needs from the model server

Labmate is a local autonomous coding/writing agent (Brain → MCP bridge → skills). The
orchestrator (Python, LangGraph) talks to the model over an **OpenAI-compatible HTTP API**
(`/v1/chat/completions`, SSE streaming). Requirements:

- OpenAI-compatible `/v1/chat/completions` + `/v1/models`, streaming (SSE).
- **Tool / function calling** (the agent dispatches MCP tools). Gemma 4 emits native
  `<|tool_call|>` JSON; vLLM has a `gemma4` tool parser, llama.cpp does it via `--jinja`.
- **Reasoning-model handling:** Gemma 4 is a thinking model — output splits into
  `reasoning_content` (chain-of-thought) and `content` (final answer). Must be able to
  toggle thinking per request (planner = thinking on; editor/tool-decision = off).
- Concurrency: ideally continuous batching for parallel sub-agent fan-out (the
  orchestrator spawns parallel workers), but single-stream is acceptable for now.
- Single GPU. Possibly co-resident small embedding + reranker models (see §6).

The architecture/specs live in this repo under `research/llm-harness-research/specs/`
(`spec_inference.md`, `spec_orchestrator.md`) and `CLAUDE.md`.

---

## 2. Hardware & environment (hard constraints)

| Thing | Value | Notes |
|---|---|---|
| GPU | 1× NVIDIA RTX A6000, 48 GB GDDR6 | Ampere, **compute capability sm_86** |
| Host | RunPod pod | `/workspace` = huge persistent network FS (237 TB free) |
| NVIDIA driver | **570.195.03 → CUDA 12.8 max** | Host-level; cannot be upgraded from inside the pod |
| Containers | **Impossible in this pod** | No `NET_ADMIN`; seccomp blocks `unshare`/`clone` for all namespace types. Docker AND Podman both fail at network/namespace creation. So everything runs as native host processes. |
| CUDA toolkit | `nvcc` release 12.8 present | + cmake — can build from source |
| Python | 3.12, PEP-668 externally-managed | pip needs `--break-system-packages` |

**The driver ceiling (CUDA 12.8) is the crux of everything below.**

---

## 3. The core problem: vLLM cannot run here

vLLM is the spec's preferred engine (continuous batching, PagedAttention, native
`gemma4` tool parser). But:

- The model is **Gemma 4** (`google/gemma-4-31B-it`; we use the pre-quantized
  `unsloth/gemma-4-31B-it-unsloth-bnb-4bit`). The `gemma4` tool **and** reasoning parsers
  exist only in **recent vLLM (0.21.0 → 0.23.0)**.
- Every one of those vLLM versions **pins `torch==2.11.0`** and ships wheels compiled for
  **CUDA 12.9 / 13.0 only** (PyPI default for 0.23.0 = cu130; GitHub assets = cu129 +
  cu130; no cu128). vLLM's own compiled extension `vllm._C` then requires
  **`libcudart.so.13`**.
- Result on this driver:
  ```
  ImportError: libcudart.so.13: cannot open shared object file
  RuntimeError: The NVIDIA driver on your system is too old (found version 12080)
  ```
- Pinning `torch==2.11.0+cu128` makes **torch** work (`torch.cuda.is_available() == True`
  on the A6000), but does NOT fix vLLM's cu13-compiled `_C` extension.
- CUDA driver→runtime support matrix: cu128 needs driver ≥ 570.26 (we have 570.195 ✓);
  cu129 needs ≥ 575.51 (✗); cu130 needs ≥ 580 (✗).

**Net:** there is no off-the-shelf vLLM wheel that is both (a) cu128-compatible and
(b) has the gemma4 parser. So vLLM is currently a dead end on this pod.

---

## 4. Current working solution (baseline to beat)

**llama.cpp** (the spec's documented single-box fallback, `spec_inference.md` §2.2):

- Built from source with CUDA 12.8: `cmake -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86`,
  target `llama-server`. Works cleanly against the 12.8 driver.
- Model: **`unsloth/gemma-4-31B-it-GGUF`**, quant **`UD-Q4_K_XL`** (18.8 GB, Unsloth
  *Dynamic* — higher accuracy than plain Q4_0/Q4_K). The repo also ships:
  - `mtp-gemma-4-31B-it.gguf` (0.5 GB) — **MTP draft model for speculative decoding**.
  - `mmproj-F16.gguf` (1.2 GB) — vision projector (Gemma 4 is multimodal; we serve text).
  - quants from `UD-IQ2_XXS` (8.5 GB) up to `Q8_0`/`UD-Q8_K_XL` (~33–35 GB).
- Serve: `llama-server -m <gguf> --jinja --n-gpu-layers 999 --ctx-size 16384
  --parallel 2 --host 127.0.0.1 --port 8000`. OpenAI API at `/v1`, health at `/health`.
- **Measured:** loads fully into VRAM (~19 GB used of 48 GB), **~31 tok/s** single-stream
  generation, reasoning/answer split parses correctly via `--jinja`.
- **Reasoning control (verified):** per-request `"chat_template_kwargs":
  {"enable_thinking": false}` disables thinking and returns a direct answer with empty
  `reasoning_content` — even at tiny `max_tokens`. (Per-request `reasoning_budget` is
  IGNORED by this build; only server flags `--reasoning on|off|auto` and
  `--reasoning-budget N` work globally.)

The reproducible setup is scripted in `infrastructure/local/` (`install.sh`,
`serve-model.sh`, `INSTALL.md`).

**Known limitations of this baseline:**
- llama.cpp's batching/throughput is weaker than vLLM under concurrent load — a concern
  because the orchestrator fans out parallel sub-agents.
- Speculative decoding (MTP draft) is downloaded but **not yet wired** into the serve
  command.
- Single model in VRAM; the spec also wants a Qwen2.5-Coder-32B "editor" (Single-Brain
  swap policy — never co-resident).

---

## 5. Research questions (the actual ask)

Produce concrete, sourced recommendations on:

1. **Can vLLM be made to work on a CUDA 12.8 driver with Gemma 4?**
   - Is building vLLM **from source against cu128** (its own `_C` + FlashInfer/etc.)
     feasible, and how much effort / how brittle? Which vLLM commit first added the
     `gemma4` tool+reasoning parsers, and does it build on torch 2.8/cu128?
   - Is there a community cu128 wheel, or a `--torch-backend`/extra-index path that yields
     a cu128 vLLM `_C`?
   - Cost/benefit vs. just staying on llama.cpp.

2. **Should we instead change the environment?** e.g. request a RunPod image/host with a
   **≥ 580 driver (CUDA 13)** so stock vLLM works. Trade-offs (the no-container limitation
   would remain; is that independent of the driver?).

3. **Optimize the llama.cpp baseline:**
   - Wire **MTP speculative decoding** (`mtp-gemma-4-31B-it.gguf` draft) — expected speedup
     on this 31B dense-ish model on A6000? Correct `llama-server` flags
     (`--model-draft` / `--draft-max` / etc.) and quality impact.
   - Flash-attention (`-fa`), `--parallel`/continuous batching behavior, KV cache dtype,
     optimal `--ctx-size` vs. KV budget at 48 GB.
   - Best quant for the quality/speed/VRAM trade at 48 GB (UD-Q4_K_XL vs UD-Q5_K_XL vs
     Q6_K), incl. tool-call & structured-output accuracy.
   - Realistic concurrent throughput vs. vLLM for agent fan-out.

4. **Tool-calling reliability:** how robust is Gemma 4 tool calling through llama.cpp
   `--jinja` vs. vLLM's dedicated `gemma4` parser? Any known failure modes
   (delimiter/JSON parsing, streaming partial tool calls)?

5. **Two-model plan (architect + editor):** best way to host Gemma 4 (architect) +
   Qwen2.5-Coder-32B (editor) on ONE A6000 under the Single-Brain (no co-residency)
   policy — process-swap latency, VRAM reclamation, or is a smaller co-resident editor
   viable?

6. **Alternative engines** worth benchmarking on cu128/sm_86: ExLlamaV2 (EXL2),
   TensorRT-LLM (CUDA 12.x?), SGLang, MLC-LLM. Which support Gemma 4 + tool calling +
   OpenAI API today on this driver?

---

## 6. Memory budget context (for co-residency questions)

48 GB total. Current usage: ~19 GB (Gemma 4 UD-Q4_K_XL weights + KV at 16k ctx). The
memory layer also wants GPU embedding + reranker models co-resident with the LLM:
- `BAAI/bge-small-en-v1.5` (embedder, ~0.13 GB)
- `BAAI/bge-reranker-v2-m3` (reranker, ~2.3 GB)

So ~21–24 GB used, ~24 GB free — room for a bigger quant, longer context, speculative
draft heads, or KV for batching. Quantify the best use of that headroom.

---

## 7. Success criteria for the recommendation

- Highest sustained tokens/sec for the agent loop (incl. realistic parallel fan-out) on
  **this exact hardware/driver**, OR a clear, low-risk path to unlock vLLM-class batching.
- Reliable Gemma 4 tool calling + per-request reasoning toggle preserved.
- Reproducible as host-native processes (no containers) via scripts in
  `infrastructure/local/`.
- Concrete: exact engine + version + build flags + model/quant + serve command, with
  sources and any benchmark numbers.

---

## 8. Key facts / pins (so the researcher doesn't re-derive them)

- Driver: 570.195.03 (CUDA 12.8). GPU: A6000 48 GB, sm_86.
- torch that works here: `2.11.0+cu128` (or any cu126/cu128 build).
- vLLM with gemma4 parser: 0.21.0–0.23.0, all pin torch 2.11.0, ship cu129/cu130 only.
- Working model: `unsloth/gemma-4-31B-it-GGUF` (GGUF, llama.cpp) — quant `UD-Q4_K_XL`.
- Also available: `unsloth/gemma-4-31B-it-unsloth-bnb-4bit` (safetensors bnb-4bit, for
  vLLM/transformers paths) and `google/gemma-4-31B-it` (full bf16, 62.5 GB, ungated).
- llama.cpp serve currently: `--jinja --n-gpu-layers 999 --ctx-size 16384 --parallel 2`.
- Reasoning off per request: `chat_template_kwargs: {"enable_thinking": false}` (verified).
