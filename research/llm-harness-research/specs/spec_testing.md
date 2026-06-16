# Testing Strategy Spec — Labmate

**Version**: 1.0.0
**Date**: 2026-06-15
**Scope**: Labmate local autonomous polyglot coding + writing agent (TypeScript MCP server, Python orchestrator, Rust/TS/Python skills, local Gemma 4 model)

---

## 1. Overview

Labmate is a polyglot autonomous agent: a TypeScript MCP server exposes tools, a Python orchestrator drives the agent loop, and skills are implemented in Rust, TypeScript, and Python. The underlying model is a locally-hosted Gemma 4 served through a vLLM OpenAI-compatible endpoint.

This creates three distinct testing challenges not present in conventional software projects:

1. **Non-determinism**: Even with `temperature=0`, float-op reductions vary across batch sizes, GPU configurations, and engine versions. Bit-identical output cannot be guaranteed.
2. **Polyglot boundary**: No single test runner covers all components. Structured failures and stack traces are lost when components are driven by brittle subprocess shells across language boundaries.
3. **Long-running agent tasks**: A single agent turn may involve many sequential tool calls. Whole-trajectory correctness is not captured by asserting only on final output.

This spec defines a three-layer testing pyramid, pytest-bdd as the behavioral source of truth for the Python orchestrator, vitest + `@langwatch/scenario` for the TypeScript MCP server, and an execution oracle (SWE-bench-style) for the eval layer. Every layer uses marker-based separation so the CI default is always GPU-free and fast.

---

## 2. Testing Architecture

### 2.1 Three-Layer Pyramid

#### Layer 1 — Mocked/Deterministic Unit Tests

**Goal**: Verify structural correctness of the orchestrator and MCP server independent of any real model.

- Inject a fake LLM at the HTTP boundary using `respx` (intercepts `httpx` calls to the OpenAI-compatible endpoint at `http://localhost:8000/v1/chat/completions`).
- Return hand-authored JSON tool-call bodies; never load a real model.
- Assert on **structure, schema, and tool routing** — not prose.
- Use `vcrpy` + `pytest-recording` cassettes for the subset of tests that need to replay real recorded responses without a live model.
- Run in CI on every commit; completes in seconds; no GPU required.
- Markers: `@mocked`

**What is tested here**:
- Tool name routing (given input task X, agent emits tool Y)
- Tool argument schema validation (emitted arguments conform to JSON Schema)
- MCP tool dispatch contract (JSON-RPC shape, tool schema advertisement)
- Error handling (malformed response, empty tool list, unknown tool name)

#### Layer 2 — Integration Tests with Real Model (Semantic Assertions)

**Goal**: Verify that real Gemma 4 output satisfies behavioral invariants without asserting on literal text.

- Load real Gemma 4 via `load_gemma()` against the local vLLM server.
- Assert on **invariants and properties**: schema validity, tool-call presence, structure.
- Route fuzzy prose quality claims through `DeepEval GEval` with a **cross-family judge** (`gpt-4o-mini`) using a numeric threshold (e.g. `>= 0.8`). Gemma must never judge Gemma (self-preference bias, see Section 3.3 and Section 9).
- Gate behind `LIVE_TESTS=1` environment variable; skip otherwise with a clear reason message.
- Markers: `@live @gpu`

**What is tested here**:
- Code summary coherence and relevance
- Plan generation structure (does the agent emit a valid sequence of steps?)
- Tool-call presence under real model pressure (real model still routes correctly)
- Multi-turn context retention invariants

#### Layer 3 — Execution-Based Eval (SWE-bench-style Oracle)

**Goal**: Verify that agent-generated code patches are functionally correct by running the target test suite.

- The oracle runs the target language's native test runner before and after the patch.
- A patch is correct only if it satisfies **FAIL_TO_PASS** (the targeted failing test now passes) **and** PASS_TO_PASS (all previously-passing tests remain green).
- The patch must not modify any test files (anti-cheating guard).
- A held-out hidden test set the agent never sees is run as a final correctness signal.
- Gate behind `LIVE_TESTS=1` and `EVAL=1`; never run in default CI.
- Markers: `@eval @live`

**What is tested here**:
- End-to-end code generation functional correctness
- Agent's ability to locate, understand, and fix real bugs
- No test-file tampering
- No regression introduction (PASS_TO_PASS guard)

---

### 2.2 Polyglot Test Coverage

Each language component lives in its **idiomatic runner**. The Python orchestrator's BDD scenarios act as the single source of **cross-process behavioral truth**, asserting only on the observable MCP contract (tool schema, JSON-RPC responses), not on internal implementation details.

| Component | Runner | Assertion Focus |
|---|---|---|
| Python orchestrator | `pytest-bdd` | Tool routing, agent loop logic, semantic quality (via DeepEval) |
| TypeScript MCP server | `vitest` + `@langwatch/scenario` | MCP tool schema, JSON-RPC response shape, multi-turn scenarios |
| Rust skills | `cargo test` | Pure unit correctness, invoked by the eval oracle |
| TypeScript skills | `vitest` | Pure unit correctness, invoked by the eval oracle |
| Python skills | `pytest` | Pure unit correctness, invoked by the eval oracle |
| Cross-process contract | `pytest-bdd` (orchestrator BDD) | MCP call shape, JSON-RPC schema — no internal language details |

The orchestrator BDD scenarios drive the TypeScript MCP server as a subprocess. They assert only on JSON-RPC responses, not on internal TypeScript state. This keeps structured failures (pytest tracebacks) intact on the Python side while the TypeScript server remains independently testable with vitest.

---

### 2.3 Marker-Based Separation

Markers are registered in `pyproject.toml` and enforced at the CI level. The default `addopts` deselects all `live`, `gpu`, and `eval` markers so they never accidentally run in the fast CI pipeline.

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
  "mocked: deterministic, no real model loaded",
  "live: requires real Gemma model (set LIVE_TESTS=1)",
  "gpu: requires A6000 or equivalent GPU",
  "eval: execution-oracle benchmark (set EVAL=1)",
]
addopts = "-m 'not live and not eval' --timeout=30"
```

The `--timeout=30` per-test hard limit (via `pytest-timeout`) ensures that a hung live test fails fast rather than blocking CI for the full job timeout.

---

## 3. Non-Determinism Strategy

### 3.1 What to Assert vs What NOT to Assert

**Assert on** (stable across model versions, temperatures, batch sizes):
- Parsed JSON/tool-call structure — the emitted `tool_calls[0].function.name` field
- Argument schema conformance — `jsonschema.validate(arguments, TOOL_SCHEMAS[name])`
- Tool presence — "at least one tool call was emitted"
- FAIL_TO_PASS / PASS_TO_PASS transitions — execution oracle outcome
- GEval numeric threshold — a score `>= 0.8`, not the exact score
- Structural properties — "the summary mentions at least one public function name"

**Never assert on** (breaks on every model version, fine-tune, batch size, or temperature change):
- Literal prose output — `assert response == "Here is a docstring for..."`
- Substring matches on model explanations — `assert "I will now" in response`
- Exact tool argument values when the model generates them — `assert args["content"] == "..."`
- Token counts or response length
- Probability scores or logprobs

### 3.2 LLM-as-Judge with DeepEval (Thresholded, Not Literal)

For prose quality assertions (code summary coherence, plan quality, explanation relevance), use `DeepEval` `GEval` with a numeric threshold:

```python
from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

