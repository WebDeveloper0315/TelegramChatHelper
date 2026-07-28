"""The single thread on which all database work runs.

SQLite is synchronous and its connections are thread-affine. ADR-013 resolves
that by giving the persistence layer one dedicated worker thread and routing
every operation through it.

The value of a *single* worker, rather than a pool, is that it makes SQLite's
one-writer rule structural instead of aspirational. Two threads writing
concurrently produce ``SQLITE_BUSY`` under load, and the usual fix -- a busy
timeout and a retry loop -- converts a design problem into an intermittent one.
With a single worker, the second write waits in a queue rather than failing, and
the failure mode disappears rather than becoming rare.

The cost is that reads also serialise behind writes. That is a real limitation,
recorded in ADR-034, and deliberately not optimised before it has been measured.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")

THREAD_NAME_PREFIX = "tgassist-db"


class DatabaseExecutor:
    """Runs blocking database callables on one dedicated thread."""

    __slots__ = ("_closed", "_executor", "_lock", "_thread_id")

    def __init__(self, *, thread_name_prefix: str = THREAD_NAME_PREFIX) -> None:
        """Create the executor and its single worker thread."""
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name_prefix)
        self._thread_id: int | None = None
        self._lock = threading.Lock()
        self._closed = False

    async def run(self, func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Run ``func`` on the database thread and await its result.

        Raises:
            RuntimeError: If the executor has been shut down. Queuing work onto
                a closed executor would hang rather than fail, so it is refused.
        """
        if self._closed:
            msg = "The database executor is closed"
            raise RuntimeError(msg)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: self._invoke(func, *args, **kwargs)
        )

    def _invoke(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if self._thread_id is None:
            with self._lock:
                self._thread_id = threading.get_ident()
        return func(*args, **kwargs)

    def is_database_thread(self) -> bool:
        """Report whether the caller is running on the database thread.

        Used by assertions that catch a connection escaping to another thread --
        the defect this executor exists to prevent.
        """
        return self._thread_id is not None and threading.get_ident() == self._thread_id

    @property
    def is_closed(self) -> bool:
        """Report whether the executor has been shut down."""
        return self._closed

    def close(self, *, wait: bool = True) -> None:
        """Shut down the worker thread. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=wait)
