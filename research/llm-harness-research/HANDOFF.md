# Research Handoff — Local-Claw Spec Research
**Date:** 2026-06-15  
**Resume in:** fresh Claude Code session in /Users/zachstallbohm/Work/gemma

---

## What Was Accomplished

### Design doc written
`docs/superpowers/specs/2026-06-15-local-claw-bootstrap-design.md` — approved by user. Covers assistant.py (Gemma 4 + Qwen2.5-Coder-32B scaffold CLI), setup-infra.sh improvements, and the full spec expansion plan.

### Deep Research Skills installed
`~/.claude/skills/research*` — installed from ~/Work/Deep-Research-skills

### Research outline created
`research/llm-harness-research/outline.yaml` — 12 components + 3 infrastructure additions (15 total)
`research/llm-harness-research/fields.yaml` — 8 fields per item including bdd_gherkin_scenarios and optimal_implementation

### Research COMPLETED (4/15)
These JSON files are written and ready to use for spec writing:
1. `results/mcp_server_typescript.json` ✅ — Full SDK patterns, pitfalls, package.json/tsconfig/index.ts skeleton
2. `results/mcp_python_client.json` ✅ — Full MCPClient class, async patterns, pitfalls  
3. `results/agent_state_machines.json` ✅ — Pydantic v2 models, checkpoint schema, LangGraph comparison
4. `results/academic_writing_skills.json` ✅ — CoD loop, citation validation cascade, IMRaD stubs

### Research PENDING (11/15)
These still need research agents launched:
- coding_agent_orchestrators (1 agent already launched this session, may need relaunch)
- tiered_context_management
- ast_code_analysis_tools
- critique_reflexion_agents
- bdd_tdd_llm_testing
- local_model_serving_unsloth
- agent_memory_systems
- polyglot_skill_framework
- multi_agent_parallel_spawning
- storage_architecture
- docker_containerization

---

## Pending Tasks (from task list)
1. Write spec_orchestrator_v2.md
2. Write spec_project_structure.md
3. Write spec_state_machine.md
4. Write spec_mcp_server.md + spec_mcp_client.md + spec_skills_framework.md
5. Write spec_curator.md
6. Write expanded skill specs (coding, writing, critique, system)
7. Write spec_infrastructure.md (Docker, storage, multi-agent)

---

## What To Do In The New Session

### Step 1: Launch all 11 pending research agents in parallel
Read `research/llm-harness-research/outline.yaml` and `fields.yaml` for the prompts.
Use the SAME prompt format as the completed agents — write JSON to `research/llm-harness-research/results/<slug>.json`.

Each agent prompt needs these fields:
- best_architecture, reference_papers, reference_repos, key_libraries
- pitfalls, bdd_gherkin_scenarios, optimal_implementation, sota_improvements, uncertain

### Step 2: Once ALL 15 results exist, write all spec files
Specs to write (read the 4 completed JSONs as examples of depth expected):

| Spec File | Research JSON to use |
|---|---|
| specs/spec_orchestrator_v2.md | coding_agent_orchestrators.json |
| specs/spec_project_structure.md | docker_containerization.json + storage_architecture.json |
| specs/spec_state_machine.md | agent_state_machines.json ✅ |
| specs/spec_mcp_server.md | mcp_server_typescript.json ✅ |
| specs/spec_mcp_client.md | mcp_python_client.json ✅ |
| specs/spec_skills_framework.md | polyglot_skill_framework.json |
| specs/spec_curator.md | tiered_context_management.json + agent_memory_systems.json |
| specs/spec_skills_coding.md | ast_code_analysis_tools.json |
| specs/spec_skills_writing.md | academic_writing_skills.json ✅ |
| specs/spec_skills_critique.md | critique_reflexion_agents.json |
| specs/spec_skills_system.md | bdd_tdd_llm_testing.json + tiered_context_management.json |
| specs/spec_infrastructure.md | docker_containerization.json + storage_architecture.json + multi_agent_parallel_spawning.json |

### Step 3: All specs must include
- BDD/TDD Gherkin scenarios (Feature/Scenario format)
- Paper citations with arxiv IDs
- GitHub repo references
- Concrete Python/TypeScript code stubs
- Failure modes

### Step 4: Invoke writing-plans skill after all specs approved

---

## Key Decisions Already Made
- assistant.py: single file, Approach A, Rich TUI print-based, 30-exchange sliding window
- Models: Gemma 4B + Qwen2.5-Coder-32B (default), both via Unsloth 4-bit
- Target hardware: RunPod RTX A6000 32GB VRAM
- Storage: MongoDB (documents) + Chroma (vectors) + Redis (task queue)
- Containerization: Docker Compose for full stack
- Testing: BDD with pytest-bdd + Gherkin for all components
- MCP transport: stdio for local, StreamableHTTP ready for remote
- Context management: 3-tier (Working/Semantic/Structural)

---

## User Context
- Building Local-Claw: polyglot autonomous coding+writing agent
- Brain: Gemma 4 via Unsloth (Python orchestrator)
- Nervous System: MCP bridge (TypeScript server + Python client)  
- Hands: Skills in TypeScript/Rust/Python
- Memory: AgentMemory + Codegraph
- assistant.py is the SCAFFOLD TOOL — will be retired once full harness is built
- The specs are written to be READ by the scaffold AI assistant to implement the system
- Specs must be concrete enough for Gemma/Qwen to act on directly
