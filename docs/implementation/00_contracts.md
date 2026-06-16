# Interface Contracts — Labmate

This document defines the exact wire formats exchanged between every pair of services. All implementation plans reference this document. When in doubt about a message shape, this file is authoritative.

---

## System Topology

```
vLLM (host :8000)
    ▲  OpenAI HTTP (Contract A)
    │
Python Orchestrator (lm-orchestrator)
    │  stdio JSON-RPC 2.0 (Contract B)
    ▼
TypeScript MCP Bridge (lm-mcp-bridge :9000)
    │  stdio JSON-RPC 2.0 (Contract B, same protocol)
    ▼
Skill child processes (spawned per skill)
    │
    ├── MongoDB (lm-mongodb :27017)  ← Contract C
    ├── Chroma  (lm-chroma   :8000)  ← Contract D
    └── Redis   (lm-redis    :6379)  ← Contract E

Skill Worker (lm-skill-worker)
    └── reads Redis Stream           ← Contract E

Discord Bot (in-process with orchestrator)
    └── discord.py asyncio           ← Contract F
```

---

## Contract A — Orchestrator ↔ vLLM (OpenAI-compatible HTTP)

**Base URL:** `${INFERENCE_URL}` (default `http://host.docker.internal:8000`)

### Chat completion request

```http
POST /v1/chat/completions
Content-Type: application/json

{
  "model": "google/gemma-4-9b-it",
  "messages": [
    {
      "role": "system",
      "content": "<system prompt with active skills injected>"
    },
    {
      "role": "user",
      "content": "Write a function that sorts a list of dicts by a key."
    },
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_abc123",
          "type": "function",
          "function": {
            "name": "repo_map",
            "arguments": "{\"path\": \"/workspace\"}"
          }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_abc123",
      "content": "<tool result JSON string>"
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "repo_map",
        "description": "Generate a ranked symbol map of a code repository",
        "parameters": {
          "type": "object",
          "properties": {
            "path": { "type": "string", "description": "Absolute path to repo" },
            "max_tokens": { "type": "integer", "default": 8192 }
          },
          "required": ["path"]
        }
      }
    }
  ],
  "tool_choice": "auto",
  "stream": true,
  "temperature": 0.2,
  "max_tokens": 4096
}
```

### Streaming response (SSE)

Each line is `data: <json>\n\n`. End is `data: [DONE]\n\n`.

```json
{"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{"content":"Here"},"index":0}]}
{"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{"content":" is"},"index":0}]}
{"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_abc123","type":"function","function":{"name":"repo_map","arguments":""}}]},"index":0}]}
{"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"path\":"}}]},"index":0}]}
{"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{},"finish_reason":"tool_calls","index":0}]}
```

### Health check

```http
GET /health
→ 200 OK  {"status": "ok"}
```

### Model list

```http
GET /v1/models
→ 200 OK  {"object":"list","data":[{"id":"google/gemma-4-9b-it","object":"model",...}]}
```

---

## Contract B — MCP JSON-RPC 2.0 (stdio)

Used in two places:
- Orchestrator (Python) **as client** → MCP Bridge (TypeScript) **as server**
- MCP Bridge (TypeScript) **as client** → Skill processes **as server**

Transport: newline-delimited JSON over stdin/stdout. Each message is one line.

### Initialize handshake (client → server, then server → client)

```json
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"clientInfo":{"name":"labmate-orchestrator","version":"1.0.0"}}}

← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"labmate-mcp-bridge","version":"1.0.0"}}}

→ {"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
```

### List available tools

```json
→ {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}

← {
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
      "tools": [
        {
          "name": "repo_map",
          "description": "Generate a ranked symbol map of a code repository",
          "inputSchema": {
            "type": "object",
            "properties": {
              "path": { "type": "string" },
              "max_tokens": { "type": "integer", "default": 8192 }
            },
            "required": ["path"]
          }
        }
      ]
    }
  }
```

### Call a tool

```json
→ {
    "jsonrpc": "2.0",
    "id": "call-uuid-001",
    "method": "tools/call",
    "params": {
      "name": "repo_map",
      "arguments": { "path": "/workspace", "max_tokens": 4096 }
    }
  }

← {
    "jsonrpc": "2.0",
    "id": "call-uuid-001",
    "result": {
      "content": [
        {
          "type": "text",
          "text": "{\"symbols\": [{\"name\": \"sort_dicts\", \"file\": \"utils.py\", \"line\": 12, \"score\": 0.87}]}"
        }
      ],
      "isError": false
    }
  }
```

### Tool error response

```json
← {
    "jsonrpc": "2.0",
    "id": "call-uuid-001",
    "result": {
      "content": [{ "type": "text", "text": "FileNotFoundError: /workspace does not exist" }],
      "isError": true
    }
  }
```

