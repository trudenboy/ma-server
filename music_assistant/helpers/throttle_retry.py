"""Context manager using asyncio_throttle that catches and re-raises RetriesExhausted."""

import asyncio
import functools
import logging
import random
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Concatenate, Protocol

from music_assistant_models.errors import (
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.helpers.datetime import utc

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.throttle_retry")

BYPASS_THROTTLER: ContextVar[bool] = ContextVar("BYPASS_THROTTLER", default=False)

# Cap exponential backoff to prevent absurd wait times
MAX_BACKOFF = 120


def parse_retry_after(value: str | None) -> int:
    """
    Parse a Retry-After header value per RFC 9110 Section 10.2.3.

    Supports both valid formats: delay-seconds (integer) and HTTP-date.

    :param value: The raw Retry-After header value, or None if absent.
    :returns: Non-negative integer seconds to wait, or 0 if unparsable/absent.
    """
    if value is None:
        return 0
    # Try delay-seconds (non-negative integer) first — the common case
    try:
        return max(0, int(value))
    except ValueError, TypeError:
        pass
    # Try HTTP-date format (e.g., "Fri, 31 Dec 1999 23:59:59 GMT")
    try:
        target = parsedate_to_datetime(value)
        delta = (target - utc()).total_seconds()
        return max(0, int(delta))
    except ValueError, TypeError:
        return 0


class Throttler:
    """
    asyncio_throttle (https://github.com/hallazzang/asyncio-throttle).

    With improvements:
    - Accurate sleep without "busy waiting" (PR #4)
    - Return the delay caused by acquire()
    """

    def __init__(self, rate_limit: int, period: float = 1.0) -> None:
        """Initialize the Throttler."""
        self.rate_limit = rate_limit
        self.period = period
        self._task_logs: deque[float] = deque()

    async def acquire(self) -> float:
        """Acquire a free slot from the Throttler, returns the throttled time."""
        cur_time = time.monotonic()
        start_time = cur_time
        while True:
            self._flush()
            if len(self._task_logs) < self.rate_limit:
                break
            # sleep the exact amount of time until the oldest task can be flushed
            time_to_release = self._task_logs[0] + self.period - cur_time
            await asyncio.sleep(time_to_release)
            cur_time = time.monotonic()

        self._task_logs.append(cur_time)
        return cur_time - start_time  # exactly 0 if not throttled

    async def __aenter__(self) -> float:
        """Wait until the lock is acquired, return the time delay."""
        return await self.acquire()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Nothing to do on exit."""

    def _flush(self) -> None:
        now = time.monotonic()
        while self._task_logs:
            if now - self._task_logs[0] > self.period:
                self._task_logs.popleft()
            else:
                break


class ThrottlerManager:
    """Throttler manager that extends asyncio Throttle by retrying."""

    def __init__(
        self, rate_limit: int, period: float = 1, retry_attempts: int = 5, initial_backoff: int = 5
    ):
        """Initialize the AsyncThrottledContextManager."""
        self.retry_attempts = retry_attempts
        self.initial_backoff = initial_backoff
        self.throttler = Throttler(rate_limit, period)
        self._cooldown_until: float = 0.0

    @property
    def cooldown_remaining(self) -> float:
        """Seconds a server-imposed rate limit still holds every caller back, 0 when clear."""
        return max(0.0, self._cooldown_until - time.monotonic())

    @asynccontextmanager
    async def acquire(self, honored_until: float = 0.0) -> AsyncGenerator[float]:
        """
        Acquire a free slot from the Throttler, returns the throttled time.

        :param honored_until: Monotonic deadline the caller already waited out, so a
            cooldown no later than it does not hold the caller back a second time.
        :raises RateLimited: When a server-imposed cooldown holds for longer than MAX_WAIT_TIME.
        """
        if BYPASS_THROTTLER.get():
            yield 0
            return
        delay = 0.0
        honored = honored_until
        while True:
            # each deadline is waited out once, however often it is extended meanwhile
            while (target := self._cooldown_until) > honored:
                if (remaining := self.cooldown_remaining) > MAX_WAIT_TIME:
                    msg = f"Rate limited for another {remaining:.0f} seconds"
                    raise RateLimited(msg, backoff_time=round(remaining))
                delay += await self._wait_until(target)
                honored = target
            delay += await self.throttler.acquire()
            # a cooldown can be armed while we wait for a free slot, so only leave
            # the gate once it is still clear with the slot in hand
            if self._cooldown_until <= honored:
                break
        yield delay

    @asynccontextmanager
    async def bypass(self) -> AsyncGenerator[None]:
        """Bypass the throttler."""
        try:
            token = BYPASS_THROTTLER.set(True)
            yield None
        finally:
            BYPASS_THROTTLER.reset(token)

    def set_cooldown(self, seconds: float) -> None:
        """
        Hold back every caller of this throttler for the given number of seconds.

        :param seconds: How long the server-imposed rate limit still applies.
        """
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)

    def set_rate_limit(self, rate_limit: int, period: float = 1) -> None:
        """
        Change the rate limit of this throttler, an active cooldown stays in place.

        :param rate_limit: Number of requests allowed per period.
        :param period: Length of the period in seconds.
        """
        self.throttler.rate_limit = rate_limit
        self.throttler.period = period

    async def _wait_until(self, deadline: float) -> float:
        """Sleep until the given monotonic deadline, return the time waited."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        await asyncio.sleep(remaining)
        return remaining


class _Throttleable(Protocol):
    """Protocol for objects that can use the @throttle_with_retries decorator."""

    @property
    def logger(self) -> logging.Logger: ...

    @property
    def throttler(self) -> ThrottlerManager: ...


def throttle_with_retries[ProviderT: _Throttleable, **P, R](
    func: Callable[Concatenate[ProviderT, P], Awaitable[R]],
) -> Callable[Concatenate[ProviderT, P], Coroutine[Any, Any, R]]:
    """Call async function using the throttler with retries."""

    @functools.wraps(func)
    async def wrapper(self: ProviderT, *args: P.args, **kwargs: P.kwargs) -> R:
        """Call async function using the throttler with retries."""
        throttler = self.throttler
        exp_backoff = throttler.initial_backoff
        async with throttler.acquire() as delay:
            if delay != 0:
                self.logger.debug(
                    "%s was delayed for %.3f secs due to throttling", func.__name__, delay
                )
            for attempt in range(throttler.retry_attempts):
                try:
                    return await func(self, *args, **kwargs)
                except ResourceTemporarilyUnavailable as e:
                    self.logger.info(
                        f"Attempt {attempt + 1}/{throttler.retry_attempts} failed: {e}"
                    )
                    if attempt < throttler.retry_attempts - 1:
                        if e.backoff_time > 0:
                            # Server told us exactly how long to wait — respect it
                            sleep_time = float(e.backoff_time)
                        else:
                            # No server guidance — exponential backoff with jitter
                            sleep_time = min(exp_backoff * random.uniform(0.75, 1.25), MAX_BACKOFF)
                            exp_backoff = min(exp_backoff * 2, MAX_BACKOFF)
                        self.logger.info(f"Retrying in {sleep_time:.1f} seconds...")
                        await asyncio.sleep(sleep_time)
            else:  # noqa: PLW0120
                msg = f"Retries exhausted, failed after {throttler.retry_attempts} attempts"
                raise RetriesExhausted(msg)

    return wrapper


def _give_up(err: ResourceTemporarilyUnavailable, wait: float) -> RetriesExhausted:
    """
    Return the error of a call that does not sit out the wait asked of it.

    :param err: The error that asked for the wait, its localization is carried over.
    :param wait: The wait that was asked for, in seconds.
    """
    return RetriesExhausted(
        f"Not retrying, asked to wait {wait:.0f} seconds",
        translation_key=err.translation_key,
        translation_args=err.translation_args,
        translation_owner=err.translation_owner,
    )
