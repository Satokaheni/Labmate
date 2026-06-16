# 06 — Skill: Deep Research

**Labmate Implementation Plan**
Skill: `deep-research` — Three-phase autonomous research pipeline

---

## 1. What This Skill Does

The `deep-research` skill gives the orchestrator a structured, multi-phase research pipeline invocable as three sequential MCP tools. The orchestrator calls them in order; each phase produces an artifact consumed by the next.

**Phase 1 — Outline (`research_outline`)**
Calls the inference server (Contract A, `INFERENCE_URL`) with the research topic and any field hints the caller provides. Returns a YAML file at `outline_path` listing subtopics, each with a `key`, `question`, and optional `fields`. This is the decomposition step: one large topic becomes N focused questions.

**Phase 2 — Deep dive (`research_deep`)**
Reads the YAML outline and fans out one async worker per subtopic. Each worker in `agents/web_search.py` performs a web search (DuckDuckGo or a configured search API via `httpx`), synthesizes the findings with a second LLM call, and writes a structured JSON file to `output_dir/{key}.json`. Workers run with a configurable parallelism limit (semaphore). The orchestrator invokes this tool once; it blocks until all workers finish (or time out).

**Phase 3 — Report (`research_report`)**
Reads every `{key}.json` in `results_dir`, passes the combined structured findings to the LLM for a final synthesis pass, and writes a single markdown report to `output_path`. The report includes an executive summary, per-subtopic sections, source citations, and gaps/limitations.

**What the orchestrator gets back:** A markdown research report at a known path, plus the intermediate YAML outline and per-topic JSON files for audit or re-use.

---

## 2. SKILL.md

```markdown
---
name: deep-research
description: >
  Autonomous multi-phase research pipeline. Use when the user asks to research
  a topic, do a deep dive, investigate a subject, find papers on a question,
  write a literature review, or compile information from the web. Runs an
  outline → parallel web-search agents → compiled markdown report pipeline.
  Returns a structured markdown report with citations and source URLs.
trigger:
  - research
  - deep research
  - deep dive
  - investigate topic
  - find papers on
  - literature review
  - compile information about
  - survey of
  - write a report on
tools:
  - name: research_outline
    description: >
      Phase 1. Generate a structured YAML outline of subtopics to research for
      a given topic. Returns the path to the written outline file.
    inputSchema:
      type: object
      properties:
        topic:
          type: string
          description: The research topic or question to decompose into subtopics.
        output_path:
          type: string
          description: Absolute path where the YAML outline file will be written.
        fields:
          type: array
          items:
            type: string
          description: >
            Optional list of specific aspects or fields to include in the
            outline (e.g. ["history", "current_state", "open_problems"]).
            When omitted the LLM chooses the decomposition.
        num_subtopics:
          type: integer
          description: Target number of subtopics to generate. Defaults to 6.
          default: 6
      required:
        - topic
        - output_path
  - name: research_deep
    description: >
      Phase 2. Read a YAML outline produced by research_outline and fan out
      parallel web-search agents, one per subtopic. Each agent searches the
      web, synthesizes findings, and writes a JSON result file. Returns a
      summary of which subtopics succeeded and failed.
    inputSchema:
      type: object
      properties:
        outline_path:
          type: string
          description: >
            Absolute path to the YAML outline file written by research_outline.
        output_dir:
          type: string
          description: >
            Absolute path to the directory where per-topic JSON results will be
            written. Created if it does not exist.
        parallel:
          type: integer
          description: >
            Maximum number of subtopic agents to run concurrently. Defaults to 3.
            Keep low (2-4) to avoid search API rate limits.
          default: 3
        timeout_per_topic:
          type: integer
          description: >
            Per-topic timeout in seconds. A worker that exceeds this is cancelled
            and its result is marked failed. Defaults to 60.
          default: 60
      required:
        - outline_path
        - output_dir
  - name: research_report
    description: >
      Phase 3. Read all per-topic JSON result files from a results directory,
      call the LLM for a final synthesis pass, and write a markdown report.
      Returns the path to the written report.
    inputSchema:
      type: object
      properties:
        results_dir:
          type: string
          description: >
            Absolute path to the directory containing per-topic JSON result
            files written by research_deep.
        output_path:
          type: string
          description: Absolute path where the final markdown report will be written.
        topic:
          type: string
          description: >
            The original research topic. Used as the report title and context
            for the synthesis LLM call.
      required:
        - results_dir
        - output_path
        - topic
model: any
version: "1.0.0"
license: MIT
---

# Deep Research Skill

You have access to a three-phase autonomous research pipeline. Use it
whenever the task requires gathering, synthesizing, or reporting on
information from external sources.

## When to Use

- User asks to "research X", "find information about X", "do a literature
  review on X", "investigate X", or "write a report on X"
- The topic is too broad for a single web search — it needs decomposition
- The user wants a structured markdown report with citations

## Workflow

Always call the three tools in sequence. Each tool writes a file that the
next tool reads.

### Step 1 — Generate outline

```json
{
  "topic": "quantum error correction",
  "output_path": "/tmp/research/outline.yaml",
  "num_subtopics": 6
}
```

Returns: `{ "outline_path": "/tmp/research/outline.yaml", "subtopics": [...] }`

### Step 2 — Run parallel deep dive

```json
{
  "outline_path": "/tmp/research/outline.yaml",
  "output_dir": "/tmp/research/results",
  "parallel": 3
}
```

Returns: `{ "succeeded": 5, "failed": 1, "results_dir": "/tmp/research/results" }`

### Step 3 — Compile report

```json
{
  "results_dir": "/tmp/research/results",
  "output_path": "/tmp/research/report.md",
  "topic": "quantum error correction"
}
```

Returns: `{ "report_path": "/tmp/research/report.md" }`

## Output

The markdown report has:
- Executive summary (2-3 paragraphs)
- One section per subtopic with synthesis and citations
- A "Gaps and limitations" section
- A "Sources" section with all URLs

## Parallelism

Keep `parallel` at 3 or lower to avoid rate-limit errors from the search
provider. Increase only if using a search API with a high request quota.
```

