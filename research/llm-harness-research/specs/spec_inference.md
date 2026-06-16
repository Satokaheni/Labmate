# Inference & Model Serving Spec

**Project:** Labmate — Local Autonomous Agent
**Target Hardware:** RunPod RTX A6000 (48 GB VRAM, Ampere SM 8.6)
**Status:** Engineering spec — pre-implementation
**Date:** 2026-06-15

---

## 1. Overview

Labmate runs a single large language model as its primary brain on a single A6000 GPU.
The primary model is Gemma 4 MoE (4-bit quantized). Qwen2.5-Coder-32B is the swap-in
fallback when the primary fails. Only one model occupies VRAM at any time (Single-Brain
Policy, section 2.4).

**Unsloth is a quantization and fine-tuning tool, not a serving engine.** It produces
weight files (NF4 safetensors or Dynamic GGUF) that are then loaded by a real serving
engine — vLLM in production, or llama.cpp for a single-box GGUF path. This distinction
is critical and is repeated throughout this document because conflating the two is the
most common source of wasted effort on this class of project.

The serving layer exposes an OpenAI-compatible API (`/v1/chat/completions`) with
server-sent events (SSE) streaming. A thin FastAPI wrapper is provided as a
single-box / transformers fallback. vLLM is the production target.

---

## 2. Architecture

### 2.1 Unsloth (Quantization) vs vLLM (Serving) — Critical Separation

```
  Training / Quantization lane          Production Serving lane
  ────────────────────────────          ──────────────────────────────────
  Hugging Face base weights
          │
          ▼
  Unsloth FastLanguageModel.from_pretrained()
  ├─ load_in_4bit=True (NF4)
  └─ FastLanguageModel.for_inference()
          │
          ├──► save_pretrained_gguf(quantization="q4_k_m") ──► llama.cpp
          │                                                    (GGUF path)
          └──► model.push_to_hub() / save_pretrained()  ──► vLLM
               (NF4 safetensors)                             (production)
```

Unsloth's `FastLanguageModel` and `model.generate()` have:
- No continuous batching
- No PagedAttention
- Serialized request handling (one request at a time)

Throughput collapses to near zero under concurrency when using Unsloth as a server.
Use it only to produce quantized weights, then hand the weights to vLLM or llama.cpp.

### 2.2 Serving Options Comparison (vLLM vs llama.cpp vs Ollama)

| Criterion | vLLM | llama.cpp (`llama-server`) | Ollama |
|---|---|---|---|
| Batching | Continuous batching + PagedAttention | None (basic) | None |
| API | OpenAI-compatible (native) | OpenAI-compatible | OpenAI-compatible |
| Weight format | NF4 safetensors, AWQ, GPTQ | GGUF only | GGUF only |
| Streaming | Native SSE | Native SSE | Native SSE |
| Gemma 4 tool-call parser | `--tool-call-parser gemma4` (built-in) | Manual jinja template | Not confirmed |
| MTP speculative decoding | Roadmap | Merged May 2026 | Via llama.cpp backend |
| FP8 KV cache | `--kv-cache-dtype fp8_e5m2` | `--cache-type-k f16` | Not exposed |
| Multi-user throughput | Excellent | Poor | Poor |
| Single-box simplicity | Moderate | High | Very high |

**Recommendation:** vLLM for production (multi-user, continuous batching). llama.cpp for
the single-box GGUF fallback path, especially if Dynamic GGUF + MTP speculative decoding
is desired. Ollama is not recommended for Labmate — insufficient control over
parser/template configuration.

### 2.3 Memory Budget (48 GB A6000)

The A6000 has 48 GB GDDR6 VRAM. Memory must be divided among: model weights, KV cache,
activations/workspace, and OS/CUDA overhead (~1–2 GB).

#### Weight footprint estimates (4-bit quantization)

```
  Formula: parameters × bits_per_param / 8 = bytes
  NF4 4-bit ≈ 4.5 bits/param with overhead (double quantization blocks)

  Gemma 4 MoE (27B active, ~100B+ total params, MoE routing):
    Active weights (routed): ~27B × 4.5 bits / 8 ≈ ~15 GB
    Full weight file (all experts): varies — expect 20–30 GB on disk
    Resident VRAM (active experts + embedding): estimate 18–22 GB
    NOTE: Exact figure must be measured on the target build.

  Qwen2.5-Coder-32B (dense):
    32B × 4.5 bits / 8 ≈ 18 GB weights
    Practical observed range: 16–20 GB (depends on quant blocks)
```

#### A6000 budget allocation

```
  Total VRAM:                        48.0 GB
  ─────────────────────────────────────────
  CUDA/OS overhead:                  ~1.5 GB
  Primary model weights (Gemma 4):  ~20.0 GB  (conservative, measure actual)
  KV cache (8192 token, FP16):       ~8.0 GB  (see formula below)
  Activation workspace:              ~2.0 GB
  ─────────────────────────────────────────
  Remaining headroom:               ~16.5 GB  (used by vLLM for KV expansion)

  Fallback model (Qwen2.5-Coder-32B, never co-resident):
    Weights:                         ~18 GB
    KV cache (8192, FP16):           ~6–8 GB
    Total: ~24–26 GB — fits after primary is unloaded
```

#### KV cache formula

