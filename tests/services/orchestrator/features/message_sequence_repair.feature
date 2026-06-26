@mocked
Feature: Message-sequence repair before the model call
  As the ReAct loop orchestrator
  I want every message list repaired right before the inference call
  So that orphaned tool results and injected synthetic turns never wedge the provider

  Scenario: an orphaned tool result is dropped before the call
    Given a message list with an orphaned tool result
    When the messages are sanitized
    Then the orphaned tool result is gone
    And the system and user prefix are unchanged
    And the sanitized list validates clean

  Scenario: a valid edit then tool then finish sequence is unchanged
    Given a well-formed edit-tool-finish message list
    When the messages are sanitized
    Then the sanitized list is identical to the input
    And the sanitized list validates clean

  Scenario: an injected synthetic user turn after a tool result stays valid
    Given a message list with a synthetic user turn injected after a tool result
    When the messages are sanitized
    Then the sanitized list is identical to the input
    And the sanitized list validates clean

  Scenario: the react loop never hands a malformed list to the model
    Given an AsyncOrchestrator with no skill router and a stub mcp
    And the model calls run_bash on turn 1 then finish on turn 2
    When the react loop runs the goal "do work"
    Then every message list the model received validates clean
    And the result ok is True