---

## 3. File Structure

```
services/skills/deep-research/
├── SKILL.md                  — frontmatter + body (Section 2 above)
├── server.py                 — Python MCP server entry point; registers all 3 tools
├── phases/
│   ├── __init__.py
│   ├── outline.py            — Phase 1: call LLM, write YAML outline
│   ├── deep.py               — Phase 2: asyncio.TaskGroup fan-out with semaphore
│   └── report.py             — Phase 3: read JSON results, call LLM, write markdown
├── agents/
│   ├── __init__.py
│   └── web_search.py         — Single-topic agent: search → parse → LLM synthesis → write JSON
├── requirements.txt
└── Dockerfile
```

---

## 4. Interface Contracts

### 4.1 JSON-RPC tool call shapes

All three tools follow Contract B (MCP JSON-RPC 2.0 over stdio). The bridge sends:

```json
{
  "jsonrpc": "2.0",
  "id": "call-uuid-001",
  "method": "tools/call",
  "params": {
    "name": "research_outline",
    "arguments": {
      "topic": "quantum error correction",
      "output_path": "/tmp/research/outline.yaml",
      "num_subtopics": 6
    }
  }
}
```

Success response:

```json
{
  "jsonrpc": "2.0",
  "id": "call-uuid-001",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"outline_path\": \"/tmp/research/outline.yaml\", \"subtopics\": [\"surface codes\", \"fault tolerance thresholds\", ...]}"
      }
    ],
    "isError": false
  }
}
```

Error response (tool execution failure, NOT a JSON-RPC protocol error):

```json
{
  "jsonrpc": "2.0",
  "id": "call-uuid-001",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "LLM call failed: connection refused at http://host.docker.internal:8000"
      }
    ],
    "isError": true
  }
}
```

### 4.2 YAML outline format (Phase 1 output, Phase 2 input)

```yaml
topic: "quantum error correction"
generated_at: "2026-06-16T10:00:00Z"
subtopics:
  - key: surface_codes
    question: "What are surface codes and how do they detect and correct qubit errors?"
    fields:
      - definition
      - physical_implementation
      - error_thresholds
  - key: fault_tolerance_thresholds
    question: "What are the fault-tolerance thresholds for quantum error correction?"
    fields:
      - theoretical_bounds
      - experimental_results
  - key: hardware_implementations
    question: "How are quantum error correction codes implemented on current hardware platforms?"
    fields: []
  - key: overhead_costs
    question: "What are the qubit overhead costs of practical error correction?"
    fields: []
  - key: recent_advances
    question: "What are the most significant recent advances in quantum error correction since 2022?"
    fields: []
  - key: open_problems
    question: "What are the key open problems in quantum error correction research?"
    fields: []
```