```
  KV_bytes = 2 × num_layers × num_heads × head_dim × max_seq_len × batch_size × dtype_bytes

  For Qwen2.5-32B (approximate: 64 layers, 40 KV heads, 128 head_dim):
  At batch=1, seq=8192, FP16:
    2 × 64 × 40 × 128 × 8192 × 1 × 2 bytes ≈ 10.7 GB

  vLLM's PagedAttention allocates KV pages on demand and pools across requests,
  so peak KV is lower than worst-case formula under mixed-length batches.
  Set --max-model-len 8192 and --gpu-memory-utilization 0.90 to leave
  the remaining 10% (~4.8 GB) as headroom.
```

#### FP8 KV cache note for A6000

The A6000 is Ampere SM 8.6. It supports `fp8_e5m2` KV cache for **memory savings only**.
It does NOT support `fp8_e4m3` (Hopper/Ada feature). There is no FP8 compute acceleration
on Ampere — FP8 KV cache halves KV memory to extend effective context length but does not
reduce latency. Use `--kv-cache-dtype fp8_e5m2` only when pushing context length past
what FP16 KV can afford.

### 2.4 Single-Brain Policy (no co-residency)

At no point shall both the primary (Gemma 4) and fallback (Qwen2.5-Coder-32B) models
reside in VRAM simultaneously.

**Enforcement:**

- At startup, load exactly one model.
- On fallback activation: explicitly unload the primary and clear CUDA caches before
  loading the fallback.
- vLLM: run two separate vLLM processes or pods; route via a sidecar proxy.
- Transformers path: `del STATE['model']; torch.cuda.empty_cache()` before loading
  the second model.

Loading both at once would consume ~38–44 GB of weights alone, leaving no room for
KV cache and causing an OOM or severe performance degradation due to VRAM fragmentation.

### 2.5 Streaming Architecture

```
  Client (HTTP)
      │
      │  POST /v1/chat/completions
      │  {"stream": true, "messages": [...], "tools": [...]}
      ▼
  ┌─────────────────────────────────────────────────────────┐
  │  FastAPI / vLLM OpenAI-compatible server                │
  │                                                         │
  │  async route handler (event loop thread)                │
  │      │                                                  │
  │      │  spawn background Thread                         │
  │      ▼                                                  │
  │  Thread: model.generate(streamer=TextIteratorStreamer)   │
  │      │                                                  │
  │      │  tokens pushed to streamer queue                 │
  │      ▼                                                  │
  │  async generator: reads streamer, yields SSE frames     │
  │      │  checks request.is_disconnected() each token     │
  │      ▼                                                  │
  │  EventSourceResponse (sse-starlette)                    │
  └─────────────────────────────────────────────────────────┘
      │
      │  data: {"choices":[{"delta":{"content":"token"}}]}
      │  ...
      │  data: [DONE]
      ▼
  Client
```

The background-thread pattern is required to avoid event-loop starvation. Calling
`model.generate()` directly in an `async` route blocks the entire event loop for the
duration of generation — all health checks and concurrent requests stall. See section
6.2 for the implementation.

---

## 3. Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Serving engine (primary) | vLLM | Continuous batching, PagedAttention, native Gemma 4 tool parser, OpenAI-compatible API out of the box |
| Serving engine (fallback) | llama.cpp or FastAPI+transformers | GGUF portability; single-box simplicity when batching is not needed |
| Quantization tool | Unsloth | NF4 Dynamic quants sit on the SOTA KL-divergence Pareto frontier; produces both NF4 (vLLM) and Dynamic GGUF (llama.cpp) |
| Quantization format (vLLM) | NF4 4-bit (bitsandbytes) | Best accuracy-to-VRAM tradeoff for Gemma 4 at 48 GB |
| Quantization format (llama.cpp) | Unsloth Dynamic GGUF (Q4_K_M equivalent) | Higher accuracy than plain Q4_0; MTP draft heads included |
| Attention | FlashAttention-2 | Required for long-context throughput on Ampere; `attn_implementation='flash_attention_2'` |
| Precision (non-quantized tensors) | bfloat16 | Ampere has native bf16 support; fp16 has lower dynamic range and is less stable for long generations |
| Tool-call parser (Gemma 4) | `gemma4_tool_parser` (vLLM) | Gemma 4 uses native `<\|tool_call\|>` delimited calls; incompatible with Gemma 3 pythonic parser |
| Tool-call parser (Gemma 3) | `pythonic` (vLLM) | Gemma 3 emits `[fn(arg='x')]` Python syntax; needs `tool_chat_template_gemma3_pythonic.jinja` |
| Co-residency | Forbidden | 48 GB VRAM is insufficient for two large quantized models plus KV cache |
| Max context | 8192 tokens (configurable) | Balances KV budget against task requirements; reject over-limit requests with HTTP 400 |
| BOS handling | `add_special_tokens=False` after templating | Chat template already prepends `<bos>`; double-BOS corrupts Gemma output |
| Padding side | Always `left` before tokenize | Decoder-only models require left-padding for batch correctness; Unsloth silently flips this |

---

## 4. Quantization with Unsloth

Unsloth is used offline (before deployment) to produce quantized weight files. It is not
invoked at serving time.

### 4.1 NF4 4-bit for vLLM

NF4 (Normal Float 4) with double quantization is the format used when loading weights
into vLLM via bitsandbytes. Unsloth's `FastLanguageModel.from_pretrained` with
`load_in_4bit=True` applies NF4 quantization and exposes the model for further
fine-tuning or saving.

