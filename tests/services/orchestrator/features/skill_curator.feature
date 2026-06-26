@mocked
Feature: Skill curator (proposal-only, background)
  As the Labmate operator
  I want a background curator that drafts candidate skills for human review
  So that recurring successful tool sequences become reusable skills
  Without ever auto-activating untrusted generated MCP servers

  Background:
    Given a skills root with an active skill "calc"
    And a curator state sidecar that has never run

  Scenario: Curator is OFF by default and is a no-op
    Given ENABLE_SKILL_CURATOR is "0"
    When the orchestrator decides whether to spawn the curator loop
    Then the curator loop task is not created
    And no ".proposed" directory is created

  Scenario: Gate stays closed before the interval has elapsed
    Given the curator last ran 1 hours ago
    And the system has been idle for 9999 seconds
    When the gate is evaluated with interval 168 hours and min idle 2 hours
    Then the gate result is closed

  Scenario: Gate stays closed when the interval elapsed but the host is busy
    Given the curator last ran 200 hours ago
    And the system has been idle for 60 seconds
    When the gate is evaluated with interval 168 hours and min idle 2 hours
    Then the gate result is closed

  Scenario: Gate opens only after interval AND idle are both satisfied
    Given the curator last ran 200 hours ago
    And the system has been idle for 9999 seconds
    When the gate is evaluated with interval 168 hours and min idle 2 hours
    Then the gate result is open

  Scenario: Gate stays closed while paused
    Given the curator is paused
    And the curator last ran 200 hours ago
    And the system has been idle for 9999 seconds
    When the gate is evaluated with interval 168 hours and min idle 2 hours
    Then the gate result is closed

  Scenario: A successful sequence is staged as a proposed skill draft
    Given a recent successful sequence "review-fix" using tools "code-review,edit_file"
    And the LLM drafts the description "Review a file then apply the fix."
    When the curator proposes a skill from that sequence
    Then a file "services/skills/.proposed/review-fix/SKILL.md" exists
    And the SKILL.md frontmatter has name "review-fix"
    And the SKILL.md body mentions tools "code-review" and "edit_file"
    And a file "services/skills/.proposed/review-fix/server.py.stub" exists
    And the server stub is marked non-functional
    And a "skill.proposed" event was emitted with name "review-fix"

  Scenario: discover() never activates a proposed skill
    Given a proposed draft "review-fix" staged under ".proposed"
    When the skill runner discovers skills
    Then the catalog does not contain "review-fix"
    And the catalog still contains "calc"

  Scenario: An unused active skill auto-transitions to archived
    Given an active skill "old-tool" last used 100000000 seconds ago
    When the curator sweeps lifecycle transitions
    Then the transition for "old-tool" is "archived"

  Scenario: A recently used active skill stays active
    Given an active skill "calc" last used 10 seconds ago
    When the curator sweeps lifecycle transitions
    Then the transition for "calc" is "active"

  Scenario: A curator failure never breaks goal execution
    Given the LLM drafting call raises an error
    When the curator runs one cycle
    Then the curator cycle returns without raising
    And the orchestrator goal loop is unaffected
