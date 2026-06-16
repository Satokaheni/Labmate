#!/bin/bash
# refactor_project.sh - Aligns folder structure with Local-Claw Specs

set -e
echo "♻️  Refactoring project structure to match specifications..."

# 1. Create Directory Structure
mkdir -p config core tools specs project

# 2. Create MISSION.md
cat << 'EOF' > MISSION.md
# 🎯 PROJECT MISSION: LOCAL-CLAW

## 🤖 Identity
Local-Claw is an autonomous, polyglot cognitive agent designed for high-end software engineering and academic writing. It utilizes a tiered memory system and a tool-augmented reasoning loop to minimize hallucinations and maximize structural accuracy.

## 🏗️ Technical Stack
- **Orchestration:** Python (FastLanguageModel / Unsloth)
- **Skill Layer:** TypeScript (MCP Protocol)
- **Performance Layer:** Rust (Context Compression / AST Analysis)
- **Memory:** AgentMemory (Conversational) & Codegraph (Structural)

## 📈 Implementation Roadmap
- [x] **Milestone 1:** Basic RAG integration.
- [x] **Milestone 2:** Active Agency (Tool-Use loop).
- [ ] **Milestone 3: The Bridge (Upcoming).** Implementation of the Model Context Protocol (MCP) for TypeScript skills.
- [ ] **Milestone 4: The Curator.** Implement a Custom Context Manager.

## 📚 Reference Documentation
- `specs/spec_orchestrator.md`: Logic for the reasoning loop.
- `specs/spec_skills_framework.md`: Definitions for TS/Rust skills.
- `specs/spec_context_manager.md`: Strategy for token management.
- `specs/spec_collaborator.md`: Architecture for the critique loop.
EOF

# 3. Create Specifications
cat << 'EOF' > specs/spec_orchestrator.md
# Spec: Orchestrator (The Brain)
**Goal:** Transition from a simple loop to a ReAct (Reason + Act) state-machine.
- **Logic:** Implement a Thought -> Action -> Observation -> Thought cycle.
- **State:** Track a "Goal Tree" to maintain focus during complex multi-step tasks.
- **Language:** Python.
EOF

cat << 'EOF' > specs/spec_skills_framework.md
# Spec: Skills Framework (The Hands)
**Goal:** Use MCP (Model Context Protocol) for a polyglot skill ecosystem.
- **Coding:** AST Analysis, Test Generation, Refactor Patterns.
- **Writing:** Style Transfer (Academic/Email), Draft Structuring, Citation Management.
- **Critique:** Adversarial Review, Consistency Checking.
- **Language:** TypeScript (Primary) / Rust (Performance).
EOF

cat << 'EOF' > specs/spec_context_manager.md
# Spec: Context Manager (The Filter)
**Goal:** Implement Hybrid Context Injection to solve token overflow.
- **Tiers:** Working Memory (Short-term) -> Semantic Memory (AgentMemory) -> Structural Memory (Codegraph).
- **Compression:** Background Summary Buffering to compress old conversation blocks.
- **Language:** Rust (Primary) / Python.
EOF

cat << 'EOF' > specs/spec_collaborator.md
# Spec: Collaborator (The Social Brain)
**Goal:** Implement a Multi-Agent critique loop.
- **Workflow:** Generator -> Critic -> Refiner.
- **Logic:** The Critic agent assumes a different persona to find flaws in the Generator's output before it ever reaches the user.
- **Language:** Python.
EOF

# 4. Create Core Application Files (Updated for relative paths)
cat << 'EOF' > config/settings.py
MODEL_NAME = "unsloth/gemma-4b-it"
MEMORY_URL = "http://localhost:3111"
CODEBASE_PATH = "project"
MAX_SEQ_LENGTH = 4096
EOF

