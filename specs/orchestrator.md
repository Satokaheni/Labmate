# 🧠 Spec: The High-Reasoning Orchestrator (S1)

## 1. Objective
To transition Local-Claw from a reactive agent (User $\rightarrow$ Tool $\rightarrow$ Answer) to a proactive agent (User $\rightarrow$ Plan $\rightarrow$ Execute $\rightarrow$ Verify $\rightarrow$ Answer). The goal is to ensure high-fidelity execution of complex, multi-step tasks in coding and academic writing without "goal drift" or reasoning loops.

## 2. The Cognitive Architecture
The orchestrator is divided into three distinct layers of reasoning to decouple strategic planning from tactical execution.

### Layer A: The Planner (Strategic Level)
The Planner manages the "Global Goal." It does not attempt to solve the problem; it only maps the path to the solution.
- **Input:** User Request + Current Project Context + `MISSION.md`.
- **Process:** Decomposes the high-level request into a sequence of dependent sub-tasks.
- **Output:** A JSON-formatted **Goal Tree**.
- **Pattern:** *Plan-and-Execute*.
- **Key Responsibility:** Breaking a request like *"Refactor the auth module and write a conference paper summary"* into separate, manageable tasks.

### Layer B: The Executor (Tactical Level)
The Executor is responsible for solving one specific task from the Goal Tree at a time.
- **Input:** Active Task + Available Toolset.
- **Process:** Employs a **Tree-of-Thoughts (ToT)** approach:
    1. **Candidate Generation:** Generate 3 distinct reasoning paths to solve the current task.
    2. **Evaluation:** Score each path based on expected utility and structural constraints.
    3. **Execution:** Implement the highest-scoring path using the provided tools.
- **Pattern:** *Tree-of-Thoughts*.

### Layer C: The Monitor (Verification Level)
The Monitor acts as an internal quality gate, preventing hallucinations and incorrect assumptions from reaching the user.
- **Input:** Execution Result + Original Goal.
- **Process:** Performs a "Consistency Check." It asks: *"Does the result of this task actually move us closer to the Global Goal, or did the agent get distracted?"*
- **Output:** APPROVED or REJECTED.
- **Action:** A REJECTED status triggers an immediate re-planning phase in Layer A.

---

## 3. Technical Implementation Details

### 3.1 The State Machine (Session State)
To maintain continuity, the agent must operate on a persistent **State Object** rather than a simple conversation string:

{
    "session_id": "string",
    "global_goal": "The original user request",
    "goal_tree": [
        {
            "id": 1, 
            "task": "Analyze AST of auth_manager.py", 
            "status": "completed", 
            "result": "Found 3 decorators..."
        },
        {
            "id": 2, 
            "task": "Implement JWT rotation", 
            "status": "active", 
            "result": null
        }
    ],
    "working_memory": "Temporary context from the current ToT branch",
    "negative_constraints": ["Failed to find 'config.yaml' in /root"],
    "current_iteration": 0
}

### 3.2 Tool-Call Evolution
Standard tool calls are upgraded to include **Intent**. This allows the Monitor to verify *why* a tool was used, not just *that* it was used.

- **New Format:** [ACTION: tool_name("query") | INTENT: "Reason for using this tool"]
- **Example:** [ACTION: search_code("database_connection") | INTENT: "I need to verify the port number before updating the .env file"]

### 3.3 The Reasoning Algorithm (The Loop)
1. **Initialization:** Receive User Input $\rightarrow$ Update State $\rightarrow$ Call **Planner**.
2. **Planning:** Generate JSON Goal Tree $\rightarrow$ Save to State.
3. **Execution Loop:**
    - Identify the next `pending` task in the Goal Tree.
    - Enter **ToT Cycle**: (Thought $\rightarrow$ Branch $\rightarrow$ Evaluate $\rightarrow$ Tool Call).
    - Capture the tool output and update `goal_tree[id].result`.
4. **Verification:** Pass the result to the **Monitor**.
    - If APPROVED $\rightarrow$ Mark task `completed`.
    - If REJECTED $\rightarrow$ Return to Step 2 (Re-plan based on failure).
5. **Synthesis:** Once all tasks are `completed`, synthesize the results into the final user response.

---

## 4. Research-Driven Enhancements

### 4.1 Negative Constraints (Anti-Looping)
To prevent "infinite loops" (e.g., searching for the same file with slightly different names), the agent maintains a list of **Negative Constraints**. If a tool returns "Not Found," that failure is recorded. The Planner is then explicitly prompted: *"Do not attempt the following failed queries: [List of Negative Constraints]."*

### 4.2 Adaptive Reasoning Depth
To optimize GPU tokens and latency:
- **Trivial Tasks:** (e.g., "Who is the author?") $\rightarrow$ Skip ToT, use direct response.
- **Complex Tasks:** (e.g., "Implement a new API endpoint") $\rightarrow$ Full Tree-of-Thoughts branching.

---

## 5. Success Metrics
- [ ] **Goal Stability:** The agent completes multi-step tasks without requiring user reminders of the original goal.
- [ ] **Failure Recovery:** The agent recognizes a failed tool call and pivots to a different strategy automatically.
- [ ] **Transparency:** The agent can output its current "Goal Tree" on request, showing the user exactly where it is in the process.