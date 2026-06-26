Feature: Tool-loop / no-progress detection in the ReAct executor
  On a weak local model the executor sometimes repeats the same tool call
  with the same arguments and burns every step. The loop detector spots a
  consecutive repeat or a short cycle of signatures and breaks the loop early
  with an honest failure, while never tripping on legitimately distinct calls.

  Background:
    Given a loop detector with the default repeat limit

  @mocked
  Scenario: A consecutive repeat of the same call trips the break
    When the call "run_bash" with arguments {"command": "ls"} is recorded
    And the call "run_bash" with arguments {"command": "ls"} is recorded
    Then the detector reports it should break
    And the trip reason mentions "repeat"

  @mocked
  Scenario: Argument key order does not matter for repeat detection
    When the call "call_skill_tool" with arguments {"skill": "x", "tool": "y"} is recorded
    And the call "call_skill_tool" with arguments {"tool": "y", "skill": "x"} is recorded
    Then the detector reports it should break

  @mocked
  Scenario: Distinct calls do not trip the break
    When the call "read_file" with arguments {"path": "a.txt"} is recorded
    And the call "read_file" with arguments {"path": "b.txt"} is recorded
    And the call "read_file" with arguments {"path": "c.txt"} is recorded
    Then the detector reports it should not break

  @mocked
  Scenario: A two-signature cycle with no new signature trips the break
    When the call "run_bash" with arguments {"command": "ls"} is recorded
    And the call "run_bash" with arguments {"command": "pwd"} is recorded
    And the call "run_bash" with arguments {"command": "ls"} is recorded
    And the call "run_bash" with arguments {"command": "pwd"} is recorded
    Then the detector reports it should break
    And the trip reason mentions "cycle"

  @mocked
  Scenario: A new signature after a near-cycle resets progress and does not trip
    When the call "run_bash" with arguments {"command": "ls"} is recorded
    And the call "run_bash" with arguments {"command": "pwd"} is recorded
    And the call "run_bash" with arguments {"command": "ls"} is recorded
    And the call "run_bash" with arguments {"command": "whoami"} is recorded
    Then the detector reports it should not break

  @mocked
  Scenario Outline: The repeat threshold is configurable
    Given a loop detector with repeat limit <limit>
    When the call "run_bash" with arguments {"command": "ls"} is recorded <count> times
    Then the detector should_break is <result>

    Examples:
      | limit | count | result |
      | 2     | 1     | False  |
      | 2     | 2     | True   |
      | 3     | 2     | False  |
      | 3     | 3     | True   |

  @mocked
  Scenario: The ReAct loop breaks early when the model repeats one tool call
    Given a ReAct orchestrator wired to a fake model that always calls run_bash with the same arguments
    When the goal "loop forever" is executed
    Then react_execute returns ok False
    And the summary mentions a loop
    And the model was called fewer times than max_steps
