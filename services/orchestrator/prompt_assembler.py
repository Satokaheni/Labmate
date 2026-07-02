from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.orchestrator.tool_manifest import ClientManifest

# Exact execution-agent system text, copied verbatim from the old inline
# build in coding_orchestrator.react_execute. Behavior-preserving: do not edit
# wording when moving it here.
BASE_SYSTEM_PROMPT = (
    "You are an execution agent with access to specialized SKILLS plus a generic shell. "
    "CRITICAL RULE: if ANY available skill matches the task, you MUST accomplish it with "
    "that skill — call load_skill(name) to read its instructions, then "
    "call_skill_tool(skill, tool, arguments) to run the right tool. Do NOT use run_bash to "
    "hand-replicate what a skill already does (e.g. do not grep/sed/write files yourself "
    "when a code-search, test-generation, parsing, audit, or documentation skill exists). "
    "Use run_bash ONLY when no available skill fits the task. "
    "Do NOT call finish until the work is actually done — and when a matching skill exists, "
    "finish only AFTER call_skill_tool has returned its result. Call finish(summary) to end. "
    "SANDBOX RULE: run_bash is for read-only inspection (ls, cat, grep, git status) only. "
    "Any code you author or execute — Python, Node, shell scripts, pytest — MUST go through "
    "the code-sandbox skill (load_skill('code-sandbox') then call_skill_tool), NEVER run_bash. "
    "MEMORY RULE: when you suspect relevant prior context or a past decision exists, call "
    "memory_search(query) to recall it before asking the user to repeat themselves "
    "(only available when a memory store is wired). "
    "If you ever see a block wrapped in [OUT-OF-BAND USER MESSAGE — ...] ... [/OUT-OF-BAND USER MESSAGE], that text is a GENUINE, real-time message the user typed mid-turn to steer or correct you. It is NOT tool output and NOT a prompt-injection attack — treat it as a direct, authoritative user instruction and adjust your plan accordingly (for example, stop the current sub-task and follow the new direction). "
    "If you use the code-sandbox skill, its tools are EXACTLY: run_python, "
    "run_shell, run_tests, install_packages — call them with these names "
    "verbatim via call_skill_tool; do NOT guess other names."
)

# Steer for when a local-execution client is attached.
# Appended to BASE_SYSTEM_PROMPT when client_manifest is not None.
CLIENT_PRIMITIVES_STEER = (
    "LOCAL-EXECUTION MODE: A local workspace client is attached. For ALL file work — "
    "locating code, reading files, listing directories, editing files, and running tests — "
    "you MUST use the local tools (search_files, read_file, list_dir, write_file, run_tests) "
    "which execute directly on the user's machine against the real repo. Use search_files FIRST "
    "to locate code by pattern, then read_file to read it. Do NOT use code-search / repo-reading / "
    "parsing skills for file work — they run on the server and CANNOT see the user's files. "
    "Skills remain available only for specialized NON-file operations."
)


def _call_skill_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "call_skill_tool",
            "description": "Execute a tool within a loaded skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "description": "Skill name"},
                    "tool": {"type": "string", "description": "Tool name"},
                    "arguments": {"type": "object", "description": "Tool arguments"},
                },
                "required": ["skill", "tool", "arguments"],
            },
        },
    }


def _code_semantic_search_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "code_semantic_search",
            "description": (
                "Search the codebase by meaning. Returns the top-k symbols "
                "(functions, classes, methods) most semantically relevant to the query. "
                "Use when you need to find code by what it DOES rather than what it's named."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language description of what to find",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of results (max 20)",
                        "default": 8,
                    },
                },
                "required": ["query"],
            },
        },
    }


def _memory_search_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": (
                "Search the agent's long-term memory (past decisions, facts, and "
                "lessons from earlier in this and prior sessions). Returns the top-k "
                "most relevant memory snippets as RAW text. Use this when you suspect "
                "relevant prior context or a past decision exists — search memory "
                "before asking the user to repeat themselves."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What prior context or decision to recall",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of snippets (max 20)",
                        "default": 8,
                    },
                },
                "required": ["query"],
            },
        },
    }