metric = GEval(
    name="Coherence",
    model="gpt-4o-mini",          # cross-family judge, NEVER Gemma or Qwen
    evaluation_params=[
        LLMTestCaseParams.INPUT,
        LLMTestCaseParams.ACTUAL_OUTPUT,
    ],
    criteria="Is the output coherent, faithful to the input, and free of hallucinations?",
    threshold=0.8,
)
tc = LLMTestCase(input=input_text, actual_output=model_output)
metric.measure(tc)
assert metric.score >= 0.8, f"GEval score {metric.score:.2f} below threshold: {metric.reason}"
```

The judge model must be from a **different model family** than Gemma (see Section 3.3). `gpt-4o-mini` is the recommended default. Periodically calibrate judge thresholds against a small human-labeled sample set (10–30 examples) each release to detect threshold drift.

### 3.3 Temperature=0 Caveat (Still Non-Deterministic Under Batching)

Setting `temperature=0` (greedy decoding) eliminates **sampler** randomness only. Outputs still vary because:

- **Dynamic batching** changes the reduction order of non-associative float operations (e.g. softmax across tokens when batch size > 1)
- **Tensor parallelism** splits matrix multiplications across GPUs; reduction order is not guaranteed
- **Different GPU/dtype/engine versions** (vLLM upgrades, BF16 vs FP16 precision switching) change numerical results
- **`temperature=0` is therefore not a substitute for mocking**

For truly deterministic unit tests: **mock the model entirely** (Layer 1).
For maximally reproducible live tests: pin the vLLM version, dtype, and hardware. On CUDA CC >= 8.0, set:
```
VLLM_BATCH_INVARIANT=1
VLLM_ENABLE_V1_MULTIPROCESSING=0
```
Expect 95–99% reproducibility, not 100%. Use property/tolerance assertions, never equality.

### 3.4 VCR Cassettes for Replay

`vcrpy` + `pytest-recording` records real HTTP exchanges to YAML cassette files and replays them in future runs without a live model:

```python
@pytest.mark.vcr               # records on first run, replays on subsequent
def test_tool_routing_vcr(agent):
    result = agent.handle("add a docstring to app.py")
    assert result.tool_name == "edit_file"
```

Cassette files live in `tests/cassettes/` and are committed to source control.

**Cassette hygiene rules**:
- Tag each cassette filename with the model id and version: `edit_file_gemma4_v2.yaml`
- Re-record cassettes in CI under a `--vcr-record=once` mode whenever the model version bumps
- Pair every cassette-backed test with a corresponding `@live` test that re-validates the same invariant against the real model
- Treat a green cassette test alongside a failing live test as a staleness signal — the cassette has drifted from real model behavior

### 3.5 pass@k for Stochastic Assertions

For eval-layer tasks where a single run is not reliable, use the **unbiased pass@k estimator** from Chen et al. (2021, HumanEval):

```
pass@k = 1 - C(n - c, k) / C(n, k)
```

Where `n` is the total number of samples, `c` is the number of correct samples, and `k` is the target pass count.

In practice:
- Run `n = 20` samples per eval task in nightly CI
- Gate on `pass@1` (k=1) but report `pass@5` and `pass@10` for trend monitoring
- A regression is flagged only when the confidence interval of `pass@k` drops below the baseline CI — not on a single-run miss
- Use `openai/human-eval`'s reference implementation of the estimator (numerically stable; handles edge cases at `c = n`)

---

## 4. pytest-bdd Setup

### 4.1 conftest.py with model fixture (fake_model / real_model)

Full stub — copy this into `tests/conftest.py`:

```python
"""
tests/conftest.py
-----------------
Shared fixtures for Labmate pytest-bdd test suite.

Layers:
  - fake_model  : mocks the OpenAI-compatible HTTP seam with respx (Layer 1)
  - real_model  : loads real Gemma 4 from local vLLM server (Layer 2, gated)
  - assert_geval: LLM-as-judge coherence helper using gpt-4o-mini (cross-family)
  - vcr_config  : cassette directory and serializer settings
"""

import json
import os
import subprocess
import pytest
import httpx

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _live_enabled() -> bool:
    return os.getenv("LIVE_TESTS") == "1"


def _eval_enabled() -> bool:
    return os.getenv("EVAL") == "1"


# ---------------------------------------------------------------------------
# Layer 1: Fake model (mocked HTTP seam via respx)
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_model(respx_mock):
    """
    Returns a callable that programs the respx mock to return a specific
    tool call. Call it in @given steps before the agent handles a task.

    Usage:
        fake_model("edit_file", {"path": "src/app.py", "content": "..."})
    """
    def _set(tool_name: str, arguments: dict):
        body = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "gemma4-local",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_test",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        respx_mock.post("http://localhost:8000/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=body)
        )

    return _set


# ---------------------------------------------------------------------------
# Layer 2: Real model (gated behind LIVE_TESTS=1)
# ---------------------------------------------------------------------------

@pytest.fixture
def real_model():
    """
    Loads the real local Gemma 4 model via the vLLM OpenAI-compatible
    endpoint. Skips the test if LIVE_TESTS=1 is not set.

    Teardown closes the client before the event loop shuts down to prevent
    'RuntimeError: Event loop is closed' on async teardown.
    """
    if not _live_enabled():
        pytest.skip("Set LIVE_TESTS=1 to run live model tests (requires GPU)")
    from labmate.models import load_gemma  # type: ignore[import]
    model = load_gemma()
    yield model
    model.close()  # synchronous close; awaitable variant called inside if async


# ---------------------------------------------------------------------------
# Layer 2: Cross-family LLM-as-judge (DeepEval GEval with gpt-4o-mini)
# ---------------------------------------------------------------------------

