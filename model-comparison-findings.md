# Gemma 4 31B vs Qwen3-32B — Model Comparison Findings

**For:** Labmate orchestrator brain selection  
**Hardware target:** NVIDIA A6000/A6000 Ada 48 GB (RunPod, current) → Mac Mini 48 GB or 32 GB discrete GPU (permanent)  
**Serving stack:** llama.cpp across all machines (CUDA on RunPod, Metal on Mac Mini, Vulkan on AMD)  
**Use case:** Academic paper reading/writing, paper critique, research ideation, coding ML experiments  
**Researched:** 2026-06-17 — 10 dimensions, 10 Opus web-search agents

---

## TL;DR Verdict

**Use a hybrid model-switching strategy.** Neither model dominates across all ten dimensions.

| Role | Model | Why |
|------|-------|-----|
| **Primary orchestrator brain** | **Gemma 4 31B** | Multimodal (reads paper figures), GPQA 84% vs 68%, 256K native context, LiveCodeBench 80% vs ~65-70% |
| **Text drafting + tool dispatch** | **Qwen3-32B** | WritingBench 8.64/10, BFCL tool-call 75.7% (#2 globally), no active parser bugs, cleaner thinking toggle |
| **Hardware** | **AMD Radeon AI PRO R9700** | 32 GB RDNA4, ~$1,250, Vulkan sidesteps Gemma 4 ROCm bug |

> If forced to pick **one model only**: start with Qwen3-32B for a working, bug-free deployment now. Revisit Gemma 4 as orchestrator once vLLM `gemma4` tool-call parser bugs (#39392, #39468, #39043) are closed.

---

## Dimension Scorecard

| # | Dimension | Winner | Confidence |
|---|-----------|--------|------------|
| 1 | Academic writing & paper critique | QWEN3 | Medium |
| 2 | Research reasoning & ideation | GEMMA4 | High |
| 3 | Multimodal vision for paper reading | GEMMA4 | **Decisive** |
| 4 | Long context performance | GEMMA4 | Medium |
| 5 | Coding & ML experiment support | GEMMA4 | Medium |
| 6 | Non-CUDA inference on 32 GB GPU | TIE | High |
| 7 | VRAM fit & quantization | QWEN3 | High |
| 8 | Agentic tool calling & MCP | QWEN3 | **Decisive** |
| 9 | Thinking mode & reasoning budget control | QWEN3 | High |
| 10 | Factual accuracy & over-refusal | UNCERTAIN | Low |

**Score: Gemma 4 wins 3–4 dimensions, Qwen3 wins 3–4, 2–3 tie/uncertain.**

---

## Dimension 1 — Academic Writing & Paper Critique Quality

**Winner: QWEN3** (for pure-text prose; Gemma 4 for figure-heavy critique)

- Qwen3-32B is the **only model with a published academic-writing benchmark**: WritingBench 8.64/10 (includes Academic & Engineering domain), matching DeepSeek-R1 (8.55)
- Independent MIMIC-IV study: Qwen3-32B leads **abstractive** summarization (fluency, paraphrastic prose); Gemma-3-27B leads **extractive** (lower confabulation near source) — Qwen drafts better, Gemma quotes more faithfully
- Gemma 4 has **no published WritingBench/EssayBench score** as of June 2026
- Critical caveat: Gemma 4 can **read paper figures, tables, OCR** natively; Qwen3-32B is **text-only** — for critiquing figure-heavy drafts, only Gemma 4 can inspect the actual content

**For Labmate:** Use Qwen3-32B for pure-text drafting and critique passes. Route any task requiring figure inspection to Gemma 4.

---

## Dimension 2 — Research Reasoning & Ideation

**Winner: GEMMA4** (decisive on science benchmarks)

| Benchmark | Gemma 4 31B | Qwen3-32B |
|-----------|-------------|-----------|
| GPQA Diamond (expert science) | **~84.3%** | 68.4% |
| Humanity's Last Exam | **~22.7–26.5%** | Not published (likely <10%) |
| AIME 2026 | **89.2%** | 72.9% (AIME-25) |
| LiveCodeBench v6 | **80.0%** | 60.6% |
| MMLU-Pro | Not published | 79.1% (thinking) |

- Gemma 4's ~16-point GPQA Diamond lead is a **meaningful gap on expert-level scientific reasoning** — directly relevant to "given these 3 papers, what experiment should we run next?"
- Qwen3-32B's MMLU-Redux 90.9 is strong on broad knowledge
- Both models reduce hallucination substantially with thinking mode ON

**For Labmate:** Gemma 4 is the better research ideation brain. Enable thinking mode for hypothesis generation and synthesis nodes.

---

## Dimension 3 — Multimodal Vision for Paper Reading

**Winner: GEMMA4** (decisive — Qwen3-32B cannot see images at all)

- **Gemma 4 31B:** Native multimodal across all sizes — images, video (as frames), OCR, PDF/document parsing, variable-aspect-ratio figures, chart comprehension, multilingual text in images. MMMU-Pro 76.9%, MATH-Vision 85.6%, OmniDocBench edit distance 0.131
- **Qwen3-32B:** **TEXT-ONLY.** No vision encoder. The base dense 32B cannot accept images. Qwen's vision capability lives in the separate `Qwen3-VL` model line (a different model)
- A full paper PDF rendered as images — including figures, plots, equation images, tables — **can only be processed by Gemma 4** of the two

**For Labmate:** This is potentially the deciding factor for a paper-reading research assistant. Gemma 4 is the only option for ingesting figure-heavy papers without a separate preprocessing pipeline.

---

## Dimension 4 — Long Context Performance & Degradation

**Winner: GEMMA4** (for Labmate's multi-paper workload)

| | Gemma 4 31B | Qwen3-32B |
|--|-------------|-----------|
| Nominal context | **256K** | 128K (native: 32K, YaRN to 128K) |
| RULER @ 128K | 66.4% | **85.6%** (non-thinking) |
| Fits a full paper (8–20K) | Yes | Yes |
| Fits 3+ papers simultaneously | Yes (native) | Needs YaRN |
| Architecture | Interleaved SWA + global attention | Dense, standard RoPE + YaRN |

- Qwen3-32B has **better measured RULER at 128K** (85.6 vs 66.4) — stronger pure-text retrieval
- Gemma 4 has a **much larger native window** (256K) with no YaRN degradation risk for short contexts
- Gemma 4's sliding-window + global attention architecture is designed to avoid "lost-in-the-middle" degradation
- For **multi-paper synthesis sessions**, Gemma 4's native 256K is the safer choice

**KV cache budget at 32 GB (Q4 weights ~20 GB):**
- Qwen3-32B: clean GQA scaling, FP16 KV ~4 GB at 16K, fits comfortably to 32K+ inside 32 GB
- Gemma 4: must configure `-np 1` + KV quantization (`--cache-type-k q4_0 --cache-type-v q8_0`); with these flags, 16K context fits in ~22–24 GB total; unconfigured defaults blow past 32 GB

---

## Dimension 5 — Coding & ML Experiment Support

**Winner: GEMMA4** (by benchmarks; caveats on tool-call bugs)

| Benchmark | Gemma 4 31B | Qwen3-32B |
|-----------|-------------|-----------|
| LiveCodeBench v6 | **80.0%** | ~60.6% |
| GPQA Diamond | **~84.3%** | 68.4% |
| SWE-bench Verified | Not published | 15.2% (mini-SWE) |
| HumanEval | Not published | ~high-80s (est.) |

- Gemma 4 beats Qwen3-32B on LiveCodeBench by ~20 points
- Neither model matches **Qwen2.5-Coder-32B** (92.7% HumanEval) or **Qwen3-Coder** (69.6% SWE-bench) for dedicated coding work — the specialist coding worker in Labmate's spec is still justified
- **Can Qwen3-32B replace both orchestrator AND coding specialist?** No — it doesn't beat Gemma 4 on coding, and is weaker than the Qwen2.5-Coder specialist on agentic repo editing
- Gemma 4's multimodal capability adds the ability to read training curves and paper pseudocode as images

**Active vLLM bug that blocks Gemma 4 coding use:** See Dimension 8.

---

## Dimension 6 — Non-CUDA Inference on 32 GB GPU

**Winner: TIE** (both viable; hardware choice matters more than model)

**Best affordable 32 GB non-CUDA GPUs (2026):**

| GPU | VRAM | Price | Notes |
|-----|------|-------|-------|
| **AMD Radeon AI PRO R9700** | 32 GB GDDR6 | **~$1,250** | RDNA4, 640 GB/s; best $/VRAM |
| AMD Radeon Pro W7800 | 32 GB ECC | ~$1,700–4,000 | RDNA3, mature ROCm |
| AMD Radeon Pro W6800 | 32 GB | ~$1,350 used | RDNA2, slowest |
| Apple M4 Max 48 GB | 48 GB unified | ~$2,600+ | MLX, no VRAM pressure |
| Apple M4 Pro 48 GB Mac Mini | 48 GB unified | ~$1,999 | Great value for Apple |

> Note: RX 7900 XTX is only 24 GB — too tight for dense 32B models at 16K+ context. There is **no 32 GB RX 9070 XT** (consumer 9070 XT is 16 GB only).

**Critical Gemma 4 ROCm bug:** On some ROCm/HIP builds, Gemma 4 GGUF quants emit **garbage/corruption tokens**. This does **NOT** occur with the Vulkan backend or with `ggml-org` UD-IQ4_NL quants. **Recommendation: use Vulkan backend on AMD for Gemma 4.**

**Qwen3-32B:** GGUF quants work cleanly on both ROCm and Metal without Gemma 4's SWA-specific issues.

**llama.cpp build flags:**
```bash
# RDNA4 (R9700):
cmake -DGGML_HIP=ON -DCMAKE_HIP_ARCHITECTURES=gfx1201 -DGGML_HIP_ROCWMMA_FATTN=ON ..
# RDNA3 (W7800):
cmake -DGGML_HIP=ON -DAMDGPU_TARGETS=gfx1100 ..
```

**Known RDNA4 HIP bug:** GPU stays pinned at 100% until process exits (ROCm 7.1.x). Vulkan backend is unaffected.

**One 32 GB card fits only one 32B model.** You cannot co-reside Gemma 4 31B + Qwen3-32B simultaneously. Use model swapping or two cards.

---

## Dimension 7 — VRAM Fit & Quantization at 32 GB

**Winner: QWEN3** (simpler, no SWA landmines)

**Weight sizes at each quant level:**

| Quant | Gemma 4 31B | Qwen3-32B |
|-------|-------------|-----------|
| Q4_K_M | ~18–20 GB | ~19.9 GB |
| Q5_K_M | ~22–23 GB | ~23.3 GB |
| Q6_K | ~26–27 GB | ~26.9 GB |
| Q8_0 | Not recommended | ~34.8 GB |

**Critical Gemma 4 quant rule:** Unsloth ships **only one GGUF** for Gemma 4: `UD-Q4_K_XL` (QAT-based). Because Google used quantization-aware training, **higher quants do NOT improve quality and can degrade it**. The usual "use Q5/Q6 for better quality" logic is **inverted** for Gemma 4 — UD-Q4_K_XL IS the quality sweet spot.

**Critical Gemma 4 SWA KV cache rule:** The default SWA checkpoint configuration uses ~3.6 GB per checkpoint × 32 checkpoints = potential 115+ GB of KV VRAM at full defaults. **Fix required:**
```bash
llama-server \
  -m models/gemma-4-31B-it-UD-Q4_K_XL.gguf \
  --jinja \
  -np 1 \
  --cache-type-k q4_0 \
  --cache-type-v q8_0 \
  --ctx-size 16384 \
  --n-gpu-layers 999
```
With these flags: ~22–24 GB total at 16K context. Fits comfortably.

**Co-residency with helpers (bge-small 0.13 GB + bge-reranker-v2-m3 2.3 GB = ~2.4 GB):**
- Qwen3-32B at Q4_K_M + Q8_0 KV at 16K + helpers: ~24–25 GB ✓
- Qwen3-32B at Q5_K_M: drop reranker to CPU if keeping helpers
- Gemma 4 at UD-Q4_K_XL + Q8_0 KV at 16K + helpers: ~24–26 GB ✓ (with -np 1)

---

## Dimension 8 — Agentic Tool Calling & MCP Compatibility

**Winner: QWEN3** (decisive — Gemma 4 has multiple active parser bugs)

### Qwen3-32B tool calling
- **BFCL v3 score: 75.7% (#2 globally**, behind GLM-4.5 at 76.7%)
- Two vLLM parsers: `hermes` (Instruct models) and `qwen3_xml` (recommended for agentic/long-context)
- Tool call format: Hermes/XML → clean JSON, maps naturally to MCP JSON-RPC 2.0
- Known issues with `hermes` parser at long context (use `qwen3_xml` instead)
- Guided decoding (xgrammar) lifts schema-constrained accuracy ~20–25%

### Gemma 4 31B tool calling — ACTIVE BUGS (as of June 2026)

| Issue | Status | Impact |
|-------|--------|--------|
| **#39392:** Shared mutable state in `Gemma4ToolParser` → pad-token corruption under concurrent requests (~2/5 fail, 0/5 sequential) | **OPEN** | Kills agentic throughput |
| **#39468:** Malformed JSON with leaked delimiter artifacts `<\|"\|>` in vLLM 0.19.0 | **OPEN** | Corrupts tool args |
| **#39043:** Reasoning tags + tool-call tags leak into chat when both parsers enabled | **OPEN** | Breaks MCP chain |
| **#41967:** MTP speculative decoding drops first tool-call args in multi-tool streaming | CLOSED | Must disable MTP for tool calls |
| **#39089:** Boolean args corrupted to `trutrue` in streaming | CLOSED | Fixed in PR #39114 |

**Root cause:** Gemma 4 uses a custom, non-JSON tool-call format (`<|tool_call>call:func{key:<|"|>value<|"|>}`) with bare unquoted keys and special delimiter tokens. The vLLM parser translates this — and the translation has multiple active failure modes.

**Workarounds if you must use Gemma 4 for tool calls:**
1. Serialize all tool-call requests (global lock) to avoid #39392 — severe throughput cost
2. Disable MTP speculative decoding to avoid #41967
3. Use non-streaming tool calls where possible
4. Consider the llama.cpp `--jinja` path which bypasses the vLLM `gemma4` parser entirely

**vLLM serve flags for MCP compatibility:**
```bash
# Qwen3-32B (recommended)
vllm serve Qwen/Qwen3-32B \
  --tool-call-parser qwen3_xml \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice

# Gemma 4 31B (wait for parser bugs to be fixed)
vllm serve google/gemma-4-31B-it \
  --tool-call-parser gemma4 \
  --reasoning-parser gemma4 \
  --enable-auto-tool-choice \
  --chat-template examples/tool_chat_template_gemma4.jinja
```

---

## Dimension 9 — Thinking Mode & Reasoning Budget Control

**Winner: QWEN3** (more reliable and portable per-request toggle)

### Qwen3-32B thinking control
- **vLLM (>= 0.9.0):** `chat_template_kwargs: {"enable_thinking": false}` per-request; request overrides server default; `--reasoning-parser qwen3`
- **Soft switch:** Append `/think` or `/no_think` to any system/user message — works per-turn, no API changes needed
- **Per-request budget cap:** `thinking_budget_tokens` in llama.cpp; `nvext.max_thinking_tokens` on NVIDIA NIM
- **Typical thinking overhead:** ~910 thinking tokens + ~663 response tokens = ~1,573 total (2–3x non-thinking)
- **Warning:** SGLang's `thinking_budget` is accepted but **NOT enforced** (open bug) — a 200-token budget still produced ~1,400 reasoning tokens

### Gemma 4 31B thinking control
- **vLLM:** `reasoning_effort: high/medium/low/none` per request; no numeric budget cap available
- **llama.cpp:** `thinking_budget_tokens` (numeric per-request cap) — **only works when server started WITHOUT `--reasoning-budget`** (leave at default -1)
- **`enable_thinking: false` via `chat_template_kwargs` is SILENTLY IGNORED in llama-server** — must use global `--reasoning off` flag to disable
- **Typical thinking overhead:** 4,000+ reasoning tokens on hard problems

### Recommended Labmate LangGraph node config

```python
# Planning / paper-critique / synthesis nodes — thinking ON
extra_body = {"thinking_budget_tokens": 3000}  # llama.cpp
# Or: reasoning_effort="high"  # vLLM

# Tool-dispatch nodes — thinking OFF (2–3x faster, no benefit on dispatch)
extra_body = {"thinking_budget_tokens": 0}  # llama.cpp
# Or: reasoning_effort="none"  # vLLM
```

**Do NOT set `--reasoning-budget N` on the server** — this blocks per-request budget control.

---

## Dimension 10 — Factual Accuracy & Over-Refusal

**Winner: UNCERTAIN** (no XSTest/OR-Bench published for either model at these versions)

### Factual accuracy

| Benchmark | Gemma 4 31B | Qwen3-32B |
|-----------|-------------|-----------|
| GPQA Diamond (reasoned science) | **~84.3%** | 68.4% |
| SimpleQA (short-form recall) | ~19.5 (low-reliability source) | ~15 (community estimate) |
| AA-Omniscience | ~-45 | **~-42** (marginal edge) |

- Both are **weak at short-form parametric factual recall** — neither should be trusted to recall specific citations, statistics, or paper details from memory
- **Both require RAG** over a verified paper corpus for citation-sensitive academic work
- Qwen3 is tuned to "refuse to guess" (CounterFactQA) — fewer hallucinations but lower raw recall
- Gemma 4 wins on **reasoned science factuality** (GPQA Diamond) where CoT matters

### Over-refusal risk

**Gemma lineage history (not Gemma 4 specifically):**
- Gemma-2B: 62.9–92.4% refusal on adversarial probes
- Gemma-2: highest refusal rate on EtiCor benchmarks
- Gemma 3 12B: refuses legitimate professional content (court decisions, clinical notes — GitHub #595)
- **Gemma 4 counter-claim:** Official Google model card explicitly states Gemma 4 "significantly outperforms Gemma 3 and 3n models in improving safety, while keeping unjustified refusals low" — first time Google markets this. Unverified by third-party XSTest.

**Qwen3-32B:**
- Generally more cooperative on Western-framed biosafety/security/dual-use academic topics
- Heavily censors China-political and Chinese cultural/metaphysical content
- Community reports: "very strong refusal rate" unsuitable for unattended batch work out of the box
- XSTest non-refusal 81.2, OR-Bench 39.4 (historical — these are for Qwen3 family, not specifically Qwen3-32B)

**Action required:** Run **XSTest + OR-Bench + domain dual-use probe set** on both models locally before committing the orchestrator. This is the single highest-value measurement to resolve the winner on this dimension.

**Mitigation regardless of model:** Add an external fact-check/judge layer (NOT Gemma or Qwen per Labmate testing rules) to flag fabricated references before they reach a draft.

---

## Hardware Recommendation

### Best option: AMD Radeon AI PRO R9700 (~$1,250)

- RDNA4, 32 GB GDDR6, 640 GB/s bandwidth — RTX 5090-class VRAM at ~1/3 the price
- Fits Q4 31–32B model (~20 GB) + 16K+ KV cache + bge helpers in 32 GB
- **Use Vulkan backend for Gemma 4** (sidesteps the ROCm/HIP token-corruption bug and the RDNA4 HIP idle bug)
- RDNA4 HIP still has a 100%-GPU-pinned idle bug under ROCm 7.1.x — Vulkan is unaffected

### Alternative: Apple M4 Pro/Max (48 GB unified, ~$2,000–2,600)

- MLX is 20–50% faster than llama.cpp for 32B at Q4 on Apple Silicon
- No VRAM-vs-system-RAM distinction — 48 GB works for model + KV + helpers comfortably
- Gemma 4 MLX support landed "almost immediately" after release; both models confirmed working

### What won't work for 16K+ context

- RX 7900 XTX (24 GB): too tight for dense 32B at 16K+ context
- Any 24 GB card: borderline at Q4 weights alone, no headroom for KV cache at 16K

---

## Recommended Labmate Configuration

### Model-switching strategy

```
Paper PDF with figures/charts → Gemma 4 31B (thinking-on, large context)
    ↓
Research ideation / synthesis → Gemma 4 31B (thinking-on, GPQA strength)
    ↓
Tool dispatch / MCP calls → Qwen3-32B (thinking-off, no parser bugs, BFCL 75.7%)
    ↓
Pure-text academic prose drafting → Qwen3-32B (WritingBench 8.64/10)
    ↓
Sensitive-topic academic queries → Qwen3-32B (lower historical over-refusal)
    ↓
ML experiment coding → Gemma 4 31B (LiveCodeBench 80%) or Qwen2.5-Coder-32B specialist
```

### llama.cpp serve command — RunPod A6000 / A6000 Ada (48 GB, CUDA)

```bash
llama-server \
  -m models/gemma-4-31B-it-UD-Q4_K_XL.gguf \
  --jinja \
  --n-gpu-layers 999 \
  --ctx-size 32768 \
  --parallel 2 \
  --host 0.0.0.0 --port 8000 \
  -fa on \
  --reasoning-format deepseek
  # 48 GB absorbs SWA KV cache — no -np 1 or KV quant needed
  # DO NOT add --reasoning-budget N
```

Build: `cmake -DGGML_CUDA=ON -DGGML_CUDA_FA_ATOMIC_CAS=ON ..`

### llama.cpp serve command — Mac Mini 48 GB (Metal)

```bash
llama-server \
  -m models/gemma-4-31B-it-UD-Q4_K_XL.gguf \
  --jinja \
  --n-gpu-layers 999 \
  --ctx-size 32768 \
  --parallel 2 \
  --host 127.0.0.1 --port 8000 \
  -fa on \
  --reasoning-format deepseek
  # Metal is built-in — no build flag needed beyond default cmake
  # DO NOT add --reasoning-budget N
```

### llama.cpp serve command — 32 GB discrete GPU (AMD R9700 / Intel B70 / future card)

```bash
llama-server \
  -m models/gemma-4-31B-it-UD-Q4_K_XL.gguf \
  --jinja \
  --n-gpu-layers 999 \
  --ctx-size 16384 \
  -np 1 \
  --cache-type-k q4_0 \
  --cache-type-v q8_0 \
  --host 127.0.0.1 --port 8000 \
  -fa on \
  --reasoning-format deepseek
  # -np 1 + KV quant required on 32 GB — SWA cache must be bounded
  # AMD R9700: build with -DGGML_VULKAN=ON (NOT HIP — avoids Gemma 4 ROCm bug)
  # Intel B70: build with -DGGML_SYCL=ON + oneAPI SDK
  # DO NOT add --reasoning-budget N
```

### llama.cpp serve command for Qwen3-32B (non-CUDA, Vulkan)

```bash
llama-server \
  -m models/Qwen3-32B-UD-Q4_K_XL.gguf \
  --jinja \
  --n-gpu-layers 999 \
  --ctx-size 32768 \
  --parallel 2 \
  --host 127.0.0.1 --port 8001 \
  -fa on
  # DO NOT add --reasoning-budget N
```

### vLLM serve command for Qwen3-32B (when Gemma 4 parser bugs are resolved, swap for Gemma 4)

```bash
vllm serve Qwen/Qwen3-32B \
  --host 0.0.0.0 --port 8000 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.85 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml \
  --reasoning-parser qwen3
```

---

## Open Issues to Watch

| Issue | Blocks | Status |
|-------|--------|--------|
| vLLM #39392: Gemma4ToolParser concurrent pad-token corruption | Gemma 4 as tool-calling brain | OPEN |
| vLLM #39468: Malformed JSON with delimiter artifacts | Gemma 4 MCP chain | OPEN |
| vLLM #39043: Reasoning/tool-call tag leakage | Gemma 4 + MCP on vLLM | OPEN |
| RDNA4 HIP idle 100% bug | R9700 + ROCm backend | Open (use Vulkan) |
| Gemma 4 GGUF ROCm corruption | R9700 + llama.cpp HIP | Open (use Vulkan) |
| SGLang thinking_budget not enforced | Qwen3 budget control on SGLang | Open (avoid) |

---

## What the Research Didn't Find (Uncertain)

- **No published Gemma 4 31B WritingBench or EssayBench score** — cannot confirm academic prose quality vs Qwen3's 8.64/10
- **No published XSTest or OR-Bench for either model at these versions** — over-refusal verdict is based on lineage + community reports
- **No head-to-head tok/s on a fixed 32 GB non-CUDA card** for either model
- **No confirmed 32 GB RX 9070 XT variant** (consumer 9070 XT is 16 GB only — this GPU doesn't exist yet in 32 GB)
- **No Gemma 4 31B BFCL score** — Gemma 4 is not on the BFCL v3/v4 leaderboard as of June 2026

---

*Research completed 2026-06-17. Sources in `model-comparison-gemma4-qwen3/results/*.json`.*
