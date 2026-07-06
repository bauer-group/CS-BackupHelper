"""Shared retry helper: exponential backoff that honors HTTP 429 Retry-After."""

from __future__ import annotations

import functools
import time
from typing import Any, Callable, TypeVar

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

F = TypeVar("F", bound=Callable[..., Any])


def retry_after_seconds(exc: BaseException) -> float | None:
    """Return an exception's ``retry_after`` value (HTTP 429) in seconds, if any."""
    value = getattr(exc, "retry_after", None)
    if value is None:
        return None
    return float(value)


def call_with_retry(
    fn: Callable[[], Any],
    *,
    attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Call ``fn()`` with retries on ``retry_on`` exceptions.

    ``sleep`` is injectable so tests can run instantly. A raised exception's
    ``retry_after`` attribute (HTTP 429) overrides the exponential wait.
    """
    exponential = wait_exponential(multiplier=base_delay, max=max_delay)

    def wait(retry_state: RetryCallState) -> float:
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None
        if exc is not None:
            seconds = retry_after_seconds(exc)
            if seconds is not None:
                return seconds
        return exponential(retry_state)

    retryer = Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait,
        retry=retry_if_exception_type(tuple(retry_on)),
        sleep=sleep,
        reraise=True,
    )
    return retryer(fn)


def retryable(
    *,
    attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[F], F]:
    """Decorator form of :func:`call_with_retry` with the same backoff behavior."""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return call_with_retry(
                lambda: fn(*args, **kwargs),
                attempts=attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                retry_on=retry_on,
                sleep=sleep,
            )

        return wrapper  # type: ignore[return-value]

    return decorator
