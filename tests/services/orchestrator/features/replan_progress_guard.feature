Feature: Replan progress guard stops over-planning and skill thrash
  In SEQUENCING_MODE=replan the planner can re-emit a near-identical sub-goal
  or re-target one skill many times (the live A/B saw repo-fault-localize run
  four times). A pure guard detects no-progress and forces an honest finish,
  and the loop resets the skill-activation budget per sub-step so load_skill
  never hits its max_chain cap mid-chain.

  @mocked
  Scenario: An immediate duplicate sub-goal trips the guard
    Given a replan history whose last step is "Review the module for bugs"
    When the planner proposes the next sub-goal "review the module for bugs"
    Then the replan guard says stop
    And the replan stop reason is "duplicate_subgoal"

  @mocked
  Scenario: A distinct next sub-goal does not trip the guard
    Given a replan history whose last step is "Generate unit tests for factorial"
    When the planner proposes the next sub-goal "Fix the off-by-one bug in factorial"
    Then the replan guard says continue

  @mocked
  Scenario: Re-targeting one skill beyond the repeat cap trips the guard
    Given a replan history that has used skill "repo-fault-localize" 2 times
    And the skill repeat cap is 2
    When the planner proposes the next sub-goal "Run repo-fault-localize again on the module"
    Then the replan guard says stop
    And the replan stop reason is "skill_repeat_cap"

  @mocked
  Scenario: The replan loop caps a planner that keeps repeating one skill
    Given a replan orchestrator whose planner always asks to run "repo-fault-localize"
    And the skill repeat cap is 2
    When the compound goal "find and fix every fault" is executed in replan mode
    Then the matching skill is dispatched at most 2 times
    And the activation budget is reset at least once per executed sub-step
