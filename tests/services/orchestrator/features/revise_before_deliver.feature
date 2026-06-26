@mocked
Feature: Revise the final answer before delivery
  As the orchestrator, before delivering a final answer I make at most one
  bounded model call to re-read the answer against the task and optionally
  replace it with a revised version — but only when it is safe and enabled.

  Background:
    Given the finalize-revision feature is enabled
    And the maximum finalize revisions is 1

  Scenario: An answer that needs revision is revised once before delivery
    Given a finalized state for task "List the prime numbers under 10"
    And the finalized answer is "2, 3, 5"
    And no side-effecting tools ran during the task
    And the run did not error
    And the revision model will return "2, 3, 5, 7"
    When the revise node runs
    Then the delivered final answer is "2, 3, 5, 7"
    And the revision model was called exactly 1 time
    And the finalize revision count is 1

  Scenario: An answer after a side-effecting tool run is NOT revised
    Given a finalized state for task "Create report.txt with the summary"
    And the finalized answer is "Created report.txt with the summary."
    And side-effecting tools ran during the task
    And the run did not error
    When the revise node runs
    Then the delivered final answer is "Created report.txt with the summary."
    And the revision model was called exactly 0 times

  Scenario: The revision cap is respected — at most MAX_FINALIZE_REVISIONS
    Given a finalized state for task "List the prime numbers under 10"
    And the finalized answer is "2, 3, 5"
    And no side-effecting tools ran during the task
    And the run did not error
    And the finalize revision count is already 1
    When the revise node runs
    Then the revision model was called exactly 0 times
    And the delivered final answer is "2, 3, 5"

  Scenario: The disabled flag delivers the answer unchanged
    Given the finalize-revision feature is disabled
    And a finalized state for task "List the prime numbers under 10"
    And the finalized answer is "2, 3, 5"
    And no side-effecting tools ran during the task
    And the run did not error
    When the revise node runs
    Then the delivered final answer is "2, 3, 5"
    And the revision model was called exactly 0 times

  Scenario: No visible answer means no revision
    Given a finalized state for task "Do something"
    And the finalized answer is ""
    And no side-effecting tools ran during the task
    And the run did not error
    When the revise node runs
    Then the revision model was called exactly 0 times

  Scenario: An errored run is not revised
    Given a finalized state for task "Fetch the data"
    And the finalized answer is "Failed subtasks: fetch (error: connection refused)"
    And no side-effecting tools ran during the task
    And the run errored with "1 subtask(s) failed"
    When the revise node runs
    Then the revision model was called exactly 0 times
    And the delivered final answer is "Failed subtasks: fetch (error: connection refused)"
