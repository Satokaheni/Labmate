@mocked
Feature: Verification-stop guard
  The ReAct loop must not accept a finish that claims completion after editing
  code without ever running tests and seeing them pass. When that happens the
  loop injects a synthetic user nudge and re-enters, capped at MAX_VERIFY_NUDGES;
  after the cap it accepts the finish but the summary is annotated honestly.

  Background:
    Given a verification-stop AsyncOrchestrator with no skill router and no mcp

  Scenario: Edit then finish without tests is nudged, then verifies and finishes
    Given MAX_VERIFY_NUDGES is "2"
    And the model writes file "src/app.py" on turn 1
    And the model calls finish with summary "I fixed the bug and all tests pass" on turn 2
    And the model calls run_tests on turn 3 with a passing result
    And the model calls finish with summary "tests pass" on turn 4
    When the verification-stop loop runs the goal "fix the bug in src/app.py"
    Then the result ok is True
    And the result summary contains "tests pass"
    And a verification nudge was injected exactly 1 time
    And the model was called exactly 4 times

  Scenario: Edit plus a passing test finishes immediately with no nudge
    Given MAX_VERIFY_NUDGES is "2"
    And the model writes file "src/app.py" on turn 1
    And the model calls run_tests on turn 2 with a passing result
    And the model calls finish with summary "done, tests pass" on turn 3
    When the verification-stop loop runs the goal "fix and verify src/app.py"
    Then the result ok is True
    And the result summary contains "done, tests pass"
    And a verification nudge was injected exactly 0 times

  Scenario: A goal that edits nothing finishes immediately with no nudge
    Given MAX_VERIFY_NUDGES is "2"
    And the model calls finish with summary "2 + 2 = 4" on turn 1
    When the verification-stop loop runs the goal "what is 2 plus 2"
    Then the result ok is True
    And the result summary contains "2 + 2 = 4"
    And a verification nudge was injected exactly 0 times

  Scenario: Cap respected — after MAX_VERIFY_NUDGES the finish is accepted honestly
    Given MAX_VERIFY_NUDGES is "1"
    And the model writes file "src/app.py" on turn 1
    And the model calls finish with summary "all done" on turn 2
    And the model calls finish with summary "still done" on turn 3
    When the verification-stop loop runs the goal "fix src/app.py"
    Then the result ok is True
    And a verification nudge was injected exactly 1 time
    And the result summary contains "not verified"
