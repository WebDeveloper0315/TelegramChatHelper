"""In-process event bus with isolated handler failures.

See ``docs/API.md`` section 5.3 for the delivery contract and ADR-031 for why
delivery is synchronous.
"""

from tgassist.infrastructure.events.bus import (
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_MAX_DEPTH,
    InProcessEventBus,
)

__all__ = [
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_MAX_DEPTH",
    "InProcessEventBus",
]
