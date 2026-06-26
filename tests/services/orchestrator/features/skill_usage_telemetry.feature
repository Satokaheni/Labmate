@mocked
Feature: Skill usage telemetry (foundation for the curator)
  As the orchestrator
  I want every skill dispatch to bump a per-skill usage counter and timestamp
  So that an unused skill ages to stale/archived while pinned skills stay active,
  and so that a telemetry failure can never break a skill dispatch.

  Background:
    Given an empty telemetry store

  Scenario: A successful skill dispatch bumps use_count and success_count
    When the skill "web-search" is dispatched with result ok
    Then the telemetry use_count for "web-search" is 1
    And the telemetry success_count for "web-search" is 1
    And the telemetry fail_count for "web-search" is 0
    And the telemetry last_used_at for "web-search" is set

  Scenario: A failed skill dispatch bumps fail_count only
    When the skill "web-search" is dispatched with result fail
    Then the telemetry use_count for "web-search" is 1
    And the telemetry success_count for "web-search" is 0
    And the telemetry fail_count for "web-search" is 1

  Scenario: An unused skill computes to stale after the threshold
    Given the skill "ast-repo-map" was last used 40 days ago
    When the telemetry states are recomputed
    Then the telemetry state for "ast-repo-map" is "stale"

  Scenario: A pinned skill stays active past the archive threshold
    Given the skill "web-search" was last used 200 days ago and is pinned
    When the telemetry states are recomputed
    Then the telemetry state for "web-search" is "active"

  Scenario: A telemetry write failure does not break the dispatch
    Given telemetry persistence is broken
    When the skill "web-search" is dispatched through the router with result ok
    Then the router dispatch result is ok