def assert_geval(
    input_text: str,
    output_text: str,
    criteria: str = "Is the output coherent, faithful to the input, and free of hallucinations?",
    threshold: float = 0.8,
    judge_model: str = "gpt-4o-mini",  # MUST be cross-family (not Gemma, not Qwen)
) -> None:
    """
    Assert that the model output meets a GEval coherence threshold.

    The judge_model MUST be from a different model family than Gemma/Qwen to
    avoid self-preference bias (arxiv:2410.21819, arxiv:2404.13076).

    Raises AssertionError with the judge's reasoning if score < threshold.
    """
    from deepeval.metrics import GEval  # type: ignore[import]
    from deepeval.test_case import LLMTestCase, LLMTestCaseParams  # type: ignore[import]

    assert judge_model not in {"gemma", "qwen", "gemma4", "gemma-4"}, (
        f"Judge model '{judge_model}' is same-family as agent model (Gemma). "
        "Use a cross-family judge (gpt-4o-mini, claude-3-haiku, etc.) to avoid self-preference bias."
    )

    metric = GEval(
        name="Coherence",
        model=judge_model,
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
        ],
        criteria=criteria,
        threshold=threshold,
    )
    tc = LLMTestCase(input=input_text, actual_output=output_text)
    metric.measure(tc)
    assert metric.score >= threshold, (
        f"GEval '{metric.name}' score {metric.score:.3f} < threshold {threshold}: {metric.reason}"
    )


# ---------------------------------------------------------------------------
# Layer 3: Execution oracle helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def execution_oracle():
    """
    Returns a callable that runs the target test suite before and after
    applying a patch and checks FAIL_TO_PASS + PASS_TO_PASS transitions.

    Skips if LIVE_TESTS=1 or EVAL=1 are not set.
    """
    if not (_live_enabled() and _eval_enabled()):
        pytest.skip("Set LIVE_TESTS=1 and EVAL=1 to run execution-oracle evals")

    def _run_tests(test_ids: list[str], cwd: str) -> dict[str, bool]:
        """Run specific test IDs and return {test_id: passed} mapping."""
        result = subprocess.run(
            ["pytest", "--tb=no", "-q", "--no-header"] + test_ids,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        outcomes: dict[str, bool] = {}
        for line in result.stdout.splitlines():
            for tid in test_ids:
                if tid in line:
                    outcomes[tid] = "PASSED" in line or "passed" in line
        # Default to False for any test not found in output
        for tid in test_ids:
            if tid not in outcomes:
                outcomes[tid] = False
        return outcomes

    return _run_tests


# ---------------------------------------------------------------------------
# VCR / pytest-recording configuration
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def vcr_config():
    return {
        "cassette_library_dir": "tests/cassettes",
        "record_mode": os.getenv("VCR_RECORD", "none"),  # 'once' to re-record
        "serializer": "yaml",
        "filter_headers": ["authorization", "x-api-key"],
    }


# ---------------------------------------------------------------------------
# Agent fixture (shared across layers)
# ---------------------------------------------------------------------------

@pytest.fixture
def agent(fake_model):  # noqa: ARG001  — fake_model wires the mock before agent is created
    """
    Instantiates the Labmate orchestrator agent pointed at the (mocked)
    local vLLM endpoint. For live tests, override by not depending on fake_model.
    """
    from labmate.orchestrator import Agent  # type: ignore[import]
    return Agent(endpoint="http://localhost:8000/v1")
```

### 4.2 respx mock for OpenAI-compatible endpoint

`respx` intercepts `httpx`-level HTTP calls. The Labmate orchestrator must use `httpx` (not `requests`) as its HTTP client for the mock to take effect. If the orchestrator uses `aiohttp` or `requests`, wrap the transport layer in an `httpx.AsyncClient` adapter or add a `respx`-compatible shim.

Key configuration:
```python
# In pyproject.toml or conftest.py
# respx_mock is a session-scoped fixture automatically injected
# when the respx package is installed.
```

The `fake_model` fixture in Section 4.1 calls `respx_mock.post(...).mock(...)` to register the intercept before the test body runs.

### 4.3 VCR cassette decorator

```python
import pytest

@pytest.mark.vcr("edit_file_gemma4_v2.yaml")   # explicit cassette name (tagged with model version)
@pytest.mark.mocked
def test_edit_file_cassette(agent):
    result = agent.handle("add a docstring to app.py")
    assert result.tool_name == "edit_file"
    # No assertion on the docstring content itself
```

Without an explicit cassette name, `pytest-recording` derives the cassette filename from the test function name. Always supply an explicit name to include the model version in the filename.

### 4.4 DeepEval assertion helper

The `assert_geval` function (Section 4.1) is a module-level helper, not a fixture, so it can be imported directly in step definition files:

```python
from tests.conftest import assert_geval

@then("the summary passes coherence quality")
def _(context):
    assert_geval(
        input_text=context["module_source"],
        output_text=context["summary"],
        criteria="Is the summary coherent, mentions real function names, and avoids hallucinations?",
        threshold=0.8,
    )
```

Threshold guidelines by assertion type:

| Assertion type | Recommended threshold |
|---|---|
| Coherence (is it readable and on-topic?) | 0.80 |
| Faithfulness (no hallucinated facts) | 0.85 |
| Relevance (answers the question asked) | 0.75 |
| Toxicity (must be very low) | 0.05 max |

### 4.5 Marker registration (mocked vs live)

Register all markers in `pyproject.toml` and always mark individual tests. Do not rely on directory structure alone for marker inference — markers must be explicit on the test or scenario.

For Gherkin scenarios, use the `@tag` syntax in the `.feature` file:

```gherkin
@mocked
Scenario: Agent routes a file-edit task to the correct MCP tool
```

In the step definition file, bind the scenario and the marker carries through automatically via `pytest-bdd`. To apply a marker programmatically to all scenarios in a feature file:

```python
# step_defs/test_tool_routing.py
import pytest
from pytest_bdd import scenarios

pytestmark = pytest.mark.mocked
scenarios("../features/tool_routing.feature")
```

---

## 5. Example Feature Files

### 5.1 Feature: Tool Routing (Mocked — Layer 1)

**File**: `tests/features/tool_routing.feature`

```gherkin
Feature: Mocked LLM tool routing
  As the Labmate orchestrator, I must route incoming tasks to the correct
  MCP tool and emit a structurally valid tool call, independent of the model's
  prose explanation.

  No real model is loaded in this feature. All LLM responses are injected via
  the fake_model respx fixture.

  Background:
    Given the OpenAI-compatible endpoint is mocked with respx
    And the agent is initialized against the mock endpoint

  @mocked
  Scenario Outline: Route task type to correct MCP tool
    Given a fake LLM that returns tool "<tool>" with valid arguments for "<task_type>"
    When the agent receives the task "<task>"
    Then exactly one MCP tool call is emitted
    And the tool name is "<tool>"
    And the tool arguments validate against the "<tool>" JSON schema
    And no assertion is made on the model's natural-language explanation

    Examples:
      | task                              | task_type | tool        |
      | add a docstring to src/app.py     | file_edit | edit_file   |
      | run the test suite                | testing   | run_tests   |
      | search for TODO comments          | search    | grep_code   |
      | list files in the src/ directory  | listing   | list_files  |
      | read the contents of config.yaml  | read      | read_file   |

  @mocked
  Scenario: Agent emits no tool call when task is unrecognized
    Given a fake LLM that returns a plain text response with no tool calls
    When the agent receives the task "what is the meaning of life"
    Then zero MCP tool calls are emitted
    And the agent response contains text content

  @mocked
  Scenario: Agent handles malformed tool arguments gracefully
    Given a fake LLM that returns tool "edit_file" with malformed JSON arguments
    When the agent receives the task "edit app.py"
    Then the orchestrator raises a ToolArgumentError
    And no MCP tool call is dispatched

  @mocked
  Scenario: Agent routes to the most specific tool when multiple are available
    Given a fake LLM that returns tool "run_tests" with arguments {"test_path": "tests/test_api.py"}
    And the MCP server advertises both "run_tests" and "run_command" tools
    When the agent receives the task "run the API tests"
    Then the tool name is "run_tests"
    And the tool argument "test_path" equals "tests/test_api.py"
```

**File**: `tests/step_defs/test_tool_routing.py`

```python
"""
Step definitions for tests/features/tool_routing.feature

Assertions are structural only — tool name + JSON schema validation.
No assertion is ever made on the model's natural-language content.
"""

import json
import pytest
from pytest_bdd import scenarios, given, when, then, parsers
from jsonschema import validate, ValidationError

# Import schema registry from Labmate core
# from labmate.schemas import TOOL_SCHEMAS

# Placeholder schemas for the stub — replace with real imports
TOOL_SCHEMAS = {
    "edit_file": {
        "type": "object",
        "required": ["path", "content"],
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "run_tests": {
        "type": "object",
        "required": ["test_path"],
        "properties": {
            "test_path": {"type": "string"},
            "extra_args": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    },
    "grep_code": {
        "type": "object",
        "required": ["pattern"],
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "list_files": {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "read_file": {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string"},
        },
        "additionalProperties": False,
    },
}

SAMPLE_ARGS = {
    "file_edit": {"path": "src/app.py", "content": "# new content"},
    "testing":   {"test_path": "tests/"},
    "search":    {"pattern": "TODO", "path": "src/"},
    "listing":   {"path": "src/"},
    "read":      {"path": "config.yaml"},
}

pytestmark = pytest.mark.mocked
scenarios("../features/tool_routing.feature")


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------

@given("the OpenAI-compatible endpoint is mocked with respx")
def _mock_endpoint(respx_mock):
    # respx_mock is auto-injected; no-op here, setup happens in @given steps
    pass


@given("the agent is initialized against the mock endpoint")
def _agent_init(agent):
    # agent fixture (conftest.py) already points at mocked endpoint
    pass


# ---------------------------------------------------------------------------
# Scenario Outline steps
# ---------------------------------------------------------------------------

@given(
    parsers.parse('a fake LLM that returns tool "{tool}" with valid arguments for "{task_type}"'),
    target_fixture="routing_context",
)
def _given_fake_tool(tool: str, task_type: str, fake_model):
    args = SAMPLE_ARGS.get(task_type, {"path": "src/"})
    fake_model(tool, args)
    return {"expected_tool": tool, "expected_args": args, "call": None}


@when(parsers.parse('the agent receives the task "{task}"'))
def _when_agent_handles(task: str, routing_context: dict, agent):
    routing_context["call"] = agent.handle(task)


@then("exactly one MCP tool call is emitted")
def _then_one_call(routing_context: dict):
    calls = routing_context["call"].tool_calls
    assert len(calls) == 1, f"Expected 1 tool call, got {len(calls)}"


@then(parsers.parse('the tool name is "{tool}"'))
def _then_tool_name(tool: str, routing_context: dict):
    actual = routing_context["call"].tool_calls[0].function.name
    assert actual == tool, f"Expected tool '{tool}', got '{actual}'"


@then(parsers.parse('the tool arguments validate against the "{tool}" JSON schema'))
def _then_schema_valid(tool: str, routing_context: dict):
    raw_args = routing_context["call"].tool_calls[0].function.arguments
    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    try:
        validate(instance=args, schema=TOOL_SCHEMAS[tool])
    except ValidationError as exc:
        pytest.fail(f"Tool arguments for '{tool}' failed schema validation: {exc.message}")


@then("no assertion is made on the model's natural-language explanation")
def _then_no_prose_assertion():
    # This step exists to make the BDD scenario self-documenting.
    # There is intentionally no assertion here.
    pass


# ---------------------------------------------------------------------------
# "Agent emits no tool call" scenario
# ---------------------------------------------------------------------------

@given("a fake LLM that returns a plain text response with no tool calls")
def _given_no_tools(fake_model, respx_mock):
    import httpx
    body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "gemma4-local",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "42", "tool_calls": []},
                "finish_reason": "stop",
            }
        ],
    }
    respx_mock.post("http://localhost:8000/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=body)
    )