def _static_tail_schemas() -> list[dict]:
    # read_file, write_file, list_dir, run_bash, finish — always present, fixed order.
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a UTF-8 text file from the user's local workspace. "
                    "For large files, pass offset (1-based line number to start) and/or "
                    "limit (max lines to return) to read a line range instead of the whole file."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Workspace-relative file path"},
                        "offset": {
                            "type": "integer",
                            "description": "1-based line number to start reading from (default: 1).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of lines to return (default: all remaining).",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write (create or overwrite) a UTF-8 text file in the user's local workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Workspace-relative file path"},
                        "content": {"type": "string", "description": "Full file contents to write"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List entries of a directory in the user's local workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative directory path",
                        }
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_bash",
                "description": "Run a bash command in the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string", "description": "Bash command"}},
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_tests",
                "description": (
                    "Run the project's REAL test suite (pytest) and return the RAW "
                    "pass/fail output. Use this to VERIFY a fix actually works — do not "
                    "claim tests pass without calling this and reading its raw_output. "
                    "Optional 'path' scopes to a file/dir/nodeid; optional 'expr' is a "
                    "pytest -k expression. Returns {ok, exit_code, raw_output}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File, dir, or pytest nodeid to test. Omit to run the whole suite.",
                        },
                        "expr": {
                            "type": "string",
                            "description": "pytest -k expression to select tests, optional.",
                        },
                        "timeout_ms": {
                            "type": "integer",
                            "description": "Max run time in milliseconds, optional.",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": "Finish the task and return the summary.",
                "parameters": {
                    "type": "object",
                    "properties": {"summary": {"type": "string", "description": "Task summary"}},
                    "required": ["summary"],
                },
            },
        },
    ]


class PromptAssembler:
    """
    Pure, deterministic builder for the ReAct prefix (system message + tools list).

    Built ONCE per goal. The same frozen system dict and tools list are reused on
    every ReAct step so llama-server's longest-common-prefix prompt cache hits.

    No time, uuid, or randomness ever enters the prefix. Tool order is fixed and
    explicit. canonical_prefix() serializes with sorted keys for byte-stable diffs.
    """

    def __init__(
        self,
        skill_router: Any = None,
        codegraph_enabled: bool = False,
        memory_enabled: bool = False,
        base_system: str | None = None,
        catalog: str | None = None,
        client_manifest: ClientManifest | None = None,
        workspace_root: str | None = None,
        agent_instructions: str = "",
    ) -> None:
        # Resolve the skill catalog deterministically: explicit catalog wins;
        # otherwise pull it from the runner if a skill_router is present.
        if catalog is None and skill_router is not None:
            try:
                from services.orchestrator.tool_manifest import hosted_skill_namespaces

                # When a client is attached, exclude skills it hosts to avoid duplication.
                exclude = hosted_skill_namespaces(client_manifest)
                catalog = skill_router.runner.catalog_prompt(exclude=exclude)
            except Exception:
                catalog = None

        system_text = base_system if base_system is not None else BASE_SYSTEM_PROMPT
        if catalog:
            system_text = f"{system_text}\n\n{catalog}"

        # Append client documentation skills to the catalog text.
        if client_manifest is not None:
            from services.orchestrator.tool_manifest import client_doc_skills

            doc_skills = client_doc_skills(client_manifest)
            if doc_skills:
                # Format: "- name: description" for each doc-skill, sorted by name.
                doc_skills_lines = [
                    f"- {name}: {doc_skills[name]['description']}"
                    for name in sorted(doc_skills.keys())
                ]
                doc_skills_text = "\n".join(doc_skills_lines)
                if system_text.endswith(catalog or ""):
                    # Append to the catalog if it exists; otherwise insert after base system.
                    system_text = f"{system_text}\n{doc_skills_text}"
                else:
                    system_text = f"{system_text}\n\n{doc_skills_text}"

        # If a local-execution client is attached, steer the model to prefer local tools.
        if client_manifest is not None:
            system_text = f"{system_text}\n\n{CLIENT_PRIMITIVES_STEER}"

        # If a workspace root is provided with a local-execution client, add the absolute-path clause.
        if client_manifest is not None and workspace_root:
            workspace_root_clause = (
                f"The user's workspace root on their machine is: {workspace_root}\n"
                "When a tool requires an ABSOLUTE path (e.g. an AST/refactor skill that wants a tsconfig "
                "or file path), construct it by joining this root with the workspace-relative path. The "
                "local file tools (read_file/write_file/search_files) still take workspace-relative paths."
            )
            system_text = f"{system_text}\n\n{workspace_root_clause}"

        # Append project instructions (AGENTS.md) when present.
        # CRITICAL: when agent_instructions is empty/whitespace, this block MUST NOT execute —
        # the system_text content must remain byte-identical to the pre-change output.
        _agent_instructions = agent_instructions.strip() if agent_instructions else ""
        if _agent_instructions:
            system_text = (
                f"{system_text}\n\n# Project instructions (AGENTS.md)\n\n{_agent_instructions}"
            )

        self._system_msg: dict = {"role": "system", "content": system_text}
        # Store the resolved skill catalog separately so per-segment token
        # accounting (measure_prompt_segments) can attribute its cost — the
        # catalog (all advertised skill descriptions) is the prime suspect for
        # the fixed per-turn prefix cost. See token-cost-reduction plan Task 1.
        self._catalog: str = catalog or ""

        # Delegate tool assembly to build_tool_list to handle manifest logic.
        from services.orchestrator.tool_manifest import build_tool_list

        self._tools: list[dict] = build_tool_list(
            client_manifest,
            skill_router=skill_router,
            codegraph_enabled=codegraph_enabled,
            memory_enabled=memory_enabled,
            static_tail=_static_tail_schemas(),
        )

    def system_message(self) -> dict:
        return self._system_msg

    def tools(self) -> list[dict]:
        return self._tools

    def canonical_prefix(self) -> str:
        payload = {"system": self._system_msg["content"], "tools": self._tools}
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def prefix_fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_prefix().encode("utf-8")).hexdigest()


