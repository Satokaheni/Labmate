# 🛠️ Spec: The Polyglot Skill Framework (S2)

## Core Objective
Implement a Model Context Protocol (MCP) bridge to allow the agent to use tools written in TypeScript, Rust, and Python, specifically integrating the Obra Superpowers framework for high-fidelity specification and implementation planning.

## Baseline Architecture
- **Client:** Python-based MCP Client in the Orchestrator.
- **Server:** Node.js MCP Server hosting individual skill modules.
- **Transport:** JSON-RPC over stdio.

---

## 🚀 The Superpower Skill (SOTA Integration)

### Tool: `invoke_superpower`
This tool allows Local-Claw to upgrade its own specifications using the Obra Superpowers framework.

- **Input:** `target_file` (path to base spec), `superpower_type` (e.g., "Architect", "Implementer", "Reviewer").
- **Logic:** 
    1. Load the corresponding prompt template from `/workspace/Work/local-claw/superpowers/`.
    2. Read the content of the `target_file`.
    3. Construct a "Supercharge Request" combining the template and the base spec.
    4. Process through Gemma 4 to generate the "Enhanced Spec."
    5. Save the result back to the file or a new `.enhanced` file.
- **Output:** The upgraded, high-fidelity specification.

---

## 🛠️ Other Tool Definitions

### 3.1 Coding Suite (Structural)
| Tool Name | Input | Logic | Output |
| :--- | :--- | :--- | :--- |
| `analyze_ast` | `file, symbol` | Uses `ts-morph` / `tree-sitter` for structural mapping. | Structural map of the symbol. |
| `apply_refactor` | `target, pattern` | AST-safe modification of source code. | Modified Code. |
| `generate_tests` | `function` | Invariant-based test generation. | Test File. |

### 3.2 Writing Suite (Professional)
| Tool Name | Input | Logic | Output |
| :--- | :--- | :--- | :--- |
| `style_morph` | `text, style` | Applies professional linguistic markers. | Styled text. |
| `draft_skeleton` | `topic, type` | Generates IMRaD or Professional outlines. | Markdown outline. |
| `cite_sync` | `text, bib` | BibTeX cross-referencing. | Citation Report. |

### 3.3 System Suite (Utility)
| Tool Name | Input | Logic | Output |
| :--- | :--- | :--- | :--- |
| `context_prune` | `prompt` | Priority-based token pruning. | Optimized Prompt. |
| `state_save` | `state` | JSON serialization of the Goal Tree. | checkpoint.json |