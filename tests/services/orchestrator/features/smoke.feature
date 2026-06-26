@mocked
Feature: BDD harness smoke test
  As the Labmate test suite
  I want pytest-bdd to bind a Gherkin scenario to Python steps
  So that later feature plans can rely on the harness wiring

  Scenario: a programmed model returns the answer we set
    Given the model is programmed to answer "2 plus 2 is 4"
    When the orchestrator asks the model a question
    Then the model reply is "2 plus 2 is 4"

  Scenario: a programmed model returns a tool call
    Given the model is programmed to call tool "list_dir" with path "."
    When the orchestrator asks the model a question
    Then the model requests tool "list_dir"