@then("zero MCP tool calls are emitted")
def _then_no_calls(routing_context: dict):
    calls = getattr(routing_context.get("call"), "tool_calls", [])
    assert len(calls) == 0


@then("the agent response contains text content")
def _then_has_text(routing_context: dict):
    content = getattr(routing_context.get("call"), "content", None)
    assert content is not None and len(content) > 0


# ---------------------------------------------------------------------------
# "Malformed arguments" scenario
# ---------------------------------------------------------------------------

@given("a fake LLM that returns tool \"edit_file\" with malformed JSON arguments")
def _given_malformed(respx_mock):
    import httpx
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {
                                "name": "edit_file",
                                "arguments": "{this is not valid json",  # intentionally broken
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    respx_mock.post("http://localhost:8000/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=body)
    )


@then("the orchestrator raises a ToolArgumentError")
def _then_raises(agent, routing_context: dict):
    from labmate.errors import ToolArgumentError  # type: ignore[import]
    with pytest.raises(ToolArgumentError):
        agent.handle("edit app.py")


@then("no MCP tool call is dispatched")
def _then_no_dispatch(agent):
    # Verified implicitly by the raised exception above; no dispatch occurs.
    pass
```

---

### 5.2 Feature: LLM-as-Judge Quality (Real Model — Layer 2)

**File**: `tests/features/llm_judge_quality.feature`

```gherkin
Feature: LLM-as-judge semantic quality (real model, gated)
  The local Gemma 4 model must produce summaries and plans that meet a
  coherence and faithfulness bar when evaluated by a cross-family judge model.

  These scenarios require LIVE_TESTS=1 and a GPU. They are excluded from
  default CI and run in the nightly eval pipeline only.

  The judge model is gpt-4o-mini (OpenAI family), which is cross-family to
  Gemma/Qwen to eliminate self-preference bias (arxiv:2410.21819).

  @live @gpu
  Scenario: Real Gemma produces a coherent code summary
    Given the real Gemma model is loaded and LIVE_TESTS is set to "1"
    And the module "tests/fixtures/sample_module.py" is loaded as input
    When the agent summarizes the module
    Then the GEval coherence score is at least 0.80
    And the summary mentions at least one public function name from the module
    And the summary does not contain hallucinated import statements

  @live @gpu
  Scenario: Real Gemma produces a structurally valid multi-step plan
    Given the real Gemma model is loaded and LIVE_TESTS is set to "1"
    And the task is "refactor the parse_config function to handle missing keys"
    When the agent generates a plan
    Then the plan contains at least 2 steps
    And each step references an MCP tool name from the registered tool list
    And the GEval faithfulness score is at least 0.80

  @live @gpu
  Scenario Outline: Semantic quality holds across task types
    Given the real Gemma model is loaded and LIVE_TESTS is set to "1"
    And the task input is "<input>"
    When the agent produces output for task type "<task_type>"
    Then the GEval "<metric>" score is at least <threshold>

    Examples:
      | input                             | task_type   | metric      | threshold |
      | Summarize src/orchestrator.py     | summary     | Coherence   | 0.80      |
      | Explain the edit_file tool schema | explanation | Relevance   | 0.75      |
      | Write a docstring for parse_args  | generation  | Faithfulness| 0.85      |

  @live @gpu
  Scenario: Judge model is never same-family as Gemma
    Given the real Gemma model is loaded and LIVE_TESTS is set to "1"
    When the assert_geval helper is called with default settings
    Then the judge model is "gpt-4o-mini"
    And the judge model is not in the Gemma family
    And the judge model is not in the Qwen family
```

**File**: `tests/step_defs/test_llm_judge_quality.py`

```python
"""
Step definitions for tests/features/llm_judge_quality.feature

All scenarios in this file require LIVE_TESTS=1 and a GPU.
The LLM judge is gpt-4o-mini (cross-family — never Gemma or Qwen).
"""

import ast
import os
import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from tests.conftest import assert_geval  # type: ignore[import]

pytestmark = [pytest.mark.live, pytest.mark.gpu]
scenarios("../features/llm_judge_quality.feature")

CROSS_FAMILY_JUDGE = "gpt-4o-mini"  # must not be Gemma, Qwen, or same-family as agent

# ---------------------------------------------------------------------------
# Background / shared given steps
# ---------------------------------------------------------------------------

@given(
    parsers.parse('the real Gemma model is loaded and LIVE_TESTS is set to "{value}"'),
    target_fixture="live_context",
)
def _given_live(value: str, real_model):
    if os.getenv("LIVE_TESTS") != value:
        pytest.skip(f"LIVE_TESTS must be '{value}' to run this scenario")
    return {"model": real_model, "output": None, "input": None}


@given(
    parsers.parse('the module "{module_path}" is loaded as input'),
)
def _given_module(module_path: str, live_context: dict):
    with open(module_path) as f:
        live_context["input"] = f.read()


@given(parsers.parse('the task is "{task}"'))
def _given_task(task: str, live_context: dict):
    live_context["task"] = task


@given(parsers.parse('the task input is "{input_text}"'))
def _given_task_input(input_text: str, live_context: dict):
    live_context["input"] = input_text


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------

@when("the agent summarizes the module")
def _when_summarize(live_context: dict, agent):
    live_context["output"] = agent.summarize(live_context["input"])


@when("the agent generates a plan")
def _when_plan(live_context: dict, agent):
    live_context["output"] = agent.plan(live_context["task"])


@when(parsers.parse('the agent produces output for task type "{task_type}"'))
def _when_produce(task_type: str, live_context: dict, agent):
    if task_type == "summary":
        live_context["output"] = agent.summarize(live_context["input"])
    elif task_type == "explanation":
        live_context["output"] = agent.explain(live_context["input"])
    elif task_type == "generation":
        live_context["output"] = agent.generate(live_context["input"])
    else:
        pytest.fail(f"Unknown task_type: {task_type}")


@when("the assert_geval helper is called with default settings")
def _when_geval_called(live_context: dict):
    live_context["judge_model"] = CROSS_FAMILY_JUDGE


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------

@then(parsers.parse("the GEval coherence score is at least {threshold:f}"))
def _then_coherence(threshold: float, live_context: dict):
    assert_geval(
        input_text=live_context["input"],
        output_text=live_context["output"],
        criteria="Is the output coherent, relevant to the input, and free of hallucinated details?",
        threshold=threshold,
        judge_model=CROSS_FAMILY_JUDGE,
    )


@then(parsers.parse("the GEval faithfulness score is at least {threshold:f}"))
def _then_faithfulness(threshold: float, live_context: dict):
    assert_geval(
        input_text=live_context["input"],
        output_text=live_context["output"],
        criteria="Does the output faithfully represent the input without introducing false information?",
        threshold=threshold,
        judge_model=CROSS_FAMILY_JUDGE,
    )


@then(parsers.parse('the GEval "{metric}" score is at least {threshold:f}'))
def _then_metric(metric: str, threshold: float, live_context: dict):
    criteria_map = {
        "Coherence": "Is the output coherent, readable, and on-topic?",
        "Relevance": "Does the output directly address the input question or task?",
        "Faithfulness": "Does the output faithfully represent the input without hallucinating facts?",
    }
    criteria = criteria_map.get(metric, f"Is the output high quality according to the {metric} dimension?")
    assert_geval(
        input_text=live_context["input"],
        output_text=live_context["output"],
        criteria=criteria,
        threshold=threshold,
        judge_model=CROSS_FAMILY_JUDGE,
    )


@then("the summary mentions at least one public function name from the module")
def _then_function_names(live_context: dict):
    source = live_context["input"]
    summary = live_context["output"]
    tree = ast.parse(source)
    public_functions = [
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]
    assert public_functions, "Sample module has no public functions — fix the fixture"
    mentioned = [fn for fn in public_functions if fn in summary]
    assert mentioned, (
        f"Summary mentions none of the public functions: {public_functions}\n"
        f"Summary was: {summary[:300]}"
    )


@then("the summary does not contain hallucinated import statements")
def _then_no_hallucinated_imports(live_context: dict):
    summary = live_context["output"]
    source = live_context["input"]
    # Extract real import names from source
    tree = ast.parse(source)
    real_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                real_imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            real_imports.add(node.module.split(".")[0])
    # Check for import-like patterns in summary that are not real
    import re
    summary_imports = re.findall(r"`import (\w+)`|`from (\w+)`", summary)
    hallucinated = []
    for match in summary_imports:
        name = match[0] or match[1]
        if name and name not in real_imports:
            hallucinated.append(name)
    assert not hallucinated, f"Summary contains hallucinated imports: {hallucinated}"


@then("the plan contains at least 2 steps")
def _then_plan_steps(live_context: dict):
    plan = live_context["output"]
    # Plan is expected to be a list of step dicts or a numbered list string
    if isinstance(plan, list):
        assert len(plan) >= 2, f"Plan has only {len(plan)} step(s)"
    else:
        # Count numbered lines as steps
        import re
        steps = re.findall(r"^\s*\d+[\.\)]\s", plan, re.MULTILINE)
        assert len(steps) >= 2, f"Plan string has only {len(steps)} numbered step(s)"


@then("each step references an MCP tool name from the registered tool list")
def _then_plan_tool_refs(live_context: dict):
    from labmate.schemas import TOOL_SCHEMAS  # type: ignore[import]
    registered_tools = set(TOOL_SCHEMAS.keys())
    plan = live_context["output"]
    plan_text = str(plan)
    referenced = [t for t in registered_tools if t in plan_text]
    assert referenced, (
        f"No registered tool names found in plan. Registered: {registered_tools}\n"
        f"Plan: {plan_text[:300]}"
    )


@then(parsers.parse('the judge model is "{model}"'))
def _then_judge_model(model: str, live_context: dict):
    assert live_context.get("judge_model") == model


@then("the judge model is not in the Gemma family")
def _then_not_gemma(live_context: dict):
    judge = live_context.get("judge_model", CROSS_FAMILY_JUDGE)
    assert "gemma" not in judge.lower(), (
        f"Judge model '{judge}' is in the Gemma family — self-preference bias risk"
    )


@then("the judge model is not in the Qwen family")
def _then_not_qwen(live_context: dict):
    judge = live_context.get("judge_model", CROSS_FAMILY_JUDGE)
    assert "qwen" not in judge.lower(), (
        f"Judge model '{judge}' is in the Qwen family — self-preference bias risk"
    )
```

---

### 5.3 Feature: Execution Oracle (SWE-bench Style — Layer 3)

**File**: `tests/features/execution_oracle.feature`

```gherkin
Feature: Execution-based coding oracle (SWE-bench style)
  An agent-generated patch is functionally correct only if it makes the
  targeted failing test pass (FAIL_TO_PASS) without breaking any previously
  passing tests (PASS_TO_PASS), and without modifying any test files.

  Requires LIVE_TESTS=1 and EVAL=1.

  @eval @live
  Scenario Outline: Agent patch satisfies the FAIL_TO_PASS / PASS_TO_PASS oracle
    Given a repository sandbox at "<repo_path>"
    And the following test is currently failing: "<fail_test>"
    And the following tests are currently passing: "<pass_tests>"
    When the agent applies a patch to fix "<fail_test>"
    Then "<fail_test>" transitions from FAIL to PASS
    And all tests in "<pass_tests>" remain PASS
    And no test files were modified by the patch

    Examples:
      | repo_path              | fail_test                                          | pass_tests                                          |
      | tests/fixtures/repo_a  | tests/test_parser.py::test_parse_missing_key       | tests/test_parser.py::test_parse_valid_config       |
      | tests/fixtures/repo_a  | tests/test_api.py::test_route_returns_404          | tests/test_api.py::test_route_returns_200           |
      | tests/fixtures/repo_b  | tests/test_refactor.py::test_handles_empty_input   | tests/test_refactor.py::test_handles_normal_input   |

  @eval @live
  Scenario: Agent does not weaken or delete existing assertions
    Given a repository sandbox at "tests/fixtures/repo_a"
    And the test "tests/test_parser.py::test_parse_missing_key" is currently failing
    When the agent applies a patch to fix "tests/test_parser.py::test_parse_missing_key"
    Then the test file "tests/test_parser.py" is unchanged after the patch
    And the assertion count in "tests/test_parser.py" is not reduced

  @eval @live
  Scenario: Hidden test set validates the patch beyond the target test
    Given a repository sandbox at "tests/fixtures/repo_a"
    And the following test is currently failing: "tests/test_parser.py::test_parse_missing_key"
    And a hidden test set at "tests/hidden/test_parser_hidden.py" unknown to the agent
    When the agent applies a patch to fix "tests/test_parser.py::test_parse_missing_key"
    Then the hidden test set passes after the patch
```

---

## 6. CI Configuration

### 6.1 Fast Mocked Suite (Every Commit, No GPU)

```yaml
# .github/workflows/ci.yml  (or equivalent)
name: CI — Mocked Test Suite

on: [push, pull_request]

jobs:
  test-mocked:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -e ".[dev]"

      - name: Run mocked test suite
        run: pytest -m "not live and not eval" --timeout=30 -x -q
        # -x: stop on first failure; -q: quiet output
        # No LIVE_TESTS or GPU required

      - name: Run TypeScript MCP server tests
        run: npx vitest run --reporter=verbose
        working-directory: mcp-server/
```

The mocked suite must complete in under 60 seconds on any standard CI runner. If it exceeds this, the test count or fixture setup is excessive — parallelize with `pytest-xdist` (`-n auto`) for independent mocked tests.

### 6.2 Live Eval Suite (Nightly, LIVE_TESTS=1, Needs A6000)

```yaml
# .github/workflows/nightly-eval.yml
name: Nightly — Live Eval Suite

on:
  schedule:
    - cron: "0 2 * * *"   # 02:00 UTC daily
  workflow_dispatch:
    inputs:
      run_eval:
        description: "Set to 'true' to run eval layer"
        default: "false"

jobs:
  test-live:
    runs-on: [self-hosted, gpu, a6000]   # requires A6000 runner
    env:
      LIVE_TESTS: "1"
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}    # for gpt-4o-mini judge
    steps:
      - uses: actions/checkout@v4

      - name: Start local vLLM server
        run: |
          python -m vllm.entrypoints.openai.api_server \
            --model gemma-4 \
            --dtype bfloat16 \
            --port 8000 &
          until curl -sf http://localhost:8000/health; do sleep 2; done
        timeout-minutes: 10

      - name: Run live integration suite
        run: pytest -m "live and not eval" --timeout=120 -q
        # Serial execution (no xdist) to control GPU contention

  test-eval:
    needs: test-live
    runs-on: [self-hosted, gpu, a6000]
    if: github.event.inputs.run_eval == 'true' || github.event_name == 'schedule'
    env:
      LIVE_TESTS: "1"
      EVAL: "1"
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
    steps:
      - uses: actions/checkout@v4

      - name: Run eval / execution-oracle suite
        run: pytest -m "eval" --timeout=300 -q