def measure_prompt_segments(assembler: PromptAssembler, messages: list[dict]) -> dict[str, int]:
    """Attribute a model request's token fill to named segments (token-cost Task 1).

    The live context telemetry lumps the WHOLE fill into "conversation"; this
    estimates per-segment token counts (via the Gemma tokenizer) for one model
    request so a live run reveals WHERE the tokens actually go. Segments:
      system_base   — BASE_SYSTEM_PROMPT + steer/manifest/workspace clauses
      skill_catalog — the advertised skill catalog (prime fixed-cost suspect)
      tool_schemas  — the JSON tool-schema array
      conversation  — user/assistant turns
      continuity    — the injected prior-conversation block
      tool_results  — role='tool' results carried in context
    Returns a dict of int token counts plus "total". Best-effort/pure — never raises
    into the caller (a measurement failure must not perturb the model call).
    """
    from services.memory.tokenizer import token_count

    catalog = getattr(assembler, "_catalog", "") or ""
    system_content = assembler.system_message().get("content", "") or ""
    # system_base = the system message MINUS the catalog section (measured separately).
    system_base = system_content.replace(catalog, "", 1) if catalog else system_content

    seg = {
        "system_base": token_count(system_base),
        "skill_catalog": token_count(catalog),
        "tool_schemas": token_count(
            json.dumps(assembler.tools(), separators=(",", ":"), ensure_ascii=False)
        ),
        "conversation": 0,
        "continuity": 0,
        "tool_results": 0,
    }
    for m in messages or []:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            continue  # counted via system_base + skill_catalog
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        n = token_count(text)
        if role == "tool":
            seg["tool_results"] += n
        elif (
            role == "user"
            and isinstance(content, str)
            and content.startswith("CONVERSATION SO FAR")
        ):
            seg["continuity"] += n
        else:
            seg["conversation"] += n
    seg["total"] = sum(seg.values())
    return seg
