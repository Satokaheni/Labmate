@mocked
Feature: Wall-clock deadline and no-progress breaker for the ReAct loop
  As the ReAct loop orchestrator
  I want a per-goal wall-clock deadline and a no-progress idle breaker
  beyond plain step counting
  So that a stalled or runaway loop hard-stops with a clear message
  And a normal productive loop is never affected

  Scenario: a loop that exceeds the wall-clock deadline stops
    Given an AsyncOrchestrator with no skill router and no mcp
    And the iteration budget cap is 10
    And the wall-clock deadline is 4 seconds
    And the no-progress limit is 0
    And the fake clock advances 2 seconds per turn
    And the model calls run_bash with command "echo 1" on turn 1
    And the model calls run_bash with command "echo 2" on turn 2
    And the model calls run_bash with command "echo 3" on turn 3
    When react_execute runs the goal "spin past the deadline"
    Then the result ok is False
    And the result summary contains "wall-clock deadline exceeded"
    And the model was called exactly 2 times

  Scenario: N consecutive no-progress turns trip the breaker
    Given an AsyncOrchestrator with no skill router and no mcp
    And the iteration budget cap is 20
    And the wall-clock deadline is 0 seconds
    And the no-progress limit is 3
    And the fake clock advances 0 seconds per turn
    And the model returns an empty no-progress turn every turn
    When react_execute runs the goal "make no progress forever"
    Then the result ok is False
    And the result summary contains "no-progress breaker tripped"
    And the model was called exactly 3 times

  Scenario: a productive loop finishes normally untouched
    Given an AsyncOrchestrator with no skill router and no mcp
    And the iteration budget cap is 10
    And the wall-clock deadline is 600 seconds
    And the no-progress limit is 5
    And the fake clock advances 1 seconds per turn
    And the model calls run_bash with command "echo work" on turn 1
    And the model calls finish with summary "all done" on turn 2
    When react_execute runs the goal "do real work then finish"
    Then the result ok is True
    And the result summary contains "all done"
    And the model was called exactly 2 times