```python
from unsloth import FastLanguageModel
import torch

model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="google/gemma-4-...",   # use actual model id from HF Hub
    max_seq_length=8192,
    dtype=torch.bfloat16,              # bf16 for Ampere; NOT fp16
    load_in_4bit=True,                 # NF4 4-bit quantization
    attn_implementation="flash_attention_2",
)

# Save NF4 safetensors for vLLM to load
model.save_pretrained("./gemma-4-nf4")
tokenizer.save_pretrained("./gemma-4-nf4")
```

vLLM loads the resulting safetensors directory and handles 4-bit inference natively via
its bitsandbytes integration (specify `--quantization bitsandbytes` on the serve
command if auto-detection does not pick it up).

### 4.2 Dynamic GGUF for llama.cpp

Unsloth's Dynamic GGUF quantization selects per-layer quantization levels based on
weight sensitivity, resulting in higher accuracy at the same file size compared to
uniform Q4_0. The Dynamic 2.0 format includes MTP draft heads for speculative decoding.

```python
# After model is loaded (section 4.1 setup)
model.save_pretrained_gguf(
    "gemma-4-dynamic",
    tokenizer,
    quantization_method="q4_k_m",       # Dynamic quant method; not plain Q4_0
)
# Produces: gemma-4-dynamic/gemma-4-unsloth.Q4_K_M.gguf
```

**Do not use plain Q4_0/Q4_K.** Unsloth Dynamic quants measurably outperform naive
uniform quantization on tool-call and structured-output accuracy benchmarks. Always
pull or build the Unsloth Dynamic GGUF, not a generic community Q4_0 GGUF.

### 4.3 Gemma 4 vs Gemma 3 Distinctions

This is a critical branch point that must be detected at load time and propagated to
both the chat template and the tool-call parser.

| Aspect | Gemma 3 | Gemma 4 |
|---|---|---|
| Tool-call format | Pythonic list: `[fn(arg='x')]` | Native delimited: `<\|tool_call\|>{json}<\|tool_call\|>` |
| vLLM parser flag | `--tool-call-parser pythonic` | `--tool-call-parser gemma4` |
| Chat template | `tool_chat_template_gemma3_pythonic.jinja` | Gemma 4 native (bundled with model) |
| Model card source | vLLM PR #17149 | vLLM gemma4_tool_parser (mid-2026) |

The Gemma 3 `pythonic` format is a prompt-engineering workaround where function calls
are emitted as Python expressions and parsed with `ast.literal_eval`. Gemma 4 uses
native tool-call tokens with a JSON body — more reliable and less sensitive to whitespace
or name collisions with Python builtins.

**Uncertainty note:** The exact `<\|tool_call\|>` delimiter regex and whether the target
Gemma 4 build is dense or MoE should be confirmed against the actual model card before
locking the regex in `parse_tool_calls`. The vLLM `gemma4_tool_parser` implementation
is the authoritative reference.

---

## 5. vLLM Production Server

vLLM is the recommended production serving engine. It provides OpenAI-compatible API,
continuous batching, PagedAttention (eliminating KV fragmentation), native SSE
streaming, and built-in Gemma 4 tool-call parsing.

### 5.1 Serve Command (with correct Gemma 4 parser flags)

#### Gemma 4 (primary model)

```bash
vllm serve ./gemma-4-nf4 \
  --host 0.0.0.0 \
  --port 8000 \
  --dtype bfloat16 \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.90 \
  --enable-auto-tool-choice \
  --tool-call-parser gemma4 \
  --quantization bitsandbytes \
  --enforce-eager false \
  --served-model-name gemma-4
```

Key flags:
- `--tool-call-parser gemma4` — activates the Gemma 4 native tool-call parser.
  This is **different from** `--tool-call-parser pythonic` used for Gemma 3. Using
  the wrong parser silently drops every tool call and leaks raw tokens into
  `message.content`.
- `--enable-auto-tool-choice` — required alongside `--tool-call-parser` to activate
  tool-call routing in vLLM.
- `--gpu-memory-utilization 0.90` — reserves ~4.8 GB headroom; tune down if OOM.
- `--dtype bfloat16` — bf16 for Ampere; do NOT use fp16.
- `--max-model-len 8192` — hard cap on prompt + generation length; protects KV budget.

#### Gemma 3 (if substituted)

```bash
vllm serve ./gemma-3-nf4 \
  --host 0.0.0.0 \
  --port 8000 \
  --dtype bfloat16 \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.90 \
  --enable-auto-tool-choice \
  --tool-call-parser pythonic \
  --chat-template tool_chat_template_gemma3_pythonic.jinja \
  --quantization bitsandbytes \
  --served-model-name gemma-3
```

