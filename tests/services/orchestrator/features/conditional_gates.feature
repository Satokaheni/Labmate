@mocked
Feature: Conditional gates skip ambiguity and verify for trivial tasks
  The orchestrator runs an LLM ambiguity gate on every task and a critique
  gate on every code/writing artifact. For clearly trivial, low-risk tasks
  this is wasted latency. A deterministic complexity classifier lets the graph
  skip those gates for trivial work while keeping them for everything else.
  The whole feature is OFF by default and must be a strict no-op when disabled.

  Background:
    Given the conditional-gates feature is enabled

  Scenario: A trivial arithmetic task skips both gates
    Given a task "What is 2+2?"
    When the orchestrator classifies the task complexity
    Then the classifier marks skip_ambiguity true
    And the classifier marks skip_verify true
    And the ambiguity gate is skipped so routing goes straight to plan
    And the verify gate is skipped so routing goes straight to check

  Scenario: An ambiguous task is still gated
    Given a task "make it better"
    When the orchestrator classifies the task complexity
    Then the classifier marks skip_ambiguity false
    And the ambiguity gate still runs and produces an ambiguity score

  Scenario: A code artifact for a non-trivial task is still verified
    Given a task "Implement a rate limiter with a sliding window and tests"
    When the orchestrator classifies the task complexity
    Then the classifier marks skip_verify false
    And the verify gate still runs and routes a low-scoring artifact to reflect

  Scenario: With the feature flag off, behavior is unchanged
    Given the conditional-gates feature is disabled
    And a task "What is 2+2?"
    When the orchestrator classifies the task complexity
    Then the classifier marks skip_ambiguity false
    And the classifier marks skip_verify false
    And the ambiguity gate still runs and produces an ambiguity score
