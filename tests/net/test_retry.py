"""Tests for the shared network retry helper (backoff + Retry-After)."""

import pytest

from backuphelper.net.retry import call_with_retry, retry_after_seconds, retryable


class _RateLimited(Exception):
    """Stand-in for an HTTP 429 error carrying a Retry-After value."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("429 Too Many Requests")
        self.retry_after = retry_after


def test_succeeds_after_transient_failures():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "ok"

    result = call_with_retry(fn, sleep=lambda _s: None)
    assert result == "ok"
    assert calls["n"] == 3


def test_reraises_after_exhausting_attempts():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("always")

    with pytest.raises(ValueError, match="always"):
        call_with_retry(fn, attempts=4, sleep=lambda _s: None)
    assert calls["n"] == 4


def test_unlisted_exception_propagates_without_retry():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise KeyError("not retried")

    with pytest.raises(KeyError):
        call_with_retry(
            fn, attempts=5, retry_on=(ValueError,), sleep=lambda _s: None
        )
    assert calls["n"] == 1


def test_sleep_gets_exponential_delays_capped_at_max():
    delays: list[float] = []

    def fn():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        call_with_retry(
            fn,
            attempts=6,
            base_delay=1.0,
            max_delay=8.0,
            sleep=delays.append,
        )
    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_retry_after_overrides_exponential_backoff():
    delays: list[float] = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _RateLimited(7)
        return "done"

    result = call_with_retry(fn, base_delay=1.0, sleep=delays.append)
    assert result == "done"
    assert delays == [7, 7]


def test_retry_after_seconds_reads_attribute_or_none():
    assert retry_after_seconds(_RateLimited(7)) == 7.0
    assert retry_after_seconds(ValueError("no attr")) is None


def test_retryable_decorator_retries_then_returns_value():
    delays: list[float] = []
    calls = {"n": 0}

    @retryable(attempts=5, base_delay=1.0, sleep=delays.append)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3
    assert delays == [1.0, 2.0]