cat << 'EOF' > core/prompt_manager.py
SYSTEM_PROMPT = """You are Local-Claw, a high-reasoning coding agent. 
You have access to two tools:
1. recall_memory(query): Use this to find past notes, decisions, or user preferences.
2. search_code(query): Use this to find actual definitions, classes, or functions in the codebase.

TOOL USE RULE:
If you need information from a tool, you MUST output a tool call in this exact format:
[TOOL: tool_name("query")]

Example:
User: "What is the auth logic?"
Assistant: I need to check the code. [TOOL: search_code("auth logic")]
"""
EOF

cat << 'EOF' > tools/memory_tool.py
import requests
from config.settings import MEMORY_URL
def recall(query):
    try:
        response = requests.get(f"{MEMORY_URL}/search", params={"q": query}, timeout=2)
        if response.status_code == 200:
            return "\n".join([m['content'] for m in response.json()])
    except Exception: return "Memory server unreachable."
    return "No relevant memories found."
def remember(content):
    try:
        requests.post(f"{MEMORY_URL}/remember", json={"content": content}, timeout=2)
        return "Stored."
    except Exception: return "Failed."
EOF

cat << 'EOF' > tools/code_tool.py
import subprocess
import os
from config.settings import CODEBASE_PATH
def search_code(query):
    try:
        abs_path = os.path.abspath(CODEBASE_PATH)
        result = subprocess.run(["codegraph", "search", query], cwd=abs_path, capture_output=True, text=True, timeout=5)
        return result.stdout if result.stdout else "No structural matches found."
    except Exception as e: return f"Codegraph Error: {e}"
EOF

cat << 'EOF' > core/orchestrator.py
import torch
import re
from unsloth import FastLanguageModel
from config.settings import MODEL_NAME, MAX_SEQ_LENGTH
from core.prompt_manager import SYSTEM_PROMPT
from tools.memory_tool import recall, remember
from tools.code_tool import search_code

class LocalClawOrchestrator:
    def __init__(self):
        self.model, self.tokenizer = FastLanguageModel.from_pretrained(
            model_name=MODEL_NAME, max_seq_length=MAX_SEQ_LENGTH, load_in_4bit=True
        )
        FastLanguageModel.for_inference(self.model)
        self.tools = {"recall_memory": recall, "search_code": search_code}
    def run_tool(self, tool_call):
        match = re.search(r"\[TOOL:\s*(\w+)\((['\"])(.*?)\2\)", tool_call)
        if match:
            name, query = match.group(1), match.group(3)
            if name in self.tools: return self.tools[name](query)
        return "Error: Invalid tool call."
    def chat(self, user_input):
        current_prompt = f"<start_of_turn>user\n{SYSTEM_PROMPT}\n\n{user_input}<end_of_turn>\n<start_of_turn>model\n"
        for _ in range(5):
            inputs = self.tokenizer([current_prompt], return_tensors="pt").to("cuda")
            outputs = self.model.generate(**inputs, max_new_tokens=1024, use_cache=True)
            response = self.tokenizer.batch_decode(outputs)[0].split("<start_of_turn>model\n")[-1].strip()
            if "[TOOL:" in response:
                tool_call = re.search(r"\[TOOL:.*?\]", response).group(0)
                result = self.run_tool(tool_call)
                current_prompt += f"{response}\nObservation: {result}\n<start_of_turn>model\n"
            else:
                remember(f"User: {user_input}\nAssistant: {response}")
                return response
        return "Max iterations reached."
EOF

cat << 'EOF' > main.py
from core.orchestrator import LocalClawOrchestrator
if __name__ == "__main__":
    agent = LocalClawOrchestrator()
    print("\nLOCAL-CLAW ACTIVE AGENCY MODE\n")
    while True:
        try:
            user_input = input("You: ")
            if user_input.lower() in ['exit', 'quit']: break
            print(f"Gemma 4: {agent.chat(user_input)}\n")
        except KeyboardInterrupt: break
EOF

echo "✅ Folder structure aligned with specs. Project is now a structured Application."