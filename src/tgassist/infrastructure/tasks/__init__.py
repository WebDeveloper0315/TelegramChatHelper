"""Background task supervision.

One module so far: :mod:`supervisor`, which owns the lifetime of the
long-lived asyncio tasks synchronisation runs on. A scheduler joins it when
something needs to run on a timer rather than on a stream.
"""

from tgassist.infrastructure.tasks.supervisor import (
    DEFAULT_INITIAL_BACKOFF,
    DEFAULT_MAX_BACKOFF,
    DEFAULT_MAX_RESTARTS,
    BackgroundTaskSupervisor,
    TaskStatus,
)

__all__ = [
    "DEFAULT_INITIAL_BACKOFF",
    "DEFAULT_MAX_BACKOFF",
    "DEFAULT_MAX_RESTARTS",
    "BackgroundTaskSupervisor",
    "TaskStatus",
]
