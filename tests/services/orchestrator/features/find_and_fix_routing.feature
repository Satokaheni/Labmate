@mocked
Feature: Find-and-fix routing — classifier and dispatcher
  The orchestrator needs to distinguish between pure read/answer goals
  (which can be served by single-skill dispatch) and edit/fix/verify goals
  (which require the multi-tool ReAct loop so the model can interleave
  read + edit + run). A deterministic edit-intent classifier routes goals
  to the appropriate handler. The feature is gated by ROUTE_EDIT_TO_REACT
  and is enabled by default.

  Background:
    Given the routing feature flag is on

  Scenario Outline: Pure classifier — edit verbs flag True, read phrases flag False
    Then requires_editing for "<goal>" is <expected>

    Examples: edit verbs trigger ReAct routing
      | goal                                                    | expected |
      | Fix the off-by-one bug in factorial                    | True     |
      | Make the tests pass                                    | True     |
      | Refactor the parser to use a state machine             | True     |
      | Implement the missing validate() method                | True     |
      | Patch the regex so it accepts unicode                  | True     |
      | Review the code and then fix the bugs you find         | True     |
      | Resolve the failing assertion in test_app.py           | True     |
      | Make it work the import is broken                      | True     |
      | Generate tests for sort(), then find and fix the bug   | True     |
      | Edit config.py to add a timeout option                 | True     |
      | Correct the typo that breaks the build                 | True     |
      | Apply the fix and rerun the suite                      | True     |
      | Debug and repair the broken endpoint                   | True     |

    Examples: read/answer goals stay on skill-first dispatch
      | goal                                                    | expected |
      | Summarize what this module does                        | False    |
      | What is the capital of France?                         | False    |
      | Find bugs in the authentication handler                | False    |
      | Explain how the event loop works                       | False    |
      | Review this file for code smells                       | False    |
      | List the functions defined in utils.py                | False    |
      | What does the parse_header function return?           | False    |
      | Describe the architecture of the orchestrator          | False    |
      | Search for papers about retrieval augmentation         | False    |

    Examples: A/B test cases
      | goal                                                    | expected |
      | Generate unit tests for the factorial function, then find and fix the bug | True |
      | Review /workspace/ab_buggy.py for bugs, then fix the code | True |
      | Find bugs in /workspace/ab_review.py                   | False    |

  Scenario Outline: Feature flag off — all goals stay on skill-first dispatch
    Given the routing feature flag is off
    Then requires_editing for "<goal>" is False

    Examples: even edit goals return False when disabled
      | goal                  |
      | Fix the bug           |
      | Make the tests pass   |
      | Implement the method  |

  Scenario: Edit-intent goals bypass single-skill and enter ReAct
    Given a skill_first orchestrator whose skill router would match a read-only review skill
    And a fake model that reads, edits, and then finishes
    When the goal "Review the code then fix the bug" is executed
    Then the single-skill fast-path was NOT taken
    And the multi-tool ReAct loop ran
    And react_execute returns ok True

  Scenario: Read-only goals stay on the single-skill fast-path
    Given a skill_first orchestrator whose skill router returns a successful read-only result
    When the goal "Summarize what this module does" is executed
    Then the single-skill fast-path WAS taken
    And react_execute returns ok True
    And the summary is the skill result

  Scenario: Flag-off behavior — edit goals revert to skill-first dispatch
    Given the routing feature flag is off
    And a skill_first orchestrator whose skill router would match a read-only review skill
    When the goal "Fix the bug in factorial" is executed
    Then the single-skill fast-path WAS taken
    And react_execute returns ok True
