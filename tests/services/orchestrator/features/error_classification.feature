@mocked
Feature: Error classification gates retry vs. terminal handling
  As the orchestrator
  I want to classify a failed goal's error string into a precise class
  So that environmental/terminal failures are not reflect-retried to exhaustion
  while genuinely transient and unknown failures keep their normal retry budget.

  Background:
    Given the error classifier is available

  Scenario Outline: An error string maps to a class and a retry decision
    When I classify the error "<error>"
    Then the error class is "<error_class>"
    And the retry decision is "<decision>"

    Examples: terminal dependency (missing tool / sandbox / docker)
      | error                                            | error_class         | decision |
      | SkillUnavailable: code-sandbox not available     | TERMINAL_DEPENDENCY | terminal |
      | docker: command not found                        | TERMINAL_DEPENDENCY | terminal |
      | unshare: operation not permitted (EPERM)         | TERMINAL_DEPENDENCY | terminal |
      | No such file or directory: bwrap                 | TERMINAL_DEPENDENCY | terminal |
      | ENOENT: missing executable nsjail                | TERMINAL_DEPENDENCY | terminal |
      | the requested skill is unavailable               | TERMINAL_DEPENDENCY | terminal |

    Examples: terminal credential (auth / api key)
      | error                                            | error_class         | decision |
      | Missing API key for Figma                        | TERMINAL_CREDENTIAL | terminal |
      | invalid API_KEY supplied                         | TERMINAL_CREDENTIAL | terminal |
      | 401 Unauthorized                                 | TERMINAL_CREDENTIAL | terminal |
      | 403 Forbidden: bad credential                    | TERMINAL_CREDENTIAL | terminal |
      | authentication failed                            | TERMINAL_CREDENTIAL | terminal |

    Examples: terminal network (connection / DNS)
      | error                                            | error_class         | decision |
      | Connection refused                               | TERMINAL_NETWORK    | terminal |
      | ECONNREFUSED 127.0.0.1:8080                      | TERMINAL_NETWORK    | terminal |
      | Temporary failure in name resolution (DNS)       | TERMINAL_NETWORK    | terminal |
      | Network is unreachable                           | TERMINAL_NETWORK    | terminal |
      | getaddrinfo ENOTFOUND searxng.local              | TERMINAL_NETWORK    | terminal |

    Examples: rate limited (429 — bounded backoff)
      | error                                            | error_class         | decision   |
      | HTTP 429 Too Many Requests                       | RATE_LIMITED        | backoff    |
      | Semantic Scholar rate limit exceeded             | RATE_LIMITED        | backoff    |
      | You have hit the rate-limit, retry later         | RATE_LIMITED        | backoff    |

    Examples: transient (timeouts — limited retry)
      | error                                            | error_class         | decision |
      | Request timed out after 60s                      | TRANSIENT           | retry    |
      | read timeout                                     | TRANSIENT           | retry    |
      | the operation TIMED OUT                          | TRANSIENT           | retry    |

    Examples: retryable / unknown (default — normal MAX_GOAL_ATTEMPTS)
      | error                                            | error_class         | decision |
      | assertion failed: expected 4 got 5              | RETRYABLE           | retry    |
      | the function returned the wrong value            | RETRYABLE           | retry    |
      | KeyError: 'name'                                 | RETRYABLE           | retry    |
      |                                                  | RETRYABLE           | retry    |

  Scenario: A terminal-class failure is NOT reflect-retried in the graph
    Given a goal that fails with "docker: command not found"
    When the execute node processes the failed result
    Then the goal is marked exhausted at MAX_GOAL_ATTEMPTS
    And the goal's error_class is "TERMINAL_DEPENDENCY"

  Scenario: An unknown failure is still retried up to MAX_GOAL_ATTEMPTS
    Given a goal that fails with "assertion failed: expected 4 got 5"
    When the execute node processes the failed result
    Then the goal's attempts increments by one
    And the goal's error_class is "RETRYABLE"
