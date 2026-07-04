@mocked
Feature: First-class run_tests tool and reliable write_file
  As the single-intent ReAct loop
  I want a flat run_tests tool that runs a real test command and returns raw output
  And a write_file that verifies its own write by reading it back
  So the model can verify a fix instead of fabricating "all tests pass" or "code updated"

  Scenario: run_tests is always available in the loop tool list
    Given an AsyncOrchestrator with no skill router and no mcp
    When the prompt assembler builds the tool list
    Then the tool list contains a tool named "run_tests"
    And the run_tests tool has a "path" parameter

  Scenario: run_tests returns the real exit code and raw output on success
    Given an AsyncOrchestrator with no skill router and a stub bash seam
    And the bash seam returns exit code 0 with output "2 passed in 0.03s"
    And the model calls run_tests with path "tests/" on turn 1
    And the model calls finish with summary "tests pass" on turn 2
    When react_execute runs the goal "run the tests"
    Then the tool result json has ok True
    And the tool result json has exit_code 0
    And the tool result raw_output contains "2 passed"

  Scenario: a failing suite surfaces the real failure text, not a summary
    Given an AsyncOrchestrator with no skill router and a stub bash seam
    And the bash seam returns exit code 1 with output "E   assert 1 == 2\n1 failed in 0.02s"
    And the model calls run_tests with path "tests/test_math.py" on turn 1
    And the model calls finish with summary "saw failure" on turn 2
    When react_execute runs the goal "run the failing tests"
    Then the tool result json has ok False
    And the tool result json has exit_code 1
    And the tool result raw_output contains "assert 1 == 2"

  Scenario: write_file read-back catches a write that did not apply
    Given an AsyncOrchestrator with no skill router and a local tool client
    And the write_file client reports success but the file reads back as "OLD CONTENT"
    And the model calls write_file with path "src/app.py" and content "NEW CONTENT" on turn 1
    And the model calls finish with summary "wrote file" on turn 2
    When react_execute runs the goal "update the file"
    Then the write_file tool result contains "write verification failed"
    And the write_file tool result contains "did not match"

  Scenario: write_file read-back confirms a write that did apply
    Given an AsyncOrchestrator with no skill router and a local tool client
    And the write_file client reports success and the file reads back as "NEW CONTENT"
    And the model calls write_file with path "src/app.py" and content "NEW CONTENT" on turn 1
    And the model calls finish with summary "wrote file" on turn 2
    When react_execute runs the goal "update the file"
    Then the write_file tool result contains "verified"
    And the write_file tool result does not contain "verification failed"

  Scenario: run_tests that hangs past its timeout is killed and reported as exit code 124
    Given an AsyncOrchestrator with no skill router and a stub bash seam
    And the bash seam times out
    And the model calls run_tests with path "tests/" on turn 1
    And the model calls finish with summary "tests timed out" on turn 2
    When react_execute runs the goal "run the tests"
    Then the tool result json has ok False
    And the tool result json has exit_code 124
    And the tool result raw_output contains "timed out"
    And the subprocess was killed
