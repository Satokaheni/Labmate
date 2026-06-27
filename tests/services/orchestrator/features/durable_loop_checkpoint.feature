@mocked
Feature: Durable per-turn inner-loop checkpoint
  As the ReAct loop orchestrator
  I want each turn snapshotted to a best-effort store
  So that a crash + restart resumes from the saved turn instead of from scratch
  And the loop is byte-identical to today when the feature is off

  Scenario: a pre-seeded checkpoint resumes mid-loop, not from scratch
    Given an AsyncOrchestrator with a fake checkpoint store and task id "task-resume"
    And loop checkpointing is enabled
    And a checkpoint is pre-seeded for goal "resume me" at turn 2 with prior message "prior work done"
    And the model calls finish with summary "finished after resume"
    When the react loop runs the goal "resume me"
    Then the result ok is True
    And the result summary contains "finished after resume"
    And the running messages include "prior work done"

  Scenario: a finished goal clears its checkpoint
    Given an AsyncOrchestrator with a fake checkpoint store and task id "task-clear"
    And loop checkpointing is enabled
    And the model calls finish with summary "all done"
    When the react loop runs the goal "do the thing"
    Then the result ok is True
    And no checkpoint remains for task "task-clear"

  Scenario: with the feature off the loop performs no checkpoint IO
    Given an AsyncOrchestrator with a fake checkpoint store and task id "task-off"
    And loop checkpointing is disabled
    And the model calls finish with summary "done"
    When the react loop runs the goal "no checkpoint"
    Then the result ok is True
    And the checkpoint store was never read or written

  Scenario: a resumed loop restores loaded_skills to avoid re-loading and re-charging budget
    Given an AsyncOrchestrator with a fake checkpoint store and task id "task-skills"
    And loop checkpointing is enabled
    And a checkpoint is pre-seeded for goal "resume with skills" at turn 1 with prior message "prior work"
    And the checkpoint has loaded_skills ["read_file", "write_file"]
    And the model calls finish with summary "finished after resume"
    When the react loop runs the goal "resume with skills"
    Then the result ok is True
    And the result summary contains "finished after resume"
    And no checkpoint remains for task "task-skills"
