"""Clock port: the only source of time in the application.

Nothing outside an implementation of this port calls ``datetime.now()`` or
``time.monotonic()``. Time is injected so that behaviour depending on it -- reply
timing, retention, decay, rate limiting, token expiry -- is deterministic under
test without monkeypatching global state.

Two distinct notions of time, deliberately separate:

* **Wall time** (:meth:`Clock.now`) answers "what is the date and time". It can
  jump backwards when the system clock is corrected, so it must never be used to
  measure a duration.
* **Monotonic time** (:meth:`Clock.monotonic`) answers "how much time has
  passed". It never decreases but has no relationship to the calendar, so it
  must never be stored or displayed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Supplies the current time.

    Contract, guaranteed by every implementation and verified by the shared
    contract test suite:

    1. :meth:`now` returns a timezone-aware ``datetime`` whose tzinfo is UTC.
       A naive datetime is never returned, and no other zone is ever returned.
       Conversion to local time happens only in the presentation layer.
    2. Successive calls to :meth:`now` never return an earlier instant than a
       previous call.
    3. :meth:`monotonic` returns a non-decreasing float of seconds from an
       arbitrary origin. Only differences between two readings are meaningful.
    4. Both methods are total: they never raise and never block.
    """

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC ``datetime``."""
        ...

    def monotonic(self) -> float:
        """Return a non-decreasing seconds counter for measuring durations."""
        ...