Note the `--chat-template` flag pointing to the Gemma 3 pythonic jinja template
(source: vLLM PR #17149). Do not mix Gemma 3's template with Gemma 4's parser or
vice versa.

#### Qwen2.5-Coder-32B (fallback model — separate process)

```bash
vllm serve ./qwen25-coder-32b-nf4 \
  --host 0.0.0.0 \
  --port 8001 \
  --dtype bfloat16 \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.88 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --quantization bitsandbytes \
  --served-model-name qwen25-coder
```

This process must not run at the same time as the primary vLLM process on the same
A6000. Start it only after the primary process has exited and VRAM is freed.

### 5.2 KV Cache Configuration

vLLM's PagedAttention allocates KV cache in fixed-size pages and pools them across
concurrent requests. This eliminates the memory fragmentation that would occur if each
request pre-allocated the maximum context length.

```bash
# Optional: switch to FP8 KV cache to extend context range
# A6000 supports fp8_e5m2 ONLY — memory savings, no compute speedup
vllm serve ./gemma-4-nf4 \
  ... \
  --kv-cache-dtype fp8_e5m2 \
  --max-model-len 16384     # extended context now feasible with halved KV size
```

```bash
# GPU memory utilization calibration
# Start at 0.90, reduce if vLLM reports OOM during KV page allocation
--gpu-memory-utilization 0.90   # ~43.2 GB usable, ~4.8 GB headroom
--gpu-memory-utilization 0.85   # ~40.8 GB if marginal
```

To pre-estimate required KV allocation before launch:

```
KV_GB = (2 × layers × kv_heads × head_dim × max_seq × dtype_bytes) / 1e9

Example — Gemma 4 (estimate 46 layers, 16 KV heads, 256 head_dim):
  FP16: 2 × 46 × 16 × 256 × 8192 × 2 / 1e9 ≈ 6.2 GB for 1 slot
  vLLM serves many slots via paging; actual KV pool is what remains after weights.
  With ~22 GB weights + 1.5 GB overhead, ~24 GB remains for KV pages at 0.90 util.
```

### 5.3 Gemma 4 Tool Call Parser (`gemma4_tool_parser`)

The `gemma4_tool_parser` is a vLLM built-in parser (available as of mid-2026) that
understands Gemma 4's native tool-call token delimiters.

**How it works inside vLLM:**
1. vLLM's `--enable-auto-tool-choice` flag activates tool routing.
2. The `--tool-call-parser gemma4` flag selects `Gemma4ToolParser` from vLLM's
   internal registry.
3. As tokens are generated, the parser scans for `<|tool_call|>` open and close
   delimiters.
4. The JSON payload between delimiters is extracted and deserialized into
   `ChatCompletionToolCall` objects.
5. These are surfaced in the OpenAI response as `choices[0].message.tool_calls`,
   with no raw tool-call tokens leaking into `choices[0].message.content`.

**Contrast with Gemma 3 pythonic parser:**
- Gemma 3: model emits `[get_weather(city='SF', unit='celsius')]` as Python source text.
  The `pythonic` parser runs `ast.parse` on this string to extract function name and
  keyword arguments. Fragile: sensitive to whitespace, Unicode quotes, and argument
  ordering.
- Gemma 4: model emits a JSON object bounded by native special tokens. Robust: JSON
  parsing is well-defined, and the model was trained explicitly on the token format.

---

## 6. FastAPI Streaming Server (Transformers Fallback)

Use this path when:
- vLLM is unavailable or overkill for single-user workloads.
- You are running GGUF weights under a transformers-compatible loader.
- You need fine-grained control over the tool-call parser (e.g., during debugging).

This is the single-box fallback — not the production path.

### 6.1 Single Model Load Pattern

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from transformers import AutoModelForCausalLM, AutoTokenizer
import torch

STATE: dict = {}
MODEL_VARIANT = "gemma-4"   # or "gemma-3" — drives parser branch; set from config
MAX_MODEL_LEN = 8192

@asynccontextmanager
async def lifespan(app: FastAPI):
    tok = AutoTokenizer.from_pretrained("unsloth/gemma-4-...")
    tok.padding_side = "left"   # initial set; must be re-set before every tokenize call

    model = AutoModelForCausalLM.from_pretrained(
        "unsloth/gemma-4-...",
        load_in_4bit=True,
        torch_dtype=torch.bfloat16,    # bf16 for Ampere — NOT fp16
        device_map="cuda",
        attn_implementation="flash_attention_2",
    )
    STATE["tok"] = tok
    STATE["model"] = model

    # Warmup: one short generation before opening traffic
    warmup_ids = tok("ping", return_tensors="pt").to("cuda")
    model.generate(**warmup_ids, max_new_tokens=1)
    STATE["ready"] = True

    yield   # server runs here

    STATE.clear()   # cleanup on shutdown

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "ready" if STATE.get("ready") else "loading"}
```

The health endpoint reports `loading` until warmup completes. Load balancers or
orchestrators should poll `/health` before routing traffic.

### 6.2 TextIteratorStreamer + `threading.Thread`

The event loop must never be blocked by `model.generate()`. The pattern is:

1. Create a `TextIteratorStreamer` — a synchronous iterator backed by a thread-safe queue.
2. Spawn a daemon `Thread` that runs `model.generate(streamer=streamer, ...)`.
3. The async route handler iterates over the streamer in a `for` loop, yielding to the
   event loop between tokens via `await asyncio.sleep(0)`.

```python
from sse_starlette.sse import EventSourceResponse
from transformers import TextIteratorStreamer
from threading import Thread
import asyncio, json

def enforce_context_limit(input_ids, max_new_tokens: int):
    total = input_ids.shape[-1] + max_new_tokens
    if total > MAX_MODEL_LEN:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "context_length_exceeded",
                "message": f"prompt ({input_ids.shape[-1]}) + max_tokens ({max_new_tokens}) = {total} exceeds limit {MAX_MODEL_LEN}",
            },
        )

