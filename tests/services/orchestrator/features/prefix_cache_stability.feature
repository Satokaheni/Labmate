@mocked
Feature: Prefix-cache stability across ReAct steps
  llama-server caches the longest common prefix of a prompt. To benefit, Labmate
  must send a byte-identical system+tools prefix on every step of one goal, and
  only ever append new messages. This feature locks that contract in place.

  Background:
    Given a fake OpenAI-compatible model that records every request body
    And an AsyncOrchestrator with no skill router and no MCP bridge

  Scenario: Two consecutive ReAct steps share a byte-identical system+tools prefix
    Given the model is scripted to call run_bash on step 1 then finish on step 2
    When react_execute runs the goal "inspect the repo then finish"
    Then the model received at least 2 requests
    And the system message of request 2 equals the system message of request 1
    And the tools list of request 2 equals the tools list of request 1
    And the serialized system+tools prefix of request 2 is byte-identical to request 1

  Scenario: Appended messages do not alter the prefix
    Given the model is scripted to call run_bash on step 1 then finish on step 2
    When react_execute runs the goal "inspect the repo then finish"
    Then request 2 has strictly more messages than request 1
    And the messages of request 1 are a prefix of the messages of request 2
    And the first message of every request is the identical system message

  Scenario: Tool ordering is stable across independent runs
    Given a second AsyncOrchestrator built with the same configuration
    When react_execute runs the goal "do nothing then finish" on each orchestrator
    Then the tools list sent by both orchestrators is identical in order and content
    And the serialized system+tools prefix is byte-identical between the two runs