Constraints:
- `key` is lowercase with underscores; used as the filename for the JSON result (`{key}.json`). Must be unique within the outline.
- `question` is the exact string passed to the web-search agent.
- `fields` is a list of aspects the LLM synthesis should address. May be empty.

### 4.3 Per-topic JSON result format (Phase 2 output, Phase 3 input)

Each parallel worker writes one file: `{output_dir}/{key}.json`.

```json
{
  "key": "surface_codes",
  "question": "What are surface codes and how do they detect and correct qubit errors?",
  "status": "success",
  "search_queries": [
    "surface codes quantum error correction",
    "surface code qubit error detection mechanism"
  ],
  "raw_results": [
    {
      "title": "Surface codes: Towards practical large-scale quantum computation",
      "url": "https://arxiv.org/abs/1208.0928",
      "snippet": "Surface codes are a family of topological quantum codes..."
    }
  ],
  "synthesis": "Surface codes are a class of topological quantum error-correcting codes that...",
  "sources": [
    {
      "title": "Surface codes: Towards practical large-scale quantum computation",
      "url": "https://arxiv.org/abs/1208.0928"
    }
  ],
  "fields_covered": {
    "definition": "A surface code encodes logical qubits into a 2D lattice of physical qubits...",
    "physical_implementation": "On superconducting platforms, surface codes require...",
    "error_thresholds": "The fault-tolerance threshold for surface codes is approximately 1%..."
  },
  "error": null,
  "completed_at": "2026-06-16T10:01:42Z"
}
```

On failure, the file is still written (to allow partial report compilation):

```json
{
  "key": "hardware_implementations",
  "question": "...",
  "status": "failed",
  "error": "Search timeout after 60s",
  "search_queries": [],
  "raw_results": [],
  "synthesis": null,
  "sources": [],
  "fields_covered": {},
  "completed_at": "2026-06-16T10:02:00Z"
}
```

### 4.4 Final markdown report structure

```markdown
# Research Report: Quantum Error Correction

*Generated: 2026-06-16 | Topics researched: 6 | Sources: 24*

## Executive Summary

[2-3 paragraph synthesis of the entire topic, written by the LLM after
reading all per-topic JSON results.]

---

## Surface Codes

[Synthesis from surface_codes.json. Addresses each field in fields_covered.
Inline citations as [1], [2], etc.]

---

## Fault-Tolerance Thresholds

[Synthesis from fault_tolerance_thresholds.json.]

---

[... one section per subtopic ...]

---

## Gaps and Limitations

[What was not found, what queries returned sparse results, which subtopics
failed with errors, what the coverage does not include.]

---

## Sources

1. Surface codes: Towards practical large-scale quantum computation — https://arxiv.org/abs/1208.0928
2. [...]
```

---

## 5. Implementation Steps

Steps are ordered. Complete each before starting the next. Each is a self-contained coding task.

### Step 1 — `server.py`: Python MCP server skeleton

Create `services/skills/deep-research/server.py`. This is the entry point spawned by the SkillRegistry.

Tasks:
- Set up `logging.basicConfig(stream=sys.stderr, ...)` before any other import that might print.
- Use the official `mcp` Python SDK: `FastMCP` or `Server` + `StdioServerTransport`.
- Register three tools: `research_outline`, `research_deep`, `research_report` — stubs at first.
- Each stub returns `{"status": "not_implemented"}` wrapped in the correct MCP content shape.
- Confirm zero stdout bytes at startup: `python server.py < /dev/null 2>/dev/null | wc -c` must output `0`.

### Step 2 — `phases/outline.py`: Phase 1

Tasks:
- Accept `topic: str`, `output_path: str`, `fields: list[str]`, `num_subtopics: int`.
- Build a prompt instructing the LLM to decompose the topic into `num_subtopics` focused research questions. Ask for YAML output matching the schema in Section 4.2.
- Call `INFERENCE_URL` via `httpx.AsyncClient` (Contract A). Use the model from the `model` env var or fall back to `"google/gemma-4-9b-it"`.
- Parse the LLM's YAML response with `yaml.safe_load`. Validate required fields (`key`, `question`). Sanitize `key` values to `[a-z0-9_]` to ensure safe filenames.
- Write the validated YAML to `output_path` (create parent dirs with `Path.mkdir(parents=True)`).
- Return `{"outline_path": output_path, "subtopics": [s["key"] for s in subtopics]}`.