async def token_generator(messages: list, tools: list | None, max_new: int, request: Request):
    tok = STATE["tok"]
    model = STATE["model"]

    # CRITICAL: re-set padding_side before EVERY tokenization call.
    # Unsloth's model.generate() silently flips it to 'right' internally.
    tok.padding_side = "left"

    # apply_chat_template with tools= builds the correctly formatted prompt string.
    # add_generation_prompt=True appends the model-turn opener.
    prompt_str = tok.apply_chat_template(
        messages,
        tools=tools,
        add_generation_prompt=True,
        tokenize=False,
    )

    # add_special_tokens=False prevents double-BOS.
    # The chat template already prepends <bos>; the tokenizer must not add another.
    ids = tok(prompt_str, return_tensors="pt", add_special_tokens=False).to("cuda")

    enforce_context_limit(ids["input_ids"], max_new)

    streamer = TextIteratorStreamer(tok, skip_prompt=True, skip_special_tokens=True)

    gen_kwargs = {
        **ids,
        "streamer": streamer,
        "max_new_tokens": max_new,
        "do_sample": False,
    }
    # Daemon thread: model.generate() blocks; the streamer queue bridges to the async loop.
    Thread(target=model.generate, kwargs=gen_kwargs, daemon=True).start()

    buf: list[str] = []
    for piece in streamer:
        # Client disconnect: stop consuming without waiting for generation to finish.
        if await request.is_disconnected():
            break
        buf.append(piece)
        chunk = {
            "choices": [{"delta": {"content": piece}, "finish_reason": None}]
        }
        yield {"data": json.dumps(chunk)}
        await asyncio.sleep(0)   # yield to event loop between tokens

    # Parse tool calls from the accumulated buffer (post-generation).
    full_text = "".join(buf)
    tool_calls = parse_tool_calls(full_text, MODEL_VARIANT)
    if tool_calls:
        tool_chunk = {"choices": [{"delta": {"tool_calls": tool_calls}, "finish_reason": "tool_calls"}]}
        yield {"data": json.dumps(tool_chunk)}

    # OpenAI SSE sentinel
    yield {"data": "[DONE]"}

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not STATE.get("ready"):
        raise HTTPException(status_code=503, detail="model not ready")
    body = await request.json()
    messages = body["messages"]
    tools = body.get("tools")
    max_new = body.get("max_tokens", 512)
    stream = body.get("stream", False)

    if not stream:
        # Non-streaming: collect all tokens and return
        # (omitted for brevity; same logic without the SSE wrapper)
        raise HTTPException(status_code=400, detail="stream=true required in this implementation")

    return EventSourceResponse(token_generator(messages, tools, max_new, request))
```

**Never call `future.result()` from inside the event loop thread.** That is the classic
streaming deadlock: the future waits for the thread, which is waiting for the loop to
drain the streamer queue, which the loop cannot do because it is blocked on `future.result()`.

### 6.3 Version-Branched Tool Call Parser (Gemma 3 vs Gemma 4)

```python
import ast, re, json

def parse_tool_calls(text: str, variant: str) -> list[dict]:
    """
    Branch on model variant to extract tool calls from raw generated text.

    Gemma 3: emits Python call syntax — [fn(arg='x'), ...]
              parse with ast module; fragile but the only Gemma 3 option.

    Gemma 4: emits native delimited JSON — <|tool_call|>{...}<|tool_call|>
              parse with regex + json.loads; robust.
    """
    if variant.startswith("gemma-3"):
        # Pythonic format: [function_name(kwarg=value, ...), ...]
        m = re.search(r"\[.*?\]", text, re.S)
        if not m:
            return []
        try:
            node = ast.parse(m.group(0), mode="eval").body
        except SyntaxError:
            return []
        out = []
        for call in getattr(node, "elts", [node]):
            if not isinstance(call, ast.Call):
                continue
            fn_name = getattr(call.func, "id", None)
            if fn_name is None:
                continue
            args = {}
            for kw in call.keywords:
                try:
                    args[kw.arg] = ast.literal_eval(kw.value)
                except (ValueError, TypeError):
                    args[kw.arg] = ast.unparse(kw.value)
            out.append({"name": fn_name, "arguments": args})
        return out

    else:  # gemma-4 and future variants
        # Native delimited format: <|tool_call|>{json}<|tool_call|>
        # NOTE: confirm exact delimiter string against the Gemma 4 model card.
        out = []
        # Pattern accounts for both <|tool_call|> and <tool_call> variants
        for m in re.finditer(
            r"<\|?tool_call\|?>(.*?)<\|?/?tool_call\|?>",
            text,
            re.S,
        ):
            try:
                obj = json.loads(m.group(1).strip())
                out.append(obj)
            except json.JSONDecodeError:
                pass
        return out
```

**Detect the variant at load time, not at request time:**

```python
def detect_model_variant(model_name_or_path: str) -> str:
    name = model_name_or_path.lower()
    if "gemma-4" in name or "gemma4" in name:
        return "gemma-4"
    elif "gemma-3" in name or "gemma3" in name:
        return "gemma-3"
    # Add future variants here
    raise ValueError(f"Cannot detect Gemma variant from: {model_name_or_path}")
```

### 6.4 Client Disconnect Handling

When a client disconnects mid-stream (network drop, timeout, user cancellation), the
server should stop consuming tokens immediately rather than running generation to
completion and burning VRAM/compute for a response no one will receive.

The `await request.is_disconnected()` check inside the `for piece in streamer` loop
(section 6.2) handles this. When disconnected, the loop `break`s, the generator
exhausts, and `EventSourceResponse` closes the response. The daemon thread running
`model.generate()` continues until the next token's queue push times out or completes,
but this is bounded and harmless — the thread is a daemon and will not block process
shutdown.

For `sse-starlette`'s `EventSourceResponse`, disconnect is also detected at the ASGI
layer and the generator is garbage-collected, so any tokens queued after the `break`
are discarded.

---

## 7. BDD Test Scenarios

```gherkin
Feature: Model loading and warmup
  Scenario: Primary brain loads once and is warmed before serving traffic
    Given the server starts with the Gemma 4 Dynamic 4-bit weights configured as primary
    When the lifespan startup hook runs
    Then the model and tokenizer are loaded exactly once into VRAM
    And tokenizer.padding_side is set to 'left'
    And a warmup generation of a short prompt completes successfully
    And the /health endpoint reports 'ready' only after warmup finishes
    And no second model is resident in VRAM at startup

