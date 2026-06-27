@mocked
Feature: OK/answer reconciliation (completion guard)
  The ok flag must agree with the final answer. A terminal punt ("file too
  large / provide a snippet") must never be ok=True, and an "I fixed it"
  success claim that was not backed by a passing test run this turn must be
  downgraded to ok=False with an honesty caveat. A neutral honest answer, and
  a success claim that WAS verified, are left ok=True unchanged.

  Background:
    Given a reconciliation AsyncOrchestrator with no skill router and no mcp

  Scenario: A punt finish is reported as not-ok
    Given the model calls finish with summary "I could not analyze the file because it is too large; provide a smaller snippet" on turn 1
    When the reconciliation loop runs the goal "find the bug in huge.py"
    Then the reconciled ok is False
    And the reconciled summary contains "too large"

  Scenario: An unverified fix claim is downgraded with a caveat
    Given the model calls finish with summary "I fixed the off-by-one bug and all tests pass" on turn 1
    When the reconciliation loop runs the goal "fix the factorial bug"
    Then the reconciled ok is False
    And the reconciled summary contains "completion-guard"

  Scenario: A neutral honest answer stays ok
    Given the model calls finish with summary "Here is the square function you asked for" on turn 1
    When the reconciliation loop runs the goal "write a square function"
    Then the reconciled ok is True
    And the reconciled summary does not contain "completion-guard"