### Step 3 — `phases/deep.py`: Phase 2 fan-out

Tasks:
- Accept `outline_path: str`, `output_dir: str`, `parallel: int`, `timeout_per_topic: int`.
- Read and parse the YAML outline with `yaml.safe_load`.
- Create `output_dir` if it does not exist.
- Use `asyncio.TaskGroup` with a `asyncio.Semaphore(parallel)` to cap concurrency.
- For each subtopic, spawn a coroutine that calls `agents.web_search.run_agent(subtopic, output_dir, timeout_per_topic)`.
- Collect results (succeeded / failed counts) and return them.
- Never let one failed worker cancel others: catch exceptions per-task inside the semaphore guard, write a failure JSON, and continue.

### Step 4 — `agents/web_search.py`: per-topic search + synthesis agent

Tasks:
- Accept a subtopic dict (`key`, `question`, `fields`) and the `output_dir`.
- Generate 2-3 search queries from the question (either via a quick LLM call or simple rule-based expansion).
- Execute searches via `httpx.AsyncClient`. Support two backends, controlled by env vars:
  - `SEARCH_API_KEY` set: use the configured search API (SerpAPI, Brave Search, or Tavily — one implementation only; default to Brave via `BRAVE_SEARCH_API_KEY`).
  - No API key: fall back to DuckDuckGo Instant Answer API (`https://api.duckduckgo.com/?q=...&format=json`).
- Parse results: extract `title`, `url`, `snippet` for each hit. Collect up to 10 results per query.
- Call the LLM with the raw results to synthesize a structured answer. Include `fields` in the prompt so the LLM addresses each requested aspect.
- Write the result JSON to a **temporary file** in `output_dir` first (`{key}.json.tmp`), then atomically rename to `{key}.json`. This prevents the report phase from reading partial JSON if the worker crashes mid-write.
- On any unhandled exception: write a failure JSON (status `"failed"`, error message) via the same temp-file + rename pattern.

### Step 5 — `phases/report.py`: Phase 3 synthesis

Tasks:
- Accept `results_dir: str`, `output_path: str`, `topic: str`.
- Glob `results_dir/*.json`. Raise a structured error if no files are found.
- Read and parse each JSON file. Separate successes from failures.
- Build a single LLM prompt containing all synthesis texts and sources. Structure the prompt to produce the markdown sections in Section 4.4: executive summary, per-topic sections, gaps, sources list.
- Call `INFERENCE_URL`. Parse the response.
- Write the markdown to `output_path` (temp-file + atomic rename).
- Return `{"report_path": output_path, "topics_included": N, "topics_failed": M}`.

### Step 6 — `Dockerfile` and `requirements.txt`

`requirements.txt`:

```
mcp>=1.0.0
httpx>=0.27.0
pyyaml>=6.0
anyio>=4.0
python-dotenv>=1.0.0
```

`Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /skill
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
```

`PYTHONUNBUFFERED=1` is required: without it, Python buffers stdout and the MCP bridge may hang waiting for the first JSON-RPC byte.

---

## 6. Key Code Patterns

### `server.py` — MCP tool registration with stderr-only logging

```python
# services/skills/deep-research/server.py
from __future__ import annotations

import logging
import sys

# CRITICAL: configure stderr logging BEFORE any other import.
# Any import that prints to stdout (banner, debug line) will corrupt JSON-RPC.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("deep-research")

import asyncio
import json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from phases.outline import generate_outline
from phases.deep import run_deep_phase
from phases.report import compile_report

app = Server("deep-research")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="research_outline",
            description="Phase 1: generate a YAML outline of subtopics for a research topic.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "output_path": {"type": "string"},
                    "fields": {"type": "array", "items": {"type": "string"}},
                    "num_subtopics": {"type": "integer", "default": 6},
                },
                "required": ["topic", "output_path"],
            },
        ),
        Tool(
            name="research_deep",
            description="Phase 2: fan out parallel search agents for each outline subtopic.",
            inputSchema={
                "type": "object",
                "properties": {
                    "outline_path": {"type": "string"},
                    "output_dir": {"type": "string"},
                    "parallel": {"type": "integer", "default": 3},
                    "timeout_per_topic": {"type": "integer", "default": 60},
                },
                "required": ["outline_path", "output_dir"],
            },
        ),
        Tool(
            name="research_report",
            description="Phase 3: compile JSON results into a markdown report.",
            inputSchema={
                "type": "object",
                "properties": {
                    "results_dir": {"type": "string"},
                    "output_path": {"type": "string"},
                    "topic": {"type": "string"},
                },
                "required": ["results_dir", "output_path", "topic"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    log.info("tool call: %s args=%s", name, arguments)  # -> stderr only
    try:
        if name == "research_outline":
            result = await generate_outline(**arguments)
        elif name == "research_deep":
            result = await run_deep_phase(**arguments)
        elif name == "research_report":
            result = await compile_report(**arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        return [TextContent(type="text", text=json.dumps(result))]
    except Exception as exc:
        log.error("tool %s failed: %r", name, exc)  # -> stderr
        return [TextContent(type="text", text=f"Error: {exc}")]


async def main() -> None:
    log.info("deep-research MCP server starting")  # -> stderr
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

### `phases/deep.py` — `asyncio.TaskGroup` fan-out with semaphore

```python
# services/skills/deep-research/phases/deep.py
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml

