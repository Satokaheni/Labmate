Feature: Endpoint failover behavior
  As a user of acompletion_with_failover
  I want the wrapper to seamlessly fallback to secondary endpoints on transient failures
  So that transient issues (connection drops, temporary 503s) don't break the workflow

  Background:
    Given backoff sleeping is disabled in the test harness
    And jitter is deterministic in the test harness

  Scenario: Primary connection error, secondary succeeds
    Given a primary base url that always returns connection errors
    And a secondary base url that returns a valid completion
    When I request a completion with failover across both endpoints
    Then the completion succeeds with the secondary endpoint's content
    And the secondary endpoint was called after the primary

  Scenario: Primary 503, secondary succeeds
    Given a primary base url that always returns 503 Service Unavailable
    And a secondary base url that returns a valid completion
    When I request a completion with failover across both endpoints
    Then the completion succeeds with the secondary endpoint's content
    And the primary endpoint was attempted exactly 2 times
    And the secondary endpoint was attempted exactly 1 times

  Scenario: Primary 400 Bad Request (non-retryable), secondary never called
    Given a primary base url that returns 400 Bad Request
    And a secondary base url that returns a valid completion
    When I request a completion with failover across both endpoints
    Then a BadRequestError is raised
    And the secondary endpoint was never called

  Scenario: Single endpoint recovers from transient blip
    Given a single base url that returns 503 once and then a valid completion
    And the per-base attempt cap is 2
    When I request a completion with failover across that endpoint
    Then the completion succeeds with that endpoint's content
    And the endpoint was attempted exactly 2 times

  Scenario: All endpoints exhausted
    Given a primary base url that always returns 503 Service Unavailable
    And a secondary base url that always returns connection errors
    When I request a completion with failover across both endpoints
    Then an AllEndpointsExhausted error is raised
