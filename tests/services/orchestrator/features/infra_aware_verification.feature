@mocked
Feature: Infra-error-aware verification stop
  The agent must not nudge "run the tests" forever when the test toolchain
  cannot run, and must finish with an honest UNVERIFIED note.

  Scenario: Broken test toolchain yields an honest unverified finish
    Given a ReAct orchestrator with a broken test toolchain
    And the agent edits a file and then calls run_tests twice
    When the agent attempts to finish
    Then the final summary marks the result as unverified
    And the final summary does not claim the tests passed
    And the unverified note contains the specific infra reason