from agents.web_search import run_agent

log = logging.getLogger("deep-research.deep")


async def run_deep_phase(
    outline_path: str,
    output_dir: str,
    parallel: int = 3,
    timeout_per_topic: int = 60,
) -> dict:
    outline_text = Path(outline_path).read_text(encoding="utf-8")
    outline = yaml.safe_load(outline_text)
    subtopics = outline.get("subtopics", [])

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(parallel)
    succeeded = 0
    failed = 0

    async def bounded(subtopic: dict) -> None:
        nonlocal succeeded, failed
        async with sem:
            try:
                await asyncio.wait_for(
                    run_agent(subtopic, output_dir),
                    timeout=timeout_per_topic,
                )
                succeeded += 1
            except asyncio.TimeoutError:
                log.warning("timeout for subtopic: %s", subtopic.get("key"))
                _write_failure(subtopic, output_dir, "timeout")
                failed += 1
            except Exception as exc:
                log.error("subtopic %s failed: %r", subtopic.get("key"), exc)
                _write_failure(subtopic, output_dir, str(exc))
                failed += 1

    async with asyncio.TaskGroup() as tg:
        for subtopic in subtopics:
            tg.create_task(bounded(subtopic))

    log.info("deep phase done: %d succeeded, %d failed", succeeded, failed)
    return {
        "results_dir": output_dir,
        "succeeded": succeeded,
        "failed": failed,
        "total": len(subtopics),
    }


