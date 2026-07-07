from __future__ import annotations

import pytest

from services.orchestrator.error_classifier import (
    TERMINAL_CLASSES,
    ErrorClass,
    classify_error,
    is_terminal,
)


@pytest.mark.mocked
class TestErrorClassEnum:
    def test_members_are_name_valued_strings(self):
        assert ErrorClass.RETRYABLE.value == "RETRYABLE"
        assert ErrorClass.TERMINAL_DEPENDENCY.value == "TERMINAL_DEPENDENCY"
        # str-Enum: equals its own value
        assert ErrorClass.RATE_LIMITED == "RATE_LIMITED"

    def test_terminal_classes_set(self):
        assert TERMINAL_CLASSES == frozenset(
            {
                ErrorClass.TERMINAL_DEPENDENCY,
                ErrorClass.TERMINAL_CREDENTIAL,
                ErrorClass.TERMINAL_NETWORK,
                ErrorClass.TERMINAL_CONTEXT,
            }
        )
        assert is_terminal(ErrorClass.TERMINAL_NETWORK) is True
        assert is_terminal(ErrorClass.RETRYABLE) is False
        assert is_terminal(ErrorClass.RATE_LIMITED) is False


@pytest.mark.mocked
class TestClassifyDependency:
    @pytest.mark.parametrize(
        "err",
        [
            "SkillUnavailable: code-sandbox not available",
            "docker: command not found",
            "unshare: operation not permitted (EPERM)",
            "No such file or directory: bwrap",
            "ENOENT: missing executable nsjail",
            "the requested skill is unavailable",
            "permission denied running container",
        ],
    )
    def test_dependency_terminal(self, err):
        assert classify_error(err) == ErrorClass.TERMINAL_DEPENDENCY


@pytest.mark.mocked
class TestClassifyCredential:
    @pytest.mark.parametrize(
        "err",
        [
            "Missing API key for Figma",
            "invalid API_KEY supplied",
            "401 Unauthorized",
            "403 Forbidden: bad credential",
            "authentication failed",
            "apikey rejected",
        ],
    )
    def test_credential_terminal(self, err):
        assert classify_error(err) == ErrorClass.TERMINAL_CREDENTIAL


@pytest.mark.mocked
class TestClassifyNetwork:
    @pytest.mark.parametrize(
        "err",
        [
            "Connection refused",
            "ECONNREFUSED 127.0.0.1:8080",
            "Temporary failure in name resolution (DNS)",
            "Network is unreachable",
            "getaddrinfo ENOTFOUND searxng.local",
        ],
    )
    def test_network_terminal(self, err):
        assert classify_error(err) == ErrorClass.TERMINAL_NETWORK


@pytest.mark.mocked
class TestClassifyRateLimited:
    @pytest.mark.parametrize(
        "err",
        [
            "HTTP 429 Too Many Requests",
            "Semantic Scholar rate limit exceeded",
            "You have hit the rate-limit, retry later",
        ],
    )
    def test_rate_limited(self, err):
        assert classify_error(err) == ErrorClass.RATE_LIMITED


@pytest.mark.mocked
class TestClassifyTransient:
    @pytest.mark.parametrize(
        "err",
        [
            "Request timed out after 60s",
            "read timeout",
            "the operation TIMED OUT",
        ],
    )
    def test_transient(self, err):
        assert classify_error(err) == ErrorClass.TRANSIENT


@pytest.mark.mocked
class TestClassifyRetryableDefault:
    @pytest.mark.parametrize(
        "err",
        [
            "assertion failed: expected 4 got 5",
            "the function returned the wrong value",
            "KeyError: 'name'",
            "",
            None,
        ],
    )
    def test_unknown_is_retryable(self, err):
        assert classify_error(err) == ErrorClass.RETRYABLE


