# Hosted MCP-Tool Routing Evaluation

This document explains how to run the hosted-tool auto-selection scoring extension to `run_routing_eval.py` and `extend_eval.py`.

## What It Does

The hosted-tool routing eval scores whether the model **auto-selects the right hosted MCP tool** (e.g., `mcp__ast-ts-refactor__find_references`) when presented with a natural-language task and a set of flat tool schemas—without any mention of skill names or tool names in the prompt.

Complementary to the pod-skill routing eval (which scores `load_skill` enum selection), this scores:
- Direct flat-tool auto-selection via `tool_choice="auto"`
- Model understanding of tool descriptions to pick the best match
- No leakage of tool names into task phrasings

## Case Format

Hosted-tool eval cases are JSONL entries with a `kind: "hosted"` discriminator:

```json
{"id": "hosted_find_1", "task": "Where is parseConfig used?", "expected": "mcp__ast-ts-refactor__find_references", "kind": "hosted", "cluster": "ts_refactor"}
{"id": "hosted_rename_1", "task": "Rename oldFunc to newFunc everywhere", "expected": "mcp__ast-ts-refactor__rename_symbol", "kind": "hosted", "cluster": "ts_refactor"}
```

- `task`: Natural-language task (must not mention the tool name)
- `expected`: The full `mcp__<server>__<tool>` name
- `kind`: `"hosted"` (vs. `"skill"` for pod skills)
- `acceptable` (optional): Array of alternative tool names that count as correct

For backward compatibility, cases with no `kind` field default to `kind: "skill"`.

## Running the Hosted-Tool Eval

### 1. Prepare the Hosted-Tools Schema File

Create a JSON file with OpenAI-compatible tool schemas:

```json
[
  {
    "type": "function",
    "function": {
      "name": "mcp__ast-ts-refactor__find_references",
      "description": "Find all places where a symbol is referenced in TypeScript code",
      "parameters": {...}
    }
  },
  {
    "type": "function",
    "function": {
      "name": "mcp__ast-ts-refactor__rename_symbol",
      "description": "Rename a symbol across the project",
      "parameters": {...}
    }
  }
]
```

Example: `eval/fixtures/hosted_tools.example.json` (committed to the repo).

### 2. Prepare the Eval Cases

Use `extend_eval.py` to generate hosted-tool cases, or write them manually in JSONL format.

**Generate cases** (requires running `infrastructure/local/start.sh` first):

```bash
python eval/extend_eval.py \
  --eval eval/routing_eval.jsonl \
  --hosted-tools eval/fixtures/hosted_tools.example.json \
  --per-tool 6 \
  --base-url http://localhost:8000/v1 \
  --model gemma-4-31b
```

Or use the **deterministic fallback** (no model needed):

```bash
python eval/extend_eval.py \
  --eval eval/routing_eval.jsonl \
  --hosted-tools eval/fixtures/hosted_tools.example.json \
  --per-tool 3 \
  --no-llm
```

This appends new hosted cases to your eval file. It skips tools already covered and avoids duplicate tasks.

### 3. Score the Hosted Cases

Run `run_routing_eval.py` with the `--hosted-tools` flag:

```bash
python eval/run_routing_eval.py \
  --eval eval/routing_eval.jsonl \
  --hosted-tools eval/fixtures/hosted_tools.example.json \
  --skills-dir services/skills \
  --base-url http://localhost:8000/v1 \
  --model gemma-4-31b \
  --repeats 3 \
  --select-attempts 3 \
  --report eval/reports/
```

**Requirements:**
- `GEMMA_BASE` endpoint running (e.g., `infrastructure/local/serve-model.sh` + `infrastructure/local/start.sh`)
- Eval file with mixed skill + hosted cases (or hosted-only)
- Hosted-tools JSON schema file

**Output:**
- Summary in stdout: per-kind, per-cluster, and per-skill accuracy
- Detailed JSON report in `eval/reports/routing-eval-<timestamp>.json`
- Markdown report in `eval/reports/routing-eval-<timestamp>.md`

The report breaks down accuracy by `kind`:
```
## Per-kind
- skill: 0.850
- hosted: 0.765
```

