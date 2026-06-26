@mocked
Feature: Live interrupt steering and cancel of a running ReAct turn
  A user can steer or cancel an agent mid-turn. A steer typed during a turn is
  delivered to the model as a genuine out-of-band user instruction on the next
  turn; a cancel halts the loop with an honest partial summary. With neither
  signal present the loop is unchanged, and a steer is consumed exactly once.

  Background:
    Given a ReAct orchestrator wired to a fakeredis steer/cancel channel
    And the active task id is "task-steer-1"

  Scenario: A steer written mid-loop is injected as an out-of-band user message on the next turn
    Given the model will call run_bash then finish over two turns
    And the user writes the steer "stop editing app.py, work on db.py instead" before the second turn
    When react_execute runs the goal "refactor the project"
    Then the messages sent on the second model call contain an out-of-band user message
    And that message wraps the steer text in the out-of-band marker
    And the steer key "labmate:steer:task-steer-1" is empty afterward

  Scenario: A cancel written mid-loop halts the loop with an honest partial summary
    Given the model will call run_bash on every turn
    And the user cancels task "task-steer-1" before the second turn
    When react_execute runs the goal "do a long job"
    Then react_execute returns ok False
    And the summary mentions it was cancelled
    And the model was called fewer times than max_steps

  Scenario: With no steer and no cancel the loop is unchanged
    Given the model will call finish on the first turn with summary "all done"
    When react_execute runs the goal "trivial task"
    Then react_execute returns ok True
    And the summary is "all done"
    And no out-of-band user message was injected

  Scenario: A steer is consumed exactly once
    Given the model will call run_bash on three turns then finish
    And the user writes the steer "use the staging database" before the second turn
    When react_execute runs the goal "multi-step job"
    Then exactly one model call carried an out-of-band user message
    And the steer key "labmate:steer:task-steer-1" is empty afterward
