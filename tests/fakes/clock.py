"""Deterministic clock fakes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tgassist.domain.ports.clock import Clock

EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
"""An arbitrary fixed instant, so tests read as dates rather than as offsets."""


class FixedClock(Clock):
    """A clock frozen at one instant.

    Time never moves, which is what makes an assertion about a computed
    timestamp exact rather than approximate.
    """

    __slots__ = ("_instant", "_monotonic")

    def __init__(self, instant: datetime = EPOCH, *, monotonic: float = 0.0) -> None:
        if instant.tzinfo is None:
            msg = "FixedClock requires a timezone-aware instant"
            raise ValueError(msg)
        self._instant = instant.astimezone(UTC)
        self._monotonic = monotonic

    def now(self) -> datetime:
        return self._instant

    def monotonic(self) -> float:
        return self._monotonic


class AdvanceableClock(Clock):
    """A clock that moves only when a test tells it to.

    Useful for behaviour that depends on elapsed time -- retention, decay,
    backoff -- without the test sleeping.
    """

    __slots__ = ("_instant", "_monotonic")

    def __init__(self, instant: datetime = EPOCH, *, monotonic: float = 0.0) -> None:
        if instant.tzinfo is None:
            msg = "AdvanceableClock requires a timezone-aware instant"
            raise ValueError(msg)
        self._instant = instant.astimezone(UTC)
        self._monotonic = monotonic

    def now(self) -> datetime:
        return self._instant

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, delta: timedelta) -> None:
        """Move both wall time and monotonic time forward by ``delta``."""
        if delta < timedelta(0):
            msg = "Time cannot be advanced backwards; construct a new clock instead"
            raise ValueError(msg)
        self._instant += delta
        self._monotonic += delta.total_seconds()

    def set(self, instant: datetime) -> None:
        """Jump wall time to ``instant``, leaving monotonic time untouched.

        This models a system clock correction, which moves wall time but not the
        monotonic source. It exists so the difference between the two can be
        exercised rather than assumed.
        """
        if instant.tzinfo is None:
            msg = "AdvanceableClock requires a timezone-aware instant"
            raise ValueError(msg)
        self._instant = instant.astimezone(UTC)