Note: MCP errors are returned as `isError: true` in the result, NOT as JSON-RPC error objects. JSON-RPC errors (`{"error": {...}}`) indicate a protocol-level failure (malformed request, unknown method), not a tool execution failure.

### Pagination (large tool results)

When a tool result exceeds the size limit, return the first page with pagination metadata:

```json
{
  "content": [
    {
      "type": "text",
      "text": "<first 8192 chars of result>"
    },
    {
      "type": "text",
      "text": "{\"has_more\": true, \"next_offset\": 8192, \"total\": 45000}"
    }
  ],
  "isError": false
}
```

---

## Contract C — MongoDB Schema

**Database:** `labmate`  
**Driver:** `motor` (Python async) — `AsyncIOMotorClient`

### Collection: `sessions`

```json
{
  "_id": "ObjectId",
  "session_id": "uuid-string",
  "created_at": "ISODate",
  "updated_at": "ISODate",
  "status": "active | completed | failed",
  "goal": "string — the original user request",
  "goal_tree": {
    "id": "root",
    "description": "string",
    "status": "pending | in_progress | completed | failed",
    "children": []
  }
}
```

Indexes: `session_id` (unique), `status + created_at` (compound), TTL on `updated_at` (90 days).

### Collection: `messages`

```json
{
  "_id": "ObjectId",
  "session_id": "uuid-string",
  "sequence": 1,
  "role": "system | user | assistant | tool",
  "content": "string | null",
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": { "name": "repo_map", "arguments": "{...}" }
    }
  ],
  "tool_call_id": "call_abc123",
  "created_at": "ISODate",
  "token_count": 142
}
```

Indexes: `session_id + sequence` (compound, unique), `session_id + role`.

### Collection: `outbox`

Transactional outbox — written atomically with the message it tracks.

```json
{
  "_id": "ObjectId",
  "message_id": "ObjectId — references messages._id",
  "session_id": "uuid-string",
  "operation": "upsert | delete",
  "projected": false,
  "created_at": "ISODate"
}
```

Index: `projected + created_at` (for the outbox worker query).

---

## Contract D — Chroma Collections

**Client:** `chromadb.AsyncHttpClient(host="chroma", port=8000)`

### Collections

| Collection name | Content | Embedding model | Metadata fields |
|----------------|---------|-----------------|-----------------|
| `episodic` | Full message text | `all-MiniLM-L6-v2` | `session_id`, `role`, `created_at`, `token_count` |
| `semantic` | Distilled facts / summaries | `all-MiniLM-L6-v2` | `session_id`, `source_message_id`, `created_at` |
| `procedural` | Skill descriptions + outcomes | `all-MiniLM-L6-v2` | `skill_name`, `success`, `created_at` |

### Upsert (called by outbox worker)

```python
await collection.upsert(
    ids=[str(message["_id"])],           # MongoDB _id as Chroma point ID (idempotency key)
    documents=[message["content"]],
    metadatas=[{"session_id": session_id, "role": role, "created_at": ts}]
)
```

### Hybrid query (BM25 + dense → RRF)

```python
# Dense retrieval
dense_results = await collection.query(
    query_texts=["how to sort dicts in Python"],
    n_results=20,
    where={"session_id": session_id}
)

# BM25 handled via rank_bm25 in Python — see spec_memory.md §7.2
# RRF fusion: score = sum(1 / (k + rank)) where k=60
```

---

## Contract E — Redis Streams

**Client:** `redis.asyncio.Redis.from_url(REDIS_URL)`

### Task queue stream

**Stream key:** `lm:tasks`  
**Consumer group:** `skill-workers`  
**Consumer name:** `worker-{hostname}-{pid}`

#### Enqueue a task (orchestrator → Redis)

```python
task_id = str(uuid.uuid4())
await redis.xadd("lm:tasks", {
    "task_id": task_id,
    "skill_name": "repo_map",
    "input_json": json.dumps({"path": "/workspace"}),
    "session_id": session_id,
    "correlation_id": tool_call_id,   # maps result back to the LLM tool call
    "created_at": datetime.utcnow().isoformat()
})
```

#### Consume tasks (skill worker)

```python
results = await redis.xreadgroup(
    groupname="skill-workers",
    consumername=consumer_name,
    streams={"lm:tasks": ">"},        # ">" = only new, undelivered messages
    count=1,
    block=5000                         # block 5s, then loop
)
# After processing:
await redis.xack("lm:tasks", "skill-workers", message_id)
```

#### Recover stalled tasks (worker crashed mid-execution)