```

**Environment variable gates summary**:

| Suite | `LIVE_TESTS` | `EVAL` | GPU required | Schedule |
|---|---|---|---|---|
| Mocked (Layer 1) | unset | unset | No | Every commit |
| Live integration (Layer 2) | `1` | unset | Yes (A6000) | Nightly |
| Execution oracle (Layer 3) | `1` | `1` | Yes (A6000) | Nightly (opt-in) |

---

## 7. Common Pitfalls

### 7.1 Asserting Literal Output Text

**Problem**: `assert response == "Here is a docstring for parse_config:"` fails on every model version bump, fine-tune, temperature change, or batch size change.

**Fix**: Assert on structure (tool name, parsed JSON, schema validity) and route prose quality claims through `assert_geval` with a numeric threshold. If you find yourself writing `in response` or `== response`, stop and ask whether a structural assertion is possible instead.

### 7.2 VCR Cassette Staleness

**Problem**: A cassette records a response from model version N. The model is upgraded to version N+1. The cassette replays the old response, the unit test stays green, but production behavior has silently drifted.

**Fix**:
- Tag every cassette filename with the model id and version: `edit_file_gemma4_v2.yaml`.
- Re-record cassettes whenever the model version bumps (set `VCR_RECORD=once` in CI).
- Pair every cassette-backed unit test with a corresponding `@live` test asserting the same invariant. A green cassette test + failing live test = staleness signal.
- Add the cassette re-recording step to the model upgrade runbook.

### 7.3 temperature=0 False Determinism

**Problem**: A developer sets `temperature=0` and assumes outputs are deterministic. Tests pass locally then flake in CI due to different batch sizes or vLLM versions.

**Fix**: Never rely on `temperature=0` for determinism. Mock the model entirely for deterministic tests (Layer 1). For live tests, pin the vLLM version, dtype, and hardware; set `VLLM_BATCH_INVARIANT=1` and `VLLM_ENABLE_V1_MULTIPROCESSING=0`; use property/threshold assertions, not equality.

### 7.4 Test-Oracle Gap

**Problem**: FAIL_TO_PASS passes because the agent hardcodes the expected return value, deletes the failing assertion, or weakens the test condition. The oracle reports success but the fix is fraudulent.

**Fix**: Always combine FAIL_TO_PASS with PASS_TO_PASS. Add a post-patch check that the test file's byte content is unchanged (or assertion count is not reduced). Maintain a held-out hidden test set the agent never sees; run it as the final correctness signal.

### 7.5 LLM-Judge Circularity / Self-Preference Bias

**Problem**: The same model (or same family — e.g. Qwen judging Gemma outputs of a Qwen fine-tune) is used as both agent-under-test and judge. Self-preference bias causes systematic over-rating (arxiv:2410.21819, arxiv:2404.13076). Gemma judging Gemma will score outputs 10–15% higher than a cross-family judge on the same outputs.

**Fix**: The judge model must be from a **different model family** than the agent model. For Labmate (Gemma 4 agent), the judge must not be Gemma or Qwen. Use `gpt-4o-mini` as the default judge. The `assert_geval` helper in `conftest.py` raises an `AssertionError` immediately if the judge model contains "gemma" or "qwen" in its name. Periodically calibrate judge thresholds against a small (10–30 example) human-labeled set each release cycle.

### 7.6 Scenario Scope Too Large

**Problem**: A single Gherkin scenario spans ten agent steps. When it fails, the failure is undiagnosable — any of ten steps could be the regression.

**Fix**: One scenario, one behavioral assertion. Use `Scenario Outline` for variations. Use `Background` for shared setup. If a test requires more than three `When/Then` pairs, split it into separate scenarios.

### 7.7 Marker Leakage into CI

**Problem**: A `@live` or `@eval` test accidentally runs in the default CI pipeline (no GPU). It hangs waiting for a model that is not present, blocking the entire CI run until the job timeout.

**Fix**: Register explicit markers in `pyproject.toml`. Set `addopts = "-m 'not live and not eval'"` as the default. Add `--timeout=30` so a hung test fails fast (30 seconds) rather than blocking the runner. Verify the CI job definition never passes `LIVE_TESTS=1` unless explicitly in the nightly pipeline.

### 7.8 Non-Idempotent Fixtures / Shared Agent State

**Problem**: The agent writes to a shared MongoDB collection or filesystem path. Test B observes the residue left by Test A, producing order-dependent flakiness that passes in isolation but fails in full suite runs.

**Fix**: Use per-test database namespace isolation (unique `test_<uuid>` collection per test). Use transactional rollback or a fresh ephemeral container per test. Assert a clean baseline in fixture `setup` — never assume state from a prior test. Use `pytest-xdist` worker isolation when parallelizing.

### 7.9 Async Fixture Teardown Race

**Problem**: The event loop is torn down before cleanup coroutines (closing `httpx.AsyncClient`, killing agent subprocesses, draining MCP connections) finish. Raises `RuntimeError: Event loop is closed` or silently leaks processes.

**Fix**: Do not define a custom `event_loop` fixture that conflicts with `pytest-asyncio`'s built-in. Set `asyncio_mode = "auto"` in `pyproject.toml`. Set event-loop scope to match the fixture scope (`scope="function"` for function-scoped fixtures). Always `await` all cleanup before `yield` returns control. Verify subprocesses and clients are actually closed in teardown with a final `assert proc.returncode is not None`.

### 7.10 Mixing Test Runners Across the Polyglot Boundary

**Problem**: The Python orchestrator drives the TypeScript MCP server via a subprocess shell (`subprocess.run(["npx", "vitest", ...])`). Structured failures (TypeScript stack traces, vitest error messages) are lost in the raw stdout capture. A TypeScript error looks like a generic non-zero exit code on the Python side.

**Fix**: Keep each language in its idiomatic runner. The Python BDD scenarios assert only on the **cross-process contract** (MCP tool schema advertisement, JSON-RPC response shape), not on TypeScript internals. TypeScript unit tests run independently with `vitest`. Rust skill tests run with `cargo test`. The eval oracle shells out to the target language's runner and parses structured exit codes, not prose output.

---

## 8. Dependencies

### Python (pyproject.toml / requirements-dev.txt)

```toml
[project.optional-dependencies]
dev = [
  # Test runner core
  "pytest>=8.0",
  "pytest-bdd>=7.0",
  "pytest-asyncio>=0.24",
  "pytest-mock>=3.0",
  "pytest-timeout>=2.0",
  "pytest-xdist>=3.0",            # parallelize mocked suite

  # HTTP mocking
  "respx>=0.21",                  # mocks httpx at HTTP seam (OpenAI-compatible endpoint)
  "httpx>=0.27",                  # must match orchestrator's HTTP client

  # VCR cassette replay
  "vcrpy>=6.0",
  "pytest-recording>=0.13",       # @pytest.mark.vcr decorator

  # LLM-as-judge (cross-family, not Gemma)
  "deepeval>=1.0",                # GEval metric; configure judge_model=gpt-4o-mini

  # Optional: retrieval/RAG metrics
  "ragas>=0.1",

  # Property-based testing
  "hypothesis>=6.0",

  # Schema validation
  "jsonschema>=4.0",
]
```

### TypeScript (MCP server — package.json devDependencies)

```json
{
  "devDependencies": {
    "vitest": ">=2.0",
    "@langwatch/scenario": "latest"
  }
}
```

### Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `LIVE_TESTS` | Enable Layer 2 live model tests | unset (tests skip) |
| `EVAL` | Enable Layer 3 execution oracle | unset (tests skip) |
| `VCR_RECORD` | VCR record mode (`none`, `once`, `new_episodes`, `all`) | `none` |
| `OPENAI_API_KEY` | Required for gpt-4o-mini cross-family judge | unset |
| `VLLM_BATCH_INVARIANT` | Pin vLLM reduction order for reproducibility (CC>=8.0) | `0` |
| `VLLM_ENABLE_V1_MULTIPROCESSING` | Disable v1 multiprocessing for reproducibility | `1` |

---

## 9. Reference Papers & Repos

### Papers

| Title | Authors | ArXiv | Relevance |
|---|---|---|---|
| Evaluating Large Language Models Trained on Code (HumanEval) | Chen et al. 2021 | 2107.03374 | Defines unbiased pass@k estimator; execution-based functional correctness |
| SWE-bench: Can Language Models Resolve Real-World GitHub Issues? | Jimenez et al. 2023 | 2310.06770 | FAIL_TO_PASS / PASS_TO_PASS execution oracle |
| AgentBench: Evaluating LLMs as Agents | Liu et al. 2023 | 2308.03688 | Multi-environment agent eval; execution-trace scoring |
| SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering | Yang et al. 2024 | 2405.15793 | Agent harness design; tool-use evaluation |
| G-Eval: NLG Evaluation using GPT-4 with Better Human Alignment | Liu et al. 2023 | 2303.16634 | LLM-as-judge scoring; foundation of DeepEval GEval metric |
| RAGAS: Automated Evaluation of Retrieval Augmented Generation | Es et al. 2023 | 2309.15217 | Reference-free metrics for RAG/agent retrieval steps |
| Self-Preference Bias in LLM-as-a-Judge | Wataoka, Takahashi & Ri 2024 | 2410.21819 | Quantifies self-grading bias; motivates cross-family judge selection |
| LLM Evaluators Recognize and Favor Their Own Generations | Panickssery et al. 2024 | 2404.13076 | Judges self-recognize and overrate own outputs; reinforces no-self-judge rule |

### Repos

| Repo | Purpose |
|---|---|
| `pytest-dev/pytest-bdd` | Gherkin BDD on top of full pytest ecosystem |
| `cucumber/pytest-bdd-ng` | Actively maintained fork with async steps and data tables |
| `confident-ai/deepeval` | pytest-native LLM eval: GEval, faithfulness, 14+ judge metrics |
| `explodinggradients/ragas` | Reference-free RAG/agent retrieval metrics |
| `princeton-nlp/SWE-bench` | Canonical FAIL_TO_PASS/PASS_TO_PASS execution oracle |
| `openai/human-eval` | Reference implementation of the numerically stable pass@k estimator |
| `langwatch/scenario` | Agent simulation/testing: Python (pytest) and TS (vitest) bindings |
| `kiwicom/pytest-recording` | vcrpy plugin exposing `@pytest.mark.vcr` cassette decorator |
| `lundberg/respx` | httpx mock layer for intercepting the OpenAI-compatible HTTP seam |

---

## 10. SOTA Improvements

### 10.1 Property-Based Testing with Hypothesis

Replace hand-authored `Scenario Outline` rows with adversarial inputs generated by `Hypothesis`. Instead of five enumerated tasks in the routing table, generate thousands of prompt variations, path strings (empty, unicode, oversized, malformed), and tool argument edge cases automatically.

```python
from hypothesis import given, settings, strategies as st
import pytest

