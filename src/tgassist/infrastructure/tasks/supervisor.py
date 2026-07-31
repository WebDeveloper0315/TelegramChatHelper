"""Supervision for long-lived asyncio tasks.

Infrastructure, not a port. There is one implementation, and the thing it
manages is asyncio itself: a protocol over `create_task` would abstract the
standard library from itself (``TELEGRAM_ARCHITECTURE.md`` section 11.3).

What it exists to own
---------------------

**Restart with backoff.** A task that dies of a recoverable failure is started
again, with a delay that grows so a permanently broken task degrades to quiet
rather than to a spin. A task that keeps dying is given up on and its last
failure is kept, so ``stop()`` can report it rather than swallowing it.

**Shutdown ordering.** Section 7.2 requires a specific order, and requiring
every caller to remember it is how it eventually gets forgotten:

1. Stop accepting new work -- every supervised task is cancelled.
2. Let the in-flight transaction finish. ``CancelledError`` arrives at an
   ``await``, and the unit of work's ``__aexit__`` rolls back what had not
   committed, so a batch is never half-written.
3. Whatever the caller does next -- disconnecting the gateway, closing the
   database -- happens after this returns.

Steps 3 to 5 of that list belong to the caller, because they are about
resources this does not own. What this guarantees is that by the time
:meth:`stop` returns, nothing supervised is still running and nothing is
still writing.

What it deliberately does not do
--------------------------------

It does not restart a task that returned normally. A consumer that reached the
end of its stream has finished, and restarting it would turn a closed connection
into an infinite loop of reconnection attempts nobody asked for.

It does not restart a cancelled task either. Cancellation is how shutdown is
expressed, and a supervisor that fought it would make shutdown impossible.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

from tgassist.infrastructure.logging import get_logger

#: How long to wait before restarting a task that failed, and the ceiling that
#: growth stops at. A first retry should be quick enough to ride out a blip; a
#: tenth should not be hammering anything.
DEFAULT_INITIAL_BACKOFF: Final = 0.5
DEFAULT_MAX_BACKOFF: Final = 30.0

#: How many consecutive failures before a task is given up on. Bounded because a
#: task that has failed this many times is not going to succeed on the next
#: attempt, and continuing would turn one broken component into an endless log.
DEFAULT_MAX_RESTARTS: Final = 5

_logger = get_logger(__name__)


@dataclass(slots=True)
class TaskStatus:
    """What has happened to one supervised task.

    Attributes:
        name: What the task is called, in logs and in reports.
        running: Whether it is executing right now.
        restarts: How many times it has been started again after failing.
        finished: Whether it returned normally, which is not a failure.
        failure: The exception that stopped it for good, or ``None``. Kept
            rather than only logged, so :meth:`BackgroundTaskSupervisor.stop`
            can hand it to a caller that must decide whether the run succeeded.
    """

    name: str
    running: bool = False
    restarts: int = 0
    finished: bool = False
    failure: BaseException | None = None


@dataclass(slots=True)
class _Supervised:
    """One task, its factory, and what has become of it."""

    name: str
    factory: Callable[[], Awaitable[object]]
    status: TaskStatus
    task: asyncio.Task[None] | None = None
    backoff: float = DEFAULT_INITIAL_BACKOFF
    stopping: bool = field(default=False)


class BackgroundTaskSupervisor:
    """Starts named long-lived tasks, restarts them, and stops them in order.

    Not reusable after :meth:`stop`: a supervisor that could be restarted would
    need to decide what to do with the failures it had already recorded, and
    building a second one costs nothing.
    """

    __slots__ = ("_initial_backoff", "_max_backoff", "_max_restarts", "_stopped", "_tasks")

    def __init__(
        self,
        *,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
    ) -> None:
        """Build a supervisor with a restart policy.

        Args:
            initial_backoff: Seconds to wait before the first restart.
            max_backoff: The longest wait, after doubling.
            max_restarts: Consecutive failures before a task is given up on.
        """
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._max_restarts = max_restarts
        self._tasks: dict[str, _Supervised] = {}
        self._stopped = False

    def start(self, name: str, factory: Callable[[], Awaitable[object]]) -> TaskStatus:
        """Run ``factory()`` as a supervised task.

        Takes a factory rather than a coroutine, because a restart needs a
        *fresh* one: an already-awaited coroutine cannot be run twice, and a
        supervisor holding one could only ever restart it into a RuntimeError.

        Whatever the factory returns is discarded. A supervised task runs for its
        effects; a return value that only the last of several attempts produced
        would be a confusing thing to hand back.

        Raises:
            RuntimeError: If a task of that name is already supervised, or if
                this supervisor has been stopped.
        """
        if self._stopped:
            msg = "This supervisor has stopped and cannot start anything else"
            raise RuntimeError(msg)
        if name in self._tasks:
            msg = f"A task named {name!r} is already supervised"
            raise RuntimeError(msg)

        supervised = _Supervised(
            name=name, factory=factory, status=TaskStatus(name=name), backoff=self._initial_backoff
        )
        # Marked running *here*, synchronously, rather than inside the task.
        # ``create_task`` only schedules; a caller that polled ``is_running``
        # before the loop next ran would see False and conclude the work had
        # finished before it started.
        supervised.status.running = True
        self._tasks[name] = supervised
        supervised.task = asyncio.create_task(self._run(supervised), name=name)
        return supervised.status

    async def _run(self, supervised: _Supervised) -> None:
        """Run one task, restarting it while that is the right thing to do."""
        while True:
            supervised.status.running = True
            try:
                await supervised.factory()
            except asyncio.CancelledError:
                # Shutdown. A supervisor that restarted through cancellation
                # would make shutdown impossible.
                supervised.status.running = False
                raise
            except BaseException as exc:
                supervised.status.running = False
                if supervised.stopping or not self._may_restart(supervised):
                    supervised.status.failure = exc
                    _logger.error(
                        "background_task_failed",
                        task=supervised.name,
                        error=type(exc).__name__,
                        restarts=supervised.status.restarts,
                    )
                    return
                supervised.status.restarts += 1
                _logger.warning(
                    "background_task_restarting",
                    task=supervised.name,
                    error=type(exc).__name__,
                    delay=supervised.backoff,
                    attempt=supervised.status.restarts,
                )
                await asyncio.sleep(supervised.backoff)
                supervised.backoff = min(supervised.backoff * 2, self._max_backoff)
                continue
            else:
                # Returned normally. A consumer that reached the end of its
                # stream has finished; restarting it would turn a closed
                # connection into an endless reconnection loop.
                supervised.status.running = False
                supervised.status.finished = True
                return

    def _may_restart(self, supervised: _Supervised) -> bool:
        """Whether this failure is one to try again after."""
        return supervised.status.restarts < self._max_restarts

    def status(self, name: str) -> TaskStatus | None:
        """Return what has become of one task, or ``None`` if it is unknown."""
        supervised = self._tasks.get(name)
        return supervised.status if supervised is not None else None

    def statuses(self) -> tuple[TaskStatus, ...]:
        """Return every supervised task's status, in the order they started."""
        return tuple(supervised.status for supervised in self._tasks.values())

    @property
    def is_running(self) -> bool:
        """Whether anything supervised is still executing."""
        return any(status.running for status in self.statuses())

    async def stop(self) -> tuple[TaskStatus, ...]:
        """Cancel every supervised task and wait for each to finish.

        Idempotent. Returns once nothing supervised is running, which is the
        precondition for the caller's next step -- disconnecting the gateway and
        closing the database, in that order (section 7.2).

        A task cancelled mid-transaction rolls that transaction back through the
        unit of work's own exit path, so shutdown cannot leave a batch
        half-written. What it can leave is work undone, which is what the next
        run's catch-up pass is for.

        Returns:
            Every task's final status, so a caller can report a failure that
            happened while nobody was watching.
        """
        self._stopped = True
        for supervised in self._tasks.values():
            supervised.stopping = True
            if supervised.task is not None:
                supervised.task.cancel()

        for supervised in self._tasks.values():
            if supervised.task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await supervised.task
                supervised.status.running = False

        return self.statuses()

    async def __aenter__(self) -> BackgroundTaskSupervisor:
        """Enter the supervisor's lifetime."""
        return self

    async def __aexit__(self, *_details: object) -> None:
        """Leave it, stopping everything supervised first."""
        await self.stop()


__all__ = [
    "DEFAULT_INITIAL_BACKOFF",
    "DEFAULT_MAX_BACKOFF",
    "DEFAULT_MAX_RESTARTS",
    "BackgroundTaskSupervisor",
    "TaskStatus",
]
