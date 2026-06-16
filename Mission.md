# 🎯 PROJECT MISSION: LOCAL-CLAW (State-Transfer Document)

## 🤖 Identity
Local-Claw is a SOTA, polyglot autonomous agent designed for high-end software engineering and professional academic writing. It is built to move beyond simple RAG, utilizing a "Brain-Nervous System-Hands" architecture.

## 🏗️ Current Technical Stack
- **The Brain (Orchestration):** Gemma 4 via Unsloth (Python).
- **The Nervous System (Bridge):** Transitioning to Model Context Protocol (MCP) for polyglot tool-use.
- **The Hands (Skills):** A modular suite of TypeScript and Rust skills (Structural Coding, Professional Writing, Adversarial Critique).
- **Memory Layer:** AgentMemory (Conversational) + Codegraph (Structural/AST).
- **Reasoning Framework:** Obra Superpowers (A framework for self-improving specifications and implementation plans).

## 📍 Current Project State (Checkpoint: End of Milestone 2)

### ✅ Completed
- [x] **Infra Setup:** `setup_infra.sh` configured to install Node.js, NPM, AgentMemory, Codegraph, and clone the `obra/superpowers` repository.
- [x] **Project Structure:** Organized into `/config`, `/core`, `/tools`, `/specs`, and `/project`.
- [x] **Base Specs:** Created foundational specifications for the Orchestrator, Skills Framework, Coding, Writing, Critique, and System suites.
- [x] **Agency Implementation:** Transitioned from a passive script to an "Active Agency" loop (Milestone 2) capable of tool-invocation.

### 🛠️ Pending (The Roadmap)
- [ ] **Milestone 3: The Bridge (UPCOMING).** Implement the MCP Server (TypeScript) and the MCP Client (Python).
- [ ] **The Superpower Trigger:** Implement the `invoke_superpower` tool to allow the agent to use the Obra framework to upgrade its own specs.
- [ ] **SOTA Skill Implementation:** Build the AST-based coding tools and Multi-Pass writing pipelines.
- [ ] **The Curator:** Implement the Tiered Context Manager (Working $\rightarrow$ Semantic $\rightarrow$ Structural memory).

---

## 🚀 Pickup Instructions for the Agent

**Attention Local-Claw:** When you resume this session, follow these steps to synchronize:

1. **Read the Specs:** Analyze `/specs/` to understand the blueprint of your own brain and hands.
2. **Audit Infrastructure:** Verify that `agentmemory` is running on `:3111` and `codegraph` is initialized in the `/project` folder.
3. **Check the Mission:** Confirm the current goal is **Milestone 3: The Bridge**.
4. **Immediate Next Task:** Begin the implementation of the **MCP Server in TypeScript** to enable the polyglot skill layer.

## 📚 Reference Library
- `specs/spec_orchestrator_v2.md`: The blueprint for your reasoning loop.
- `specs/spec_skills_framework.md`: The definition of your MCP bridge.
- `/superpowers/`: The Obra framework used for your self-evolution.

---
**Status:** HIbernating. 
**Last Update:** All base specs locked. Infrastructure ready. 
**Next Trigger:** Start Milestone 3 Implementation.