### 4. Acceptance Criteria

**Target accuracy for new hosted tools: ≥0.80**

- If a tool scores <0.80, improve its `description` in the schema file (clarity, specificity, action verbs)
- No regression on existing pod-skill accuracy (>0.05 drop is a red flag)
- Stability ≥0.7 per case (model agreement across repeats)

## API Integration (Running Live in the Morning)

Once the eval is ready, run it live on the RunPod GPU host:

```bash
ssh user@runpod-host
cd /workspace/Labmate

# Ensure model is loaded
infrastructure/local/serve-model.sh  # wait for "model loaded"

# Start services (in another terminal)
infrastructure/local/start.sh

# Run the hosted eval
python eval/run_routing_eval.py \
  --eval eval/fixtures/hosted_routing.example.jsonl \
  --hosted-tools eval/fixtures/hosted_tools.example.json \
  --base-url http://localhost:8000/v1 \
  --model gemma-4-31b \
  --repeats 3 \
  --report eval/reports/
```

The model will make real `tool_choice="auto"` calls against the hosted-tools schemas and score whether it picks the right tool.

## Internals

### Scoring Seam

The hosted-tool scorer uses an injectable `call_model` parameter (default: real OpenAI-compatible HTTP call). This allows unit tests to stub the model without network access:

```python
async def my_stub_model(client, model, task, hosted_tools, system_prompt, temperature):
    # Return a fake response with a specific tool call
    ...

results = await evaluate(
    cases, client, model, catalog, ...,
    hosted_tools=hosted_tools,
    call_model=my_stub_model  # injected
)
```

### Case-Kind Routing

The `evaluate()` function routes cases based on `kind`:
- `kind == "skill"`: calls `route_one()` (skill enum selector)
- `kind == "hosted"`: calls `route_hosted_one()` (flat-tool selector)
- Missing `kind`: defaults to `"skill"` (backward compatible)

### Mixed Reports

When an eval file contains both skill and hosted cases, the summary includes per-kind breakdowns:

```json
{
  "overall": 0.81,
  "by_kind": {
    "skill": 0.85,
    "hosted": 0.76
  },
  "by_cluster": {...},
  "by_skill": {...}
}
```

## Testing (Deterministic, No Network)

Run unit tests without a model or network:

```bash
cd /Users/zachstallbohm/Work/Labmate
PYTHONPATH=. python -m pytest tests/eval/test_hosted_routing.py -v
```

Coverage:
- Scoring with stubbed model calls (correct/wrong/decline)
- Case format parsing (kind defaults, mixed files)
- Case generation (deterministic templated + LLM-based)
- Per-kind accuracy tracking
- Fixture loading and validation

**16 tests, all passing.**

## Example Files

- **Hosted tools schema**: `eval/fixtures/hosted_tools.example.json` (3 tools: find_references, rename_symbol, extract_function)
- **Example cases**: `eval/fixtures/hosted_routing.example.jsonl` (6 cases across 2 tools)

These are fixtures for development. In production, you'd create tool schemas matching your actual hosted MCP manifest.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `Failed to load hosted tools from ...` | Malformed JSON or file not found | Validate schema with `jq < hosted_tools.json` |
| `skipping hosted case <id>: no hosted_tools loaded` | `--hosted-tools` flag missing when running `run_routing_eval.py` | Add `--hosted-tools <path>` |
| Low accuracy (<0.60) on new tools | Tool descriptions are unclear or too generic | Rewrite descriptions with action verbs and use-case specifics |
| `KeyError: 'tool_calls'` during eval | Model response format mismatch (not OpenAI-compatible) | Verify endpoint is llama.cpp with OpenAI-compat enabled |
| All cases skipped | Eval file has no hosted cases | Check that cases have `kind: "hosted"` and a valid `expected` name |

## References

- **Original routing eval**: `run_routing_eval.py` (skill-only)
- **Case generation**: `extend_eval.py` (additive, non-destructive)
- **Tests**: `tests/eval/test_hosted_routing.py` (16 unit tests, no network)
- **CLAUDE.md**: Project-wide instructions and testing harness reference