Feature: Streaming token delivery without blocking the event loop
  Scenario: Tokens stream over SSE while other requests stay responsive
    Given the model is loaded and ready
    When a client POSTs a chat completion with stream=true
    Then generation runs in a background thread feeding a TextIteratorStreamer
    And each token is yielded as an SSE data frame as it is produced
    And a concurrent /health request returns within 200ms during generation
    And the stream terminates with a 'data: [DONE]' sentinel

Feature: Tool-call parsing for Gemma
  Scenario Outline: The correct parser is selected per Gemma variant
    Given the loaded model variant is <variant>
    And tools are supplied via apply_chat_template(tools=...)
    When the model emits a tool call
    Then the <parser> extracts it into structured tool_calls
    And message.content contains no raw tool-call tokens
    Examples:
      | variant | parser          |
      | gemma-3 | pythonic parser |
      | gemma-4 | gemma4 parser   |

Feature: Fallback routing to Qwen2.5-Coder on primary failure
  Scenario: Primary failure swaps in the coder fallback without co-residency
    Given the Gemma primary is loaded and the Qwen2.5-Coder-32B fallback is NOT resident
    When a request to the primary raises a fatal generation error
    Then the router unloads the primary and calls torch.cuda.empty_cache()
    And it loads Qwen2.5-Coder-32B into the freed VRAM
    And the request is retried and answered by the fallback
    And at no point are both models resident simultaneously

Feature: KV cache OOM rejection
  Scenario: An over-budget context is rejected before allocation
    Given max_model_len is configured to fit within the 48 GB KV budget
    When a request arrives whose prompt plus max_tokens exceeds max_model_len
    Then the server returns HTTP 400 with a context_length_exceeded error
    And no KV cache is allocated for the rejected request
    And in-flight generations for other clients are unaffected
```

---

## 8. Common Pitfalls

### 8.1 Using Unsloth as a Production Serving Engine

**Problem:** Running `FastLanguageModel` + `model.generate()` in a web server loop.
Unsloth has no continuous batching and no PagedAttention. Every request is serialized.
Under any concurrency, throughput collapses to single-digit tokens/second and queue
depth explodes.

**Fix:** Use Unsloth only to quantize weights. Serve the output with vLLM or llama.cpp.

---

### 8.2 Padding-Side Flip Bug

**Problem:** Unsloth's `model.generate()` internally resets `tokenizer.padding_side`
from `'left'` to `'right'`. The next batch tokenization will right-pad a decoder-only
model, corrupting batched outputs and triggering the
`"right-padding was detected"` warning.

**References:** unslothai/unsloth issues #3283, #2217, #2138, #2939.

**Fix:** Re-set `tokenizer.padding_side = 'left'` immediately before **every**
tokenization call, not once at init:

```python
# Wrong — set once and forgotten:
tokenizer.padding_side = "left"
# ... later in the request handler ...
ids = tokenizer(prompt)   # padding_side may have been flipped to 'right'