def _write_failure(subtopic: dict, output_dir: str, error: str) -> None:
    import json
    import tempfile
    import os
    from datetime import datetime, timezone

    key = subtopic.get("key", "unknown")
    result = {
        "key": key,
        "question": subtopic.get("question", ""),
        "status": "failed",
        "error": error,
        "search_queries": [],
        "raw_results": [],
        "synthesis": None,
        "sources": [],
        "fields_covered": {},
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    dest = Path(output_dir) / f"{key}.json"
    # atomic write: temp file + rename
    fd, tmp = tempfile.mkstemp(dir=output_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
```

### `agents/web_search.py` — search → parse → LLM synthesis → atomic JSON write

```python
# services/skills/deep-research/agents/web_search.py
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

log = logging.getLogger("deep-research.web_search")

INFERENCE_URL = os.environ.get("INFERENCE_URL", "http://host.docker.internal:8000")
MODEL = os.environ.get("RESEARCH_MODEL", "google/gemma-4-9b-it")
BRAVE_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")


async def run_agent(subtopic: dict, output_dir: str) -> None:
    key = subtopic["key"]
    question = subtopic["question"]
    fields = subtopic.get("fields", [])

    log.info("starting agent for key=%s", key)

    search_queries = _expand_queries(question)
    raw_results = await _search_all(search_queries)
    synthesis, sources, fields_covered = await _synthesize(question, fields, raw_results)

    result = {
        "key": key,
        "question": question,
        "status": "success",
        "search_queries": search_queries,
        "raw_results": raw_results,
        "synthesis": synthesis,
        "sources": sources,
        "fields_covered": fields_covered,
        "error": None,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

    _atomic_write(result, Path(output_dir) / f"{key}.json")
    log.info("agent done: key=%s sources=%d", key, len(sources))


def _expand_queries(question: str) -> list[str]:
    # Rule-based expansion — avoids an LLM round-trip for query generation.
    # The question is used verbatim plus a "recent" variant and a "papers" variant.
    base = question.rstrip("?")
    return [
        question,
        f"{base} recent research 2024 2025",
        f"{base} academic papers survey",
    ]


async def _search_all(queries: list[str]) -> list[dict]:
    results = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for q in queries:
            hits = await _search_one(client, q)
            results.extend(hits)
    # deduplicate by URL
    seen: set[str] = set()
    unique = []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)
    return unique[:15]  # cap at 15 results total


async def _search_one(client: httpx.AsyncClient, query: str) -> list[dict]:
    if BRAVE_API_KEY:
        return await _brave_search(client, query)
    return await _ddg_search(client, query)


async def _brave_search(client: httpx.AsyncClient, query: str) -> list[dict]:
    resp = await client.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": 5},
        headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
        for r in data.get("web", {}).get("results", [])
    ]


async def _ddg_search(client: httpx.AsyncClient, query: str) -> list[dict]:
    # DuckDuckGo Instant Answer API — limited results, no API key required.
    resp = await client.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for r in data.get("RelatedTopics", [])[:5]:
        if "Text" in r and "FirstURL" in r:
            results.append({
                "title": r.get("Text", "")[:80],
                "url": r["FirstURL"],
                "snippet": r.get("Text", ""),
            })
    return results


async def _synthesize(
    question: str, fields: list[str], raw_results: list[dict]
) -> tuple[str, list[dict], dict]:
    snippets = "\n\n".join(
        f"[{i+1}] {r['title']}\nURL: {r['url']}\n{r['snippet']}"
        for i, r in enumerate(raw_results)
    )
    fields_instruction = ""
    if fields:
        fields_list = ", ".join(f'"{f}"' for f in fields)
        fields_instruction = (
            f"\n\nFor each of these aspects, write a dedicated paragraph: {fields_list}."
            " Use the aspect name as a heading."
        )

    prompt = (
        f"You are a research assistant. Based on the search results below, write a "
        f"thorough synthesis answering this question:\n\n{question}{fields_instruction}\n\n"
        f"Cite sources using [N] notation where N is the result number.\n\n"
        f"SEARCH RESULTS:\n{snippets}\n\n"
        f"Write your synthesis now. Do not repeat the question."
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{INFERENCE_URL}/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 2048,
                "stream": False,
            },
        )
        resp.raise_for_status()

    data = resp.json()
    synthesis = data["choices"][0]["message"]["content"]

    sources = [{"title": r["title"], "url": r["url"]} for r in raw_results]

    # Parse field coverage from the synthesis text.
    # Look for lines that start with a field name as a heading.
    fields_covered: dict[str, str] = {}
    if fields:
        lines = synthesis.split("\n")
        current_field: str | None = None
        current_lines: list[str] = []
        for line in lines:
            stripped = line.lstrip("#").strip().lower().replace(" ", "_")
            if stripped in {f.lower().replace(" ", "_") for f in fields}:
                if current_field:
                    fields_covered[current_field] = "\n".join(current_lines).strip()
                current_field = stripped
                current_lines = []
            elif current_field:
                current_lines.append(line)
        if current_field and current_lines:
            fields_covered[current_field] = "\n".join(current_lines).strip()

    return synthesis, sources, fields_covered


def _atomic_write(data: dict, dest: Path) -> None:
    """Write JSON to a temp file in the same directory, then atomically rename.

    If the process crashes mid-write, the destination file is either the previous
    complete version or does not exist. It is never a partial write.
    """
    fd, tmp_path = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, dest)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
```

### How the skill calls the inference server (`INFERENCE_URL`, OpenAI-compatible)

All LLM calls follow Contract A. Use `httpx.AsyncClient` with `stream=False` for synthesis calls (responses are bounded). Use `stream=True` only if you need to show progress for very long completions.

```python
# Common pattern — reuse this in outline.py and report.py
import os
import httpx

INFERENCE_URL = os.environ.get("INFERENCE_URL", "http://host.docker.internal:8000")
MODEL = os.environ.get("RESEARCH_MODEL", "google/gemma-4-9b-it")

