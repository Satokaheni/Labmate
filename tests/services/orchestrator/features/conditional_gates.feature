Feature: Conditional gates for trivial tasks

  The conditional-gates feature allows skipping the expensive ambiguity and verify
  LLM gates on clearly trivial tasks (e.g. "What is 2+2?"). When enabled, a
  deterministic classifier certifies trivial tasks and sets skip flags; the routers
  then short-circuit, saving latency. The feature is OFF by default.

  Background:
    Given the conditional-gates feature is enabled

  Scenario: Trivial question skips both gates
    Given a task "What is 2+2?"
    When the orchestrator classifies the task complexity
    Then the classifier marks skip_ambiguity true
    And the classifier marks skip_verify true
    And the ambiguity gate is skipped so routing goes straight to plan
    And the verify gate is skipped so routing goes straight to check

  Scenario: Ambiguous task requires ambiguity gate
    Given a task "make it better"
    When the orchestrator classifies the task complexity
    Then the classifier marks skip_ambiguity false
    And the ambiguity gate still runs and produces an ambiguity score

  Scenario: Artifact-producing task requires verify gate
    Given a task "Implement a rate limiter with a sliding window and tests"
    When the orchestrator classifies the task complexity
    Then the classifier marks skip_verify false
    And the verify gate still runs and routes a low-scoring artifact to reflect

  Scenario: Feature disabled preserves original behavior
    Given the conditional-gates feature is disabled
    And a task "What is 2+2?"
    When the orchestrator classifies the task complexity
    Then the classifier marks skip_ambiguity false
    And the classifier marks skip_verify false
