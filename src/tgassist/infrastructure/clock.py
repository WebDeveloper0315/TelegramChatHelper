"""System clock adapter."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from tgassist.domain.ports.clock import Clock


class SystemClock(Clock):
    """Reads the operating system clock.

    ``now`` is always timezone-aware and always UTC. Returning a naive datetime
    would push timezone handling out to every call site, which is how mixed
    naive and aware values end up compared -- a bug that surfaces months later
    as a comparison error or a silently wrong duration.

    ``monotonic`` comes from a separate, non-decreasing source, so a system
    clock correction cannot make a measured duration negative.
    """

    __slots__ = ()

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Return a non-decreasing seconds counter for measuring durations."""
        return time.monotonic()