async def llm_call(messages: list[dict], max_tokens: int = 2048) -> str:
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            f"{INFERENCE_URL}/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": max_tokens,
                "stream": False,
            },
        )
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]
```

Never hardcode the model name or base URL. Read from env vars, fall back to the defaults above.

### JSON result schema (dataclass for type safety)

```python
# agents/web_search.py — keep this as the canonical schema reference
from dataclasses import dataclass, field, asdict
from typing import Optional

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str

@dataclass
class TopicResult:
    key: str
    question: str
    status: str                    # "success" | "failed"
    search_queries: list[str]
    raw_results: list[dict]
    synthesis: Optional[str]
    sources: list[dict]            # [{"title": str, "url": str}]
    fields_covered: dict[str, str] # field_name -> paragraph text
    error: Optional[str]
    completed_at: str              # ISO 8601

    def to_json(self) -> str:
        import json
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)
```

---

## 7. Integration Verification

### Phase 1 in isolation

```bash
# Confirm zero stdout pollution at startup
python services/skills/deep-research/server.py < /dev/null 2>/dev/null | wc -c
# Must output: 0

# Phase 1 via Python directly (bypasses MCP)
cd services/skills/deep-research
python - <<'EOF'
import asyncio
from phases.outline import generate_outline

result = asyncio.run(generate_outline(
    topic="attention mechanisms in transformers",
    output_path="/tmp/dr_test/outline.yaml",
    num_subtopics=4,
))
print(result)
import yaml, pathlib
print(yaml.safe_load(pathlib.Path("/tmp/dr_test/outline.yaml").read_text()))
EOF
```

Expected: `result["outline_path"]` exists, YAML parses cleanly, has 4 subtopics with `key` and `question` fields.

### Phase 2 in isolation

```bash
python - <<'EOF'
import asyncio
from phases.deep import run_deep_phase

result = asyncio.run(run_deep_phase(
    outline_path="/tmp/dr_test/outline.yaml",
    output_dir="/tmp/dr_test/results",
    parallel=2,
    timeout_per_topic=45,
))
print(result)
import os, json
for f in os.listdir("/tmp/dr_test/results"):
    data = json.loads(open(f"/tmp/dr_test/results/{f}").read())
    print(f, data["status"], len(data.get("sources", [])), "sources")
EOF
```

Expected: `result["succeeded"] > 0`, each JSON file parses cleanly, no partial files exist.

### Phase 3 in isolation

```bash
python - <<'EOF'
import asyncio
from phases.report import compile_report

result = asyncio.run(compile_report(
    results_dir="/tmp/dr_test/results",
    output_path="/tmp/dr_test/report.md",
    topic="attention mechanisms in transformers",
))
print(result)
import pathlib
print(pathlib.Path("/tmp/dr_test/report.md").read_text()[:500])
EOF
```

Expected: `result["report_path"]` exists, markdown contains "## Executive Summary" and "## Sources".

### End-to-end via MCP protocol

Send the MCP initialize handshake then call all three tools in sequence via stdin:

```bash
python services/skills/deep-research/server.py <<'MCPEOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"test","version":"0.0.1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"research_outline","arguments":{"topic":"graph neural networks","output_path":"/tmp/mcp_test/outline.yaml","num_subtopics":3}}}
MCPEOF
```

Expected: each line of stdout is a valid JSON-RPC response. `id:2` response lists three tools. `id:3` response has `isError: false` and `outline_path` in the content JSON.

### Atomic write safety test

```bash
python - <<'EOF'
# Verify that a simulated crash mid-write does not leave a partial .json file.
# The _atomic_write function writes to a .tmp then os.replace; the destination
# is either absent or complete.
import os, json, pathlib
from agents.web_search import _atomic_write

dest = pathlib.Path("/tmp/atomic_test/topic.json")
dest.parent.mkdir(parents=True, exist_ok=True)

# Simulate a complete write
_atomic_write({"key": "test", "status": "success"}, dest)
assert dest.exists()
data = json.loads(dest.read_text())
assert data["key"] == "test"