@pytest.mark.mocked
class TestClassifierProperties:
    def test_case_insensitive(self):
        assert classify_error("DOCKER: COMMAND NOT FOUND") == ErrorClass.TERMINAL_DEPENDENCY
        assert classify_error("connection REFUSED") == ErrorClass.TERMINAL_NETWORK
        assert classify_error("API_KEY MISSING") == ErrorClass.TERMINAL_CREDENTIAL

    def test_deterministic(self):
        s = "Connection refused while reaching searxng"
        assert classify_error(s) == classify_error(s) == ErrorClass.TERMINAL_NETWORK

    def test_accepts_exception_instance(self):
        assert (
            classify_error(ConnectionRefusedError("Connection refused"))
            == ErrorClass.TERMINAL_NETWORK
        )
        assert classify_error(RuntimeError("docker not found")) == ErrorClass.TERMINAL_DEPENDENCY

    def test_credential_beats_network_when_both_present(self):
        # "401 Unauthorized" returned by a remote host over a working connection
        # is a credential problem, not a network one. Credential is checked first.
        assert (
            classify_error("401 Unauthorized from api.semanticscholar.org")
            == ErrorClass.TERMINAL_CREDENTIAL
        )

    def test_rate_limit_beats_network_429_noise(self):
        # A 429 is rate limiting even if the message also mentions the host/connection.
        assert (
            classify_error("429 Too Many Requests from connection pool") == ErrorClass.RATE_LIMITED
        )

    def test_migrated_markers_still_terminal(self):
        # Every legacy _NONRETRYABLE_ERROR_MARKERS substring must still classify as
        # a terminal OR rate-limited class (i.e. NOT plain RETRYABLE), so nothing
        # previously caught as nonretryable regresses.
        legacy = [
            "skillunavailable",
            "not available",
            "unavailable",
            "no such",
            "not found",
            "missing",
            "docker",
            "permission denied",
            "eperm",
            "enoent",
            "connection refused",
            "network",
            "timed out",
            "timeout",
            "api key",
            "apikey",
            "credential",
            "rate limit",
            "429",
        ]
        for marker in legacy:
            cls = classify_error(marker)
            assert cls != ErrorClass.RETRYABLE, f"{marker!r} regressed to RETRYABLE"


@pytest.mark.mocked
class TestStateField:
    def test_state_accepts_error_class_field(self):
        from services.orchestrator.types import State  # noqa: F401

        # TypedDict total=False: error_class is an optional key. A plain dict
        # carrying it must satisfy the annotation at type-check time and at
        # runtime round-trip cleanly.
        s: State = {"error_class": ErrorClass.TERMINAL_DEPENDENCY.value}
        assert s["error_class"] == "TERMINAL_DEPENDENCY"

    def test_error_class_annotation_present(self):
        from services.orchestrator.types import State

        assert "error_class" in State.__annotations__


@pytest.mark.mocked
class TestTerminalContext:
    """Context-overflow errors are TERMINAL (retry can't fix an oversized prompt)."""

    def test_context_overflow_messages_classify_terminal_context(self):
        from services.orchestrator.error_classifier import (
            ErrorClass,
            classify_error,
            is_terminal,
        )

        cases = [
            "This model's maximum context length is 131072 tokens",
            "context_length_exceeded: reduce the length of the messages",
            "prompt is too long for the context window",
            "error: n_ctx exceeded",
            "too many tokens in the request",
        ]
        for msg in cases:
            cls = classify_error(msg)
            assert cls == ErrorClass.TERMINAL_CONTEXT, f"{msg!r} -> {cls}"
            assert is_terminal(cls) is True

    def test_bare_context_word_does_not_false_positive(self):
        from services.orchestrator.error_classifier import ErrorClass, classify_error

        # A generic error merely mentioning "context" must NOT be TERMINAL_CONTEXT.
        assert classify_error("failed in the context of node execution") != (
            ErrorClass.TERMINAL_CONTEXT
        )

    def test_rate_limit_precedence_over_context(self):
        from services.orchestrator.error_classifier import ErrorClass, classify_error

        # "too many requests" is rate limiting even if the message also says tokens.
        assert classify_error("429 too many requests") == ErrorClass.RATE_LIMITED
