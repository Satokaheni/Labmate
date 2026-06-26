@mocked
Feature: Iteration budget with grace and cheap-call refund
  As the ReAct loop orchestrator
  I want to bound iterations with a grace turn after exhaustion
  And refund read-only turns so inspection does not starve work
  So that infinite loops are safely halted with a final chance to finish

  Scenario: exhaustion grants exactly one grace turn
    Given an AsyncOrchestrator with no skill router and no mcp
    And the iteration budget cap is 2
    And the model calls run_bash with command "echo 1" on turn 1
    And the model calls run_bash with command "echo 2" on turn 2
    And the model calls run_bash with command "echo 3" on turn 3
    When react_execute runs the goal "loop forever"
    Then the result ok is False
    And the result summary contains "budget exhausted"
    And the model was called exactly 3 times

  Scenario: grace turn can successfully finish
    Given an AsyncOrchestrator with no skill router and no mcp
    And the iteration budget cap is 1
    And the model calls run_bash with command "echo work" on turn 1
    And the model calls finish with summary "finished on grace" on turn 2
    When react_execute runs the goal "needs one more step"
    Then the result ok is True
    And the result summary contains "finished on grace"
    And the model was called exactly 2 times

  Scenario: read-only iterations are refunded
    Given an AsyncOrchestrator with no skill router and no mcp
    And the iteration budget cap is 2
    And the model calls list_dir with path "." on turn 1
    And the model calls run_bash with command "echo a" on turn 2
    And the model calls run_bash with command "echo b" on turn 3
    And the model calls finish with summary "done after refund" on turn 4
    When react_execute runs the goal "inspect then work twice"
    Then the result ok is True
    And the result summary contains "done after refund"
    And the model was called exactly 4 times

  Scenario: env var LABMATE_MAX_ITERATIONS overrides max_steps
    Given an AsyncOrchestrator with no skill router and no mcp
    And the iteration budget cap is 6
    And the LABMATE_MAX_ITERATIONS env var is set to "1"
    And the model calls run_bash with command "echo a" on turn 1
    And the model calls run_bash with command "echo b" on turn 2
    When react_execute runs the goal "loop with env override"
    Then the result ok is False
    And the result summary contains either "budget exhausted" or "absolute turn limit"
    And the model was called exactly 2 times