# Verify no .tmp files remain
tmps = list(dest.parent.glob("*.tmp"))
assert tmps == [], f"stale tmp files: {tmps}"
print("atomic write test passed")
EOF
```

---

## 8. Done Criteria

- [ ] `python server.py < /dev/null 2>/dev/null | wc -c` outputs `0` — zero stdout bytes at startup
- [ ] `tools/list` response lists exactly three tools: `research_outline`, `research_deep`, `research_report`, each with a valid self-contained `inputSchema`
- [ ] `research_outline` writes a YAML file that parses cleanly and has the expected fields (`topic`, `generated_at`, `subtopics` with `key` and `question` per entry)
- [ ] All `key` values in the outline are `[a-z0-9_]+` — safe to use as filenames
- [ ] `research_deep` fans out parallel workers up to the `parallel` limit — confirmed by adding `log.info("semaphore acquired for %s", key)` and observing at most `parallel` interleaved lines in stderr
- [ ] A per-topic worker that times out writes a failure JSON (status `"failed"`) and does not cancel other workers
- [ ] No partial JSON files exist after a run — only complete files or no file (atomic write confirmed by test)
- [ ] `research_report` produces a markdown file containing at minimum: "Executive Summary" heading, one heading per subtopic, "Sources" section with URLs
- [ ] All LLM calls use `INFERENCE_URL` from the environment — no hardcoded `localhost` or `8000` in the source
- [ ] All logging goes exclusively to stderr — grepping source for bare `print(` returns zero matches in `server.py`, `phases/`, and `agents/`
- [ ] `Dockerfile` builds without error: `docker build -t deep-research services/skills/deep-research`
- [ ] Container starts and passes the zero-stdout test: `docker run --rm deep-research python server.py < /dev/null 2>/dev/null | wc -c` outputs `0`
- [ ] `SKILL.md` frontmatter parses as valid YAML and passes `SkillRunner.discover()` without warnings

---

## 9. Common Mistakes

### Stdout pollution — the critical failure mode

Any `print()` call, import-time banner, or library that writes to stdout corrupts the JSON-RPC stream before `initialize` is received. The failure message will be misleading (`"Unexpected token"`, `"Method not found"`), not `"stdout pollution"`.

Enforce before writing any business logic:

```python
# FIRST lines of server.py — before all other imports
import logging, sys
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
```

Then grep the entire skill directory:

```bash
grep -rn 'print(' services/skills/deep-research/
```

Zero matches required. Use `log.info()` / `log.error()` everywhere.

### Using sync `httpx` inside async coroutines

`httpx.get(...)` (the sync form) blocks the event loop. Inside an `async def` coroutine, always use `httpx.AsyncClient`:

```python
# WRONG — blocks the event loop, kills parallelism
results = httpx.get(url).json()

# CORRECT — non-blocking
async with httpx.AsyncClient(timeout=20.0) as client:
    resp = await client.get(url)
    results = resp.json()
```

If you must call a sync library (e.g. a search SDK with no async interface), wrap it: `await asyncio.to_thread(sync_fn, args)`.

### Exceeding search rate limits under parallel load

With `parallel=5` and 3 queries per topic, 15 concurrent HTTP requests hit the search API simultaneously. DuckDuckGo will 429. Brave Search's free tier allows 1 req/s.

Mitigation already built in:
- `parallel` defaults to 3 (keeps concurrent requests at 9 max).
- The semaphore in `deep.py` enforces this limit.
- Document in the SKILL.md body that `parallel` should be 2-4 for most providers.

Do not raise the default or remove the semaphore.

### Writing partial JSON on crash (fixed by atomic rename)

If `json.dump()` raises mid-write (disk full, process killed), the output file is truncated. The report phase will fail with a JSON parse error on that file.

The pattern in `_atomic_write` prevents this: write to `{key}.json.tmp` in the same directory (same filesystem, so `os.replace` is atomic), then `os.replace(tmp, dest)`. The destination is either the previous complete file or the new complete file. Never a partial write.

Always use `_atomic_write`. Never open the destination path directly for writing.

### TaskGroup cancellation semantics

`asyncio.TaskGroup` cancels all sibling tasks when any task raises an unhandled exception. This would abort the entire fan-out if one worker crashes.

The `bounded()` wrapper in `deep.py` catches all exceptions inside the `async with sem` block and writes a failure JSON instead of re-raising. This prevents `TaskGroup` from seeing an unhandled exception and cancelling siblings. The `try/except` in `bounded()` is load-bearing — do not simplify it away.

### `yaml.load()` instead of `yaml.safe_load()`

The outline YAML is written by the LLM. Never pass LLM output to `yaml.load()` with any loader other than `SafeLoader`. Use `yaml.safe_load(text)` everywhere. An LLM could in principle produce a YAML document with `!!python/object/apply:os.system` — `yaml.safe_load` rejects it; `yaml.load` executes it.
