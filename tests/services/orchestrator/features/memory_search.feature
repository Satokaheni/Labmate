@mocked
Feature: memory_search tool — queryable memory inside the ReAct loop
  As the single-intent ReAct loop
  I want a flat memory_search tool that retrieves prior context from vector memory
  So the model can recall earlier decisions mid-task instead of asking the user to repeat them

  Scenario: memory_search is absent from the tool list when no memory store is wired
    Given an AsyncOrchestrator with no skill router and no memory store
    When the prompt assembler builds the tool list with memory disabled
    Then the tool list does not contain a tool named "memory_search"

  Scenario: memory_search appears in the tool list when a memory store is wired
    Given an AsyncOrchestrator with no skill router and a memory store
    When the prompt assembler builds the tool list with memory enabled
    Then the tool list contains a tool named "memory_search"
    And the memory_search tool has a "query" parameter
    And the memory_search tool has a "k" parameter

  Scenario: the model calls memory_search and ranked snippets land in the loop as raw text
    Given an AsyncOrchestrator with no skill router and a memory store
    And the memory store returns the snippet "We chose Postgres over Mongo for billing."
    And the memory store returns the snippet "The retry budget was capped at 2 attempts."
    And the model calls memory_search with query "what database for billing" on turn 1
    And the model calls finish with summary "recalled the decision" on turn 2
    When react_execute runs the goal "continue the billing work"
    Then the memory_search tool result contains "Postgres over Mongo"
    And the memory_search tool result contains "retry budget was capped"

  Scenario: memory_search returns the snippets raw, not an LLM summary
    Given an AsyncOrchestrator with no skill router and a memory store
    And the memory store returns the snippet "Decision: use AsyncMongoDBSaver, never MemorySaver."
    And the model calls memory_search with query "checkpointer choice" on turn 1
    And the model calls finish with summary "done" on turn 2
    When react_execute runs the goal "recall the checkpointer decision"
    Then the memory_search tool result contains "AsyncMongoDBSaver, never MemorySaver"

  Scenario: memory_search reports an empty result clearly when memory has nothing
    Given an AsyncOrchestrator with no skill router and a memory store
    And the model calls memory_search with query "nonexistent topic" on turn 1
    And the model calls finish with summary "nothing found" on turn 2
    When react_execute runs the goal "recall an unknown thing"
    Then the memory_search tool result contains "no relevant memory found"