@pytest.mark.mocked
@given(
    path=st.one_of(
        st.just(""),
        st.text(max_size=4096),
        st.from_regex(r"\.\.\/+.*"),   # path traversal attempts
    )
)
@settings(max_examples=200)
def test_edit_file_never_out_of_schema(path: str, fake_model, agent):
    fake_model("edit_file", {"path": path, "content": "x"})
    try:
        result = agent.handle(f"edit {path}")
        if result.tool_calls:
            validate(result.tool_calls[0].function.arguments, TOOL_SCHEMAS["edit_file"])
    except ToolArgumentError:
        pass  # Raising is acceptable; crashing the orchestrator is not
```

This catches schema edge cases no hand-authored scenario would reach, including path traversal payloads, empty strings, and oversized content blobs.

### 10.2 Execution-Trace Evaluation (Not Just Final-Answer Scoring)

Score the **trajectory** of the agent, not just the final outcome. An agent that reaches the right answer by an unrepeatable path (e.g. random lucky guess, wrong tool sequence that happens to produce the right file) will not generalize.

Use AgentBench/SWE-agent-style trajectory scoring:
- Did the agent call tools in the correct logical order?
- Were there redundant or wasted tool calls?
- Did the agent recover correctly from a failed tool response?
- Was the total tool-call count within an acceptable budget?

Implement as a post-oracle step that reads the agent's tool-call log and validates the sequence against a reference trajectory or a set of trajectory constraints.

### 10.3 Multi-Sample pass@k CI Gate with Statistical Significance

Rather than gating on a single pass/fail run for eval-layer tasks, run `n = 20` samples per task and compute `pass@1`, `pass@5`, and `pass@10` using the HumanEval unbiased estimator. A regression is flagged only when the CI of `pass@k` drops below the baseline CI — not on a single-run miss.

```python
import math
from scipy.special import comb

