@mocked
Feature: Reconcile the final rendered answer
  A goal whose skill returned ok=True but whose user-facing rendered final
  answer is a punt (e.g. "file too large / provide a snippet") must be stored
  with ok=False. Genuine successes and verified fixes stay ok=True. This is the
  third reconciliation seam, after the skill-first and ReAct finish seams.

  Background:
    Given an orchestrator handler with a mocked redis and storage
    And run_task returns a state with error None and final_answer "internal"

  Scenario: A rendered punt flips a false-ok to ok=False
    Given stream_final_answer renders "I couldn't analyze the file because it is too large. Please share a snippet."
    When the handler processes task "fa-bdd-punt"
    Then the stored result ok is False
    And the stored final answer contains "too large"

  Scenario: A genuine success answer stays ok=True
    Given stream_final_answer renders "Here is the square function you requested."
    When the handler processes task "fa-bdd-ok"
    Then the stored result ok is True

  Scenario: An unverified success claim is downgraded
    Given stream_final_answer renders "I fixed the bug and all tests pass."
    When the handler processes task "fa-bdd-claim"
    Then the stored result ok is False
