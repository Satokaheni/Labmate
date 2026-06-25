@mocked
Feature: Stateful reflection collects prior attempts and conditions next diagnosis
  As the orchestrator
  I want to track prior diagnoses for each failed goal
  So that retries can build on past strategies instead of repeating them.

  Scenario: First failure has no prior section in diagnosis
    Given a goal "g1" that has failed once with error "AssertionError: expected 4 got 5"
    And the state has no prior reflections for goal "g1"
    When the reflect node runs
    Then the diagnosis prompt does not contain a "previously tried" section
    And the diagnosis prompt contains "Compute 2+2"
    And the diagnosis prompt contains "AssertionError: expected 4 got 5"

  Scenario: First failure emits reflection tagged with goal_id
    Given a goal "g1" that has failed once with error "AssertionError: expected 4 got 5"
    And the state has no prior reflections for goal "g1"
    When the reflect node runs
    Then the emitted reflection message is tagged with goal_id "g1"

  Scenario: Second failure includes prior reflection and forbids repeat
    Given a goal "g1" that has failed twice with error "AssertionError: still wrong"
    And a prior reflection for goal "g1" saying "Try converting the input to int before adding"
    When the reflect node runs
    Then the diagnosis prompt contains "Try converting the input to int before adding"
    And the diagnosis prompt contains "Previously tried"
    And the diagnosis prompt instructs the model not to repeat a previously-tried fix

  Scenario: Collect prior reflections for a goal
    Given a goal "g1" with 5 prior reflections numbered "1" through "5"
    When prior reflections are collected for goal "g1"
    Then exactly 3 reflections are returned
    And they are "fix 3", "fix 4", and "fix 5" in that order

  Scenario: Collect a single prior reflection
    Given a goal "g1" that has failed once with error "test"
    And a prior reflection "single fix attempt" for goal "g1"
    When prior reflections are collected for goal "g1"
    Then exactly 1 reflections are returned
    And it is "single fix attempt"
