Feature: Raw tool-output grounding (budget-aware, not summaries)
  The weak local model can only judge whether an edit applied or whether tests
  actually passed if it SEES the real tool output. The ReAct executor must feed
  tool results back verbatim when they fit a generous byte budget, and only on
  genuine overflow keep a head AND a tail (joined by a clear truncation marker)
  so the end-of-output evidence — FAILED lines, assert messages, tracebacks —
  always reaches the model instead of being cut at 2-4k chars.

  @mocked
  Scenario: Output under budget reaches the model verbatim
    Given a tool output of 500 characters
    And a tool-result budget of 16000 characters
    When the output is grounded
    Then the grounded text equals the original output exactly
    And the grounded text contains no truncation marker

  @mocked
  Scenario: Output exactly at the budget is still verbatim
    Given a tool output of 16000 characters
    And a tool-result budget of 16000 characters
    When the output is grounded
    Then the grounded text equals the original output exactly
    And the grounded text contains no truncation marker

  @mocked
  Scenario: Over-budget output keeps a head and a tail with a marker
    Given a tool output of 40000 characters
    And a tool-result budget of 16000 characters
    When the output is grounded
    Then the grounded text is no longer than the budget plus the marker
    And the grounded text starts with the head of the original output
    And the grounded text ends with the tail of the original output
    And the grounded text contains a truncation marker reporting the dropped char count

  @mocked
  Scenario: A long failing-test output reaches the model with its FAILED lines intact
    Given a tool output that is a long passing-test preamble followed by a FAILED assertion at the very end
    And a tool-result budget of 16000 characters
    When the output is grounded
    Then the grounded text contains the FAILED assertion line
    And the grounded text contains the assert detail line

  @mocked
  Scenario: A small bash result flows verbatim into the ReAct tool message
    Given a ReAct orchestrator wired to a fake model that runs one bash command then finishes
    And the bash command returns "hello world" as its only output
    When the goal "echo hello" is executed
    Then the tool message appended to the model context contains "hello world" verbatim
    And the tool message contains no truncation marker

  @mocked
  Scenario: A huge bash result is grounded with a head, a tail, and a marker before reaching the model
    Given a ReAct orchestrator wired to a fake model that runs one bash command then finishes
    And the bash command returns 50000 characters ending in "TAILSENTINEL"
    When the goal "dump log" is executed
    Then the tool message appended to the model context contains a truncation marker
    And the tool message appended to the model context contains "TAILSENTINEL"
