Feature: De-duplicate load_skill calls within a single goal
  On the Q4 31B model the ReAct loop wastes iteration budget by re-loading
  skills it has already loaded this goal — leaving too few steps to actually
  read, edit, test, and verify. The loop now records which skills it has
  loaded this goal: a FIRST load runs the real loader and is charged a turn,
  but a REPEAT load of an already-loaded skill is short-circuited with a clear
  "already loaded — call its tools directly" message and the wasted iteration
  is refunded. Non-load tools and first loads are unaffected.

  @mocked
  Scenario: The pure guard flags a repeat load and passes a first load
    Given the set of loaded skills is "code-review"
    Then is_repeat_load for "code-review" is True
    And is_repeat_load for "test-gen" is False

  @mocked
  Scenario: The already-loaded message names the skill and lists loaded skills
    Given the set of loaded skills is "code-review,test-gen"
    When the already-loaded message is built for "code-review"
    Then the message text contains "already loaded"
    And the message text contains "code-review"
    And the message text contains "test-gen"
    And the message text contains "do not load_skill"

  @mocked
  Scenario: A repeat load_skill in the ReAct loop is short-circuited and refunded
    Given a ReAct orchestrator whose model loads "code-review" twice then finishes
    When the goal "review then fix the file" is executed
    Then the skill runner loaded "code-review" exactly once
    And the second load result reports it is already loaded
    And the iteration budget was refunded for the repeat load

  @mocked
  Scenario: A first load of a different skill is not short-circuited
    Given a ReAct orchestrator whose model loads "code-review" then "test-gen" then finishes
    When the goal "review then test the file" is executed
    Then the skill runner loaded "code-review" exactly once
    And the skill runner loaded "test-gen" exactly once
    And neither first load reported already loaded