```python
# Reclaim messages idle > 30s
await redis.xautoclaim(
    "lm:tasks", "skill-workers", consumer_name,
    min_idle_time=30000,               # ms
    start_id="0-0"
)
```

#### Task result stream (worker → orchestrator)

**Stream key:** `lm:results:{session_id}`

```python
await redis.xadd(f"lm:results:{session_id}", {
    "task_id": task_id,
    "correlation_id": correlation_id,
    "status": "success | error",
    "output_json": json.dumps(result),
    "completed_at": datetime.utcnow().isoformat()
})
# TTL: expire after 1 hour
await redis.expire(f"lm:results:{session_id}", 3600)
```

### Working memory keys

| Key pattern | Type | Purpose | TTL |
|-------------|------|---------|-----|
| `lm:session:{session_id}:context` | String (JSON) | Current assembled context window | 30 min |
| `lm:session:{session_id}:state` | String (JSON) | LangGraph node state snapshot | 1 hour |
| `lm:session:{session_id}:tokens` | String (int) | Running token count | 1 hour |
| `lm:skill:registry` | Hash | `skill_name → metadata_json` | No TTL |

---

## Contract F — SKILL.md Format

Every skill directory must contain a `SKILL.md` file with this exact structure:

```markdown
---
name: ast-repo-map
description: Generate a PageRank-ranked symbol map of a code repository. Use when the user asks about codebase structure, what functions exist, or needs a repo overview.
trigger:
  - repo map
  - codebase overview
  - what functions
  - symbol map
  - list classes
tools:
  - name: repo_map
    description: Scan a repository and return a ranked JSONL symbol map
    inputSchema:
      type: object
      properties:
        path:
          type: string
          description: Absolute path to the repository root
        max_tokens:
          type: integer
          description: Token budget for the output
          default: 8192
        language:
          type: string
          description: Filter to a specific language (python, typescript, rust)
      required:
        - path
model: any
version: "1.0.0"
license: MIT
---

# AST Repo Map Skill

## Purpose
(Instructions the LLM reads when this skill is activated — what it does, when to use it, what to expect back.)

## Tool Usage
(How to call the tools, what the arguments mean, what the output format is.)

## Example
(A concrete example of a tool call and its result.)
```

### Metadata-only catalog entry (injected into system prompt)

When the orchestrator builds the system prompt, it injects only the YAML frontmatter per skill — not the full body. The body is lazy-loaded via a `load_skill` tool call when the LLM decides to activate it.

```
[SKILLS AVAILABLE]
- ast-repo-map: Generate a PageRank-ranked symbol map of a code repository. Use when the user asks about codebase structure...
- python-executor: Execute Python code in a sandboxed subprocess. Use when the user asks to run code...
```

---

## Contract G — Orchestrator Internal State (LangGraph)

The `State` TypedDict that travels through every LangGraph node:

```python
class GoalStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"

class Goal(TypedDict):
    id: str
    description: str
    status: GoalStatus
    parent_id: Optional[str]
    children: List[str]       # child goal IDs
    result: Optional[str]

class State(TypedDict):
    session_id: str
    messages: List[dict]      # OpenAI message format (Contract A)
    goals: Dict[str, Goal]    # goal_id → Goal
    root_goal_id: str
    active_tool_calls: List[dict]
    iteration: int
    token_count: int
    next_action: str          # "plan" | "execute" | "check" | "reflect" | "done" | "approval"
    error: Optional[str]
```

This state is checkpointed to MongoDB via `AsyncMongoDBSaver` after every node transition.

---

## Environment Variables Reference

All services read configuration from environment variables. No hardcoded URLs.

| Variable | Default | Used by |
|----------|---------|---------|
| `INFERENCE_URL` | `http://host.docker.internal:8000` | orchestrator, mcp-bridge |
| `MONGO_URI` | `mongodb://mongodb:27017/labmate` | orchestrator |
| `CHROMA_URL` | `http://chroma:8000` | orchestrator |
| `REDIS_URL` | `redis://redis:6379/0` | orchestrator, skill-worker |
| `MCP_BRIDGE_URL` | `http://mcp-bridge:9000` | orchestrator |
| `MCP_PORT` | `9000` | mcp-bridge |
| `LOG_LEVEL` | `info` | all services |
| `DISCORD_TOKEN` | — | orchestrator (Discord mode) |
| `DISCORD_WEBHOOK_URL` | — | orchestrator (Discord mode) |
| `LIVE_TESTS` | `0` | test suite |
| `IMG_MCP_BRIDGE` | `labmate/mcp-bridge:latest` | run-services.sh |
| `IMG_ORCHESTRATOR` | `labmate/orchestrator:latest` | run-services.sh |
| `IMG_SKILL_WORKER` | `labmate/skill-worker:latest` | run-services.sh |