# Correct:
tokenizer.padding_side = "left"   # re-set immediately before
ids = tokenizer(prompt)
```

The bug is entirely avoided by serving via vLLM, which manages padding internally.

---

### 8.3 Double-BOS Token Corruption

**Problem:** Applying `apply_chat_template()` (which prepends `<bos>`) and then
calling `tokenizer(...)` with `add_special_tokens=True` (the default) prepends a
second `<bos>`. Gemma models degrade noticeably with double-BOS and emit the warning
`"Detected duplicate leading <bos> in prompt"`.

**Additional complication:** `llama.cpp` has a hardcoded `add_bos=true` in some builds.
`--override-kv tokenizer.ggml.add_bos_token=bool:false` does NOT reliably suppress it
in all builds. The safest fix is to ensure the chat template does not include `<bos>`,
or to pass `add_special_tokens=False` in the transformers path.

**Fix:**
```python
# Correct: suppress automatic BOS after templating
ids = tokenizer(
    prompt_str,
    return_tensors="pt",
    add_special_tokens=False,   # template already added <bos>
)
```

---

### 8.4 Chat Template / Parser Mismatch Between Model Variant and Engine Parser

**Problem:** Using `--tool-call-parser pythonic` for a Gemma 4 model (or
`--tool-call-parser gemma4` for a Gemma 3 model) causes the parser to fail silently.
Tool calls are not extracted into `tool_calls`; instead, raw tool-call tokens leak into
`message.content`. The model appears not to use tools even when it is generating them.

**Fix:** Detect the model variant at startup and select the matching parser + template.
Never reuse a Llama/Hermes parser for any Gemma variant.

| Model | vLLM flag | Chat template |
|---|---|---|
| Gemma 3 | `--tool-call-parser pythonic` | `tool_chat_template_gemma3_pythonic.jinja` |
| Gemma 4 | `--tool-call-parser gemma4` | Gemma 4 native (bundled) |

---

### 8.5 Naive Q4_0 Accuracy Degradation

**Problem:** Community GGUF files quantized with plain Q4_0 or uniform Q4_K measurably
degrade tool-call and structured-output accuracy compared to Unsloth Dynamic 2.0 quants.
The Dynamic format selects per-layer quantization levels based on weight sensitivity
(minimizing KL divergence) and sits on the SOTA Pareto frontier.

**Fix:** Always pull or build the Unsloth Dynamic GGUF for the target model, not a
generic Q4_0 GGUF from the community. The Unsloth Hub namespace is `unsloth/<model>-GGUF`.

---

### 8.6 KV Cache OOM at Long Context

**Problem:** FP16 KV cache grows as `O(layers × heads × head_dim × context × batch)`.
Weight quantization (GPTQ, AWQ, NF4) does not reduce KV cache size — it only reduces
weight storage. A 32B model at long context with batched requests can OOM purely on KV
even when weights fit comfortably.

**Fix:**
1. Set `--max-model-len 8192` (or lower) as a hard cap.
2. Set `--gpu-memory-utilization 0.90` — vLLM uses the remainder for KV pages.
3. Reject over-limit requests at the API boundary (HTTP 400) before KV allocation.
4. Optionally use `--kv-cache-dtype fp8_e5m2` for memory savings on A6000 (no latency
   improvement on Ampere — see section 8.8).

---

### 8.7 asyncio Deadlock / Event-Loop Starvation

**Problem:** Calling blocking `model.generate()` directly inside an `async` FastAPI
route freezes the entire event loop. All health checks, concurrent requests, and
internal FastAPI machinery stall for the full duration of generation.

Second form: calling `future.result()` from inside the same event loop thread that the
future is running on — classic deadlock where the loop cannot advance to complete the
future because it is blocked waiting for the future.

**Fix:** Run `model.generate()` in a daemon `Thread` feeding a `TextIteratorStreamer`.
The async handler iterates the streamer (which is a synchronous iterator backed by a
thread-safe queue) and yields to the event loop between tokens via `await asyncio.sleep(0)`.
Do not use `loop.run_in_executor` with `model.generate` unless you are certain the
executor pool size is not exhausted — `Thread` is simpler and more predictable here.

---

### 8.8 FP8 KV Cache Is Not a Latency Win on A6000

**Problem:** Enabling FP8 KV cache on A6000 with `fp8_e4m3` raises:
`"type fp8e4nv not supported in this architecture"`. A6000 is Ampere SM 8.6 and
supports only `fp8_e5m2` — and only for memory, not for compute. Expecting a latency
speedup from FP8 KV on this GPU will be disappointed.

**Fix:** Use `--kv-cache-dtype fp8_e5m2` only when you need to push context length
beyond what FP16 KV can afford on 48 GB. Do not use it as a performance optimization.
FP8 compute acceleration requires Hopper (H100) or Ada (RTX 4090).

---

### 8.9 GPU Memory Fragmentation from Multiple Model Loads

**Problem:** Loading the primary model, then the fallback into the same Python process
without explicitly freeing the primary first fragments VRAM. Even if nominal weight
totals would fit, fragmentation causes an OOM or leaves insufficient contiguous memory
for KV pages.

**Fix:** Never co-reside both models. Before loading the fallback:

```python
del STATE["model"]
torch.cuda.empty_cache()
# Now load the fallback
STATE["model"] = AutoModelForCausalLM.from_pretrained(...)
```

In vLLM: run the primary and fallback as separate processes (or pods) and route between
them at the proxy layer. Never load two models into one vLLM instance.

---

## 9. Dependencies

### Python packages

| Package | Min version | Role |
|---|---|---|
| `unsloth` | >=2024.8 | NF4 4-bit quantization + Dynamic GGUF export; fine-tuning. NOT serving. |
| `vllm` | >=0.6.x | Production serving engine: AsyncLLMEngine, continuous batching, OpenAI API, `gemma4` tool parser. |
| `bitsandbytes` | >=0.43 | NF4 4-bit kernels used by Unsloth and transformers `load_in_4bit`. |
| `transformers` | >=4.44 | Tokenizer, `apply_chat_template(tools=)`, `TextIteratorStreamer`, `generate()`. |
| `peft` | >=0.12 | LoRA/QLoRA adapter handling if serving adapters on top of the base model. |
| `accelerate` | >=0.33 | `device_map` dispatch for single-GPU loading. |
| `torch` | >=2.3 | Use bf16 on A6000 (Ampere native). Do NOT use fp16 for primary inference. |
| `fastapi` | >=0.110 | ASGI web framework for the OpenAI-compatible HTTP/SSE wrapper. |
| `uvicorn` | latest | ASGI server for FastAPI. |
| `sse-starlette` | >=2.1 | `EventSourceResponse` for clean SSE + client-disconnect handling. |
| `flash-attn` | >=2.5 | FlashAttention-2 kernels for long-context attention on Ampere. Required for `attn_implementation='flash_attention_2'`. |

### System / CUDA

| Component | Requirement |
|---|---|
| CUDA | >=12.1 (for bf16 + FlashAttention-2 on Ampere) |
| cuDNN | >=8.9 |
| Python | >=3.10 |
| GPU | NVIDIA RTX A6000 48 GB (Ampere SM 8.6) |
| llama.cpp | Build from source post May 2026 for MTP speculative decoding support |

### RunPod template recommendation

Use a RunPod template with CUDA 12.1+ and PyTorch 2.3+ pre-installed. Install vLLM
via `pip install vllm` (includes FlashAttention-2 for Ampere). Install Unsloth
separately in the quantization environment — it need not be present on the serving pod.

---

## 10. Reference Papers & Repos

### Papers

| Paper | ArXiv | Relevance |
|---|---|---|
| QLoRA: Efficient Finetuning of Quantized LLMs (Dettmers et al., 2023) | 2305.14314 | NF4 4-bit quantization + double quantization — the foundation of Unsloth's 4-bit loading |
| PagedAttention / vLLM: Efficient Memory Management for LLM Serving (Kwon et al., 2023) | 2309.06180 | PagedAttention + continuous batching — the core reason to serve via vLLM instead of Unsloth |
| FlashAttention-2: Faster Attention with Better Parallelism (Dao, 2023) | 2307.08691 | Fused attention kernel for long-context throughput on Ampere |
| GPTQ: Accurate Post-Training Quantization for Generative Pre-trained Transformers (Frantar et al., 2022) | 2210.17323 | Weight-only PTQ; alternative serving path to NF4/GGUF (vLLM supports GPTQ natively) |
| AWQ: Activation-aware Weight Quantization (Lin et al., 2023) | 2306.00978 | Activation-aware 4-bit; another common vLLM serving format |
| Better & Faster LLMs via Multi-Token Prediction (Gloeckle et al., 2024) | 2404.19737 | Theory behind MTP draft heads now shipped in Unsloth Dynamic 2.0 GGUFs |

### Repositories

| Repo | URL | Role |
|---|---|---|
| unslothai/unsloth | https://github.com/unslothai/unsloth | 4-bit NF4 + Dynamic GGUF quantization + fine-tuning. `FastLanguageModel` loader. Not a batching server. |
| vllm-project/vllm | https://github.com/vllm-project/vllm | Production serving engine: PagedAttention, continuous batching, OpenAI-compatible API, `gemma4` and `pythonic` tool parsers. |
| ggml-org/llama.cpp | https://github.com/ggml-org/llama.cpp | GGUF inference engine, `llama-server` SSE, MTP speculative decoding (merged May 2026). |
| huggingface/transformers | https://github.com/huggingface/transformers | `AutoModelForCausalLM`, `TextIteratorStreamer`, `apply_chat_template(tools=)`. |
| fastapi/fastapi | https://github.com/fastapi/fastapi | ASGI web framework for the OpenAI-compatible wrapper + `StreamingResponse` SSE. |
| vllm-project/vllm PR #17149 | https://github.com/vllm-project/vllm/pull/17149 | Reference Gemma 3 pythonic tool-call chat template (`tool_chat_template_gemma3_pythonic.jinja`). |

---

## 11. SOTA Improvements

### 11.1 vLLM Continuous Batching (Primary Win)

PagedAttention + continuous batching in vLLM gives multi-x throughput under concurrency
compared to serialized `model.generate()`. This is the single highest-leverage
improvement for Labmate if it ever transitions from single-user to multi-user or
agent-loop workloads with concurrent tool calls. The reason not to serve from Unsloth
or raw transformers is almost entirely this.

### 11.2 MTP Speculative Decoding (Unsloth Dynamic 2.0 + llama.cpp)

Multi-token prediction draft heads are now shipped in Unsloth Dynamic 2.0 GGUFs and
merged into llama.cpp (May 2026). This gives approximately:
- Dense models (including Gemma 4 if dense or near-dense): ~1.5–2.2x generation speedup.
- MoE models (Gemma 4 full MoE): ~1.15–1.25x (less benefit; MoE routing is the bottleneck).
- Cost: ~2 GB extra VRAM for the draft heads.
- Tunable via `--spec-draft-n-max` in range 1–6.

**Benchmark before committing.** Published figures (1.4–2.2x) are aggregate across
models and hardware. The exact speedup for the target Gemma 4 quant on the A6000 must
be measured empirically.

### 11.3 FlashAttention-2

Required for long-context throughput on Ampere. Enabled via:
- Transformers: `attn_implementation='flash_attention_2'` in `from_pretrained()`.
- vLLM: enabled by default when `flash-attn` is installed.

Without FlashAttention-2, attention complexity is `O(n²)` in memory, limiting effective
context length and throughput on the A6000.

### 11.4 FP8 KV Cache on A6000 (Context Extension)

`--kv-cache-dtype fp8_e5m2` halves KV memory usage, allowing the same VRAM to support
roughly 2x the context length. This is a **memory-only** win on Ampere — no latency
reduction. Use when context requirements push past what FP16 KV can afford at 48 GB.

### 11.5 ExLlamaV2 (Alternative Single-Box Backend)

ExLlamaV2 (EXL2 quantization format) offers very fast 4-bit single-GPU inference with
low memory overhead. A strong alternative to llama.cpp for the single-box fallback path
when vLLM's batching infrastructure is not needed. Lower operational complexity than
vLLM at the cost of the batching / multi-user features.

### 11.6 Gemma 4 Native Function Calling vs Gemma 3 Prompt Engineering

Gemma 4's native tool-call tokens + `gemma4_tool_parser` are more reliable than Gemma
3's pythonic-list prompt-engineering approach for structured tool calls. The Gemma 4
format is trained natively and JSON-based; Gemma 3's format is a post-hoc text-format
convention. If both are viable, prefer Gemma 4 for any workload that is tool-call-heavy.
Detect the variant at model load time and route to the appropriate parser — never mix.

---

*End of spec. See `/research/llm-harness-research/results/local_model_serving_unsloth.json` for the underlying research artifact.*