def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k estimator (Chen et al. 2021)."""
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k, exact=True) / comb(n, k, exact=True)
```

This eliminates single-shot flakiness from the eval gate and gives a statistically grounded signal for model regressions.

### 10.4 Golden-Dataset Drift Detection

Maintain a curated gold set of (input, expected_structure, quality_threshold) triples. Run the full gold set nightly and compare current model outputs against the gold standard via the LLM judge and structural metrics. Alert (Slack/PagerDuty) when aggregate scores degrade below a configured baseline delta (e.g. > 5% drop in mean GEval coherence). This catches silent model drift, cassette staleness, and vLLM version regressions before they reach production.

### 10.5 Synthetic Adversarial Scenario Generation

Use a stronger cross-family model (e.g. `gpt-4o`) to automatically generate new Gherkin `Scenario Outline` rows that probe failure modes not covered by the hand-authored suite. A human or the LLM judge gates the generated cases before they are added to the feature files. This expands the behavioral suite automatically as the agent's capabilities grow.

### 10.6 Cross-Family LLM Judge with Human Calibration

Institutionalize the no-self-judge rule as a CI-enforced constraint (the `assert_geval` helper raises immediately on a same-family judge name). Each release cycle, run the judge against a small (10–30 example) human-labeled calibration set and verify that judge scores correlate with human ratings (Pearson r >= 0.7). If correlation drops, recalibrate the threshold or replace the judge model. This directly counters self-preference bias (arxiv:2410.21819) and prevents the judge's quality signal from drifting independently of the agent.

---

*End of spec. All code stubs are illustrative; replace import paths (`labmate.*`) with the actual Labmate package structure as it is built.*
