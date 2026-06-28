Feature: Fix-loop headroom for edit/fix goals
  Edit/fix goals are inherently multi-step: edit, run tests, see a failure,
  edit again. The harness must not punish that legitimate retry. Mutating
  tools get a higher consecutive-repeat tolerance before the loop detector
  halts, verification/inspection turns are refunded so they do not starve
  the editing budget, and edit-intent goals run under a higher iteration
  ceiling. Read/inspect thrash still halts, and non-edit goals are unchanged.

  @mocked
  Scenario: A second identical write_file does NOT halt a mutating retry
    Given a loop detector with the default repeat limit
    When the mutating call "write_file" with arguments {"path": "a.py", "content": "x"} is recorded
    And the mutating call "write_file" with arguments {"path": "a.py", "content": "x"} is recorded
    Then the detector reports it should not break

  @mocked
  Scenario: A fourth identical write_file finally halts the mutating retry
    Given a loop detector with the default repeat limit
    When the mutating call "write_file" with arguments {"path": "a.py", "content": "x"} is recorded
    And the mutating call "write_file" with arguments {"path": "a.py", "content": "x"} is recorded
    And the mutating call "write_file" with arguments {"path": "a.py", "content": "x"} is recorded
    And the mutating call "write_file" with arguments {"path": "a.py", "content": "x"} is recorded
    Then the detector reports it should break
    And the trip reason mentions "repeat"

  @mocked
  Scenario: A true read-tool thrash still halts at the default limit
    Given a loop detector with the default repeat limit
    When the read call "read_file" with arguments {"path": "a.py"} is recorded
    And the read call "read_file" with arguments {"path": "a.py"} is recorded
    Then the detector reports it should break
    And the trip reason mentions "repeat"

  @mocked
  Scenario: A run_tests verification turn is refunded so it does not eat the budget
    Given an iteration budget with capacity 2
    When a "run_tests" turn is consumed and refunded
    And a "run_tests" turn is consumed and refunded
    Then 2 working turns still fit in the budget

  @mocked
  Scenario: An edit-intent goal runs under the higher iteration ceiling
    Given a ReAct orchestrator wired to a fake model that writes a file then finishes
    When the edit goal "fix the bug in app.py" is executed
    Then react_execute returns ok True
    And the model was allowed more than max_steps turns of headroom
