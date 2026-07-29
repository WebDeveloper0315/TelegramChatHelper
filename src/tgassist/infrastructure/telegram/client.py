"""The boundary between TDLib's thread and the asyncio application.

``td_receive`` is a blocking call that exactly one thread may make. Everything
else in this application runs on the event loop. This module is the seam
between the two, and it is the only place in the project where a thread hands
work to the loop (ADR-048).

Thread ownership
----------------

======================  ==================================================
``tgassist-td``         Calls ``td_receive``. Nothing else ever does.
The asyncio loop        Calls ``td_send``, resolves futures, owns the queue.
======================  ==================================================

``td_send`` is thread-safe by TDLib's own contract, so requests go straight
from the loop rather than being marshalled onto the receive thread. Only
receipt needs a thread, and only one.

How a frame gets from TDLib to the application
----------------------------------------------

The receive thread never touches application state. It parses a frame and hands
it to the loop:

* a frame carrying an ``@extra`` we issued resolves that request's future;
* anything else is an update, and goes on a bounded queue.

Backpressure
------------

The queue is bounded. When it is full the receive thread **blocks**, before
calling ``td_receive`` again, so TDLib buffers internally rather than this
process growing an unbounded Python queue. A full queue means consumption is
behind, and :meth:`TdjsonClient.health` reports it rather than absorbing it.

End of stream
-------------

:meth:`TdjsonClient.receive` returns ``None`` when the stream has ended. That
signal is carried by an event rather than by a sentinel value placed on the
queue, for a specific reason: an in-band sentinel cannot be delivered through a
**full** queue, which is exactly the state a stalled consumer leaves it in. An
end signal that can be blocked by the condition it is meant to report is not an
end signal.

Restart
-------

**Not supported.** A closed client stays closed; construct another. TDLib
identifies a client by an integer it issues, a closed client's identifier is
dead, and reusing the object would mean tracking which generation every pending
future belonged to. Nothing needs it, so the state machine has no edge for it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import json
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from tgassist.domain.errors import (
    TdlibNotRunningError,
    TdlibRequestFailedError,
    TdlibShutdownTimeoutError,
)
from tgassist.infrastructure.logging import get_logger
from tgassist.infrastructure.telegram.loader import NativeLibrary

logger = get_logger(__name__)

DEFAULT_QUEUE_CAPACITY: Final = 1024
"""Updates held before the receive thread is made to wait.

Large enough that an ordinary burst does not stall ingestion, small enough that
a stalled consumer is visible in memory rather than invisible until an
out-of-memory kill.
"""

DEFAULT_RECEIVE_TIMEOUT: Final = 1.0
"""Seconds ``td_receive`` waits before returning nothing.

Also the granularity of shutdown: the thread cannot notice a stop request while
it is inside ``td_receive``, so this bounds how long it takes to notice.
"""

DEFAULT_SHUTDOWN_TIMEOUT: Final = 10.0
"""Seconds to wait for the receive thread to stop before reporting failure."""

DEFAULT_REQUEST_TIMEOUT: Final = 30.0
"""Seconds to wait for a reply before giving up on a request."""

_BACKPRESSURE_POLL: Final = 0.1
"""How often a blocked receive thread rechecks whether it should stop."""

THREAD_NAME: Final = "tgassist-td"

_EXTRA: Final = "@extra"
_TYPE: Final = "@type"
_ERROR_TYPE: Final = "error"


class ClientState(StrEnum):
    """Where a client is in its life.

    ``STOPPED`` is both the initial and the final state; ``FAILED`` is terminal
    and distinct, because "never started" and "died" need different responses.
    """

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ClientHealth:
    """What the client knows about itself.

    Attributes:
        state: The lifecycle state.
        thread_alive: Whether the receive thread is still running.
        client_id: The identifier TDLib issued, or ``None`` before start.
        frames_received: Frames read from TDLib, of every kind.
        updates_queued: Updates waiting to be consumed *now*.
        queue_capacity: How many may wait before the receive thread blocks.
        queue_high_water: The most that have ever waited. A value at capacity
            means consumption fell behind at least once.
        pending_requests: Requests sent and not yet answered.
        malformed_frames: Frames TDLib sent that were not JSON objects. Counted
            rather than raised: one bad frame must not end the stream.
        failure: Why the client failed, if it did.
    """

    state: ClientState
    thread_alive: bool
    client_id: int | None
    frames_received: int
    updates_queued: int
    queue_capacity: int
    queue_high_water: int
    pending_requests: int
    malformed_frames: int
    failure: str | None

    @property
    def is_saturated(self) -> bool:
        """Whether the queue is full, so the receive thread is being made to wait."""
        return self.updates_queued >= self.queue_capacity


class TdjsonClient:
    """Drives one TDLib client, bridging its thread to the event loop."""

    __slots__ = (
        "_client_id",
        "_ended",
        "_failure",
        "_frames",
        "_high_water",
        "_ids",
        "_library",
        "_loop",
        "_malformed",
        "_pending",
        "_queue",
        "_queue_capacity",
        "_receive_timeout",
        "_shutdown_timeout",
        "_state",
        "_state_lock",
        "_stopping",
        "_thread",
    )

    def __init__(
        self,
        library: NativeLibrary,
        *,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        receive_timeout: float = DEFAULT_RECEIVE_TIMEOUT,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
    ) -> None:
        """Build a client over a loaded library.

        Args:
            library: A verified, loaded ``tdjson``. Obtaining one is the
                loader's job, and this class does not repeat its checks.
            queue_capacity: Updates that may wait before the receive thread is
                made to block.
            receive_timeout: How long ``td_receive`` waits, and therefore how
                long shutdown may take to be noticed.
            shutdown_timeout: How long :meth:`close` waits for the thread.
        """
        self._library = library
        self._queue_capacity = queue_capacity
        self._receive_timeout = receive_timeout
        self._shutdown_timeout = shutdown_timeout

        self._state = ClientState.STOPPED
        self._state_lock = threading.Lock()
        self._failure: str | None = None

        self._client_id: int | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = threading.Event()
        self._ended: asyncio.Event | None = None

        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)

        self._frames = 0
        self._malformed = 0
        self._high_water = 0

    # -- Lifecycle --------------------------------------------------------

    @property
    def state(self) -> ClientState:
        """Return the lifecycle state."""
        with self._state_lock:
            return self._state

    async def start(self) -> None:
        """Create the TDLib client and start the receive thread.

        Asynchronous because it captures the running loop: every frame the
        thread receives is handed back to *this* loop, and there is no correct
        default for which one that is.

        Raises:
            TdlibNotRunningError: If the client has already been started. A
                closed client is not restarted -- construct another.
        """
        with self._state_lock:
            if self._state is not ClientState.STOPPED or self._thread is not None:
                msg = f"A client in state {self._state.value} cannot be started"
                raise TdlibNotRunningError(
                    msg,
                    user_message="The Telegram client is already running.",
                    context={"state": self._state.value},
                )
            self._state = ClientState.STARTING

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self._queue_capacity)
        self._ended = asyncio.Event()
        self._stopping.clear()

        self._client_id = self._library.create_client_id()
        self._thread = threading.Thread(target=self._receive_loop, name=THREAD_NAME, daemon=True)
        self._thread.start()

        with self._state_lock:
            self._state = ClientState.RUNNING

        logger.info("tdlib_client_started", client_id=self._client_id)

    async def close(self) -> None:
        """Stop the receive thread and release every waiter.

        Deterministic and idempotent: calling it twice is not an error, and it
        returns only once the thread has stopped, every pending request has
        been failed, and :meth:`receive` has been unblocked.

        Raises:
            TdlibShutdownTimeoutError: If the receive thread did not stop. The
                client is still marked stopped and every waiter is released --
                a hung thread must not also hang the application -- but the
                caller is told, because a thread that will not stop is a defect
                rather than a tidy-up detail.
        """
        with self._state_lock:
            if self._state in {ClientState.STOPPED, ClientState.FAILED}:
                already_finished = self._thread is None or not self._thread.is_alive()
                if already_finished:
                    self._release_waiters("The Telegram client is closed.")
                    return
            self._state = ClientState.STOPPING

        self._stopping.set()

        thread = self._thread
        stalled = False
        if thread is not None and thread.is_alive():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, thread.join, self._shutdown_timeout)
            stalled = thread.is_alive()

        self._release_waiters("The Telegram client is closed.")

        with self._state_lock:
            if self._state is not ClientState.FAILED:
                self._state = ClientState.STOPPED

        logger.info(
            "tdlib_client_stopped",
            frames_received=self._frames,
            queue_high_water=self._high_water,
            stalled=stalled,
        )

        if stalled:
            msg = f"The {THREAD_NAME} thread did not stop within {self._shutdown_timeout}s"
            raise TdlibShutdownTimeoutError(
                msg,
                user_message="The Telegram client did not shut down cleanly.",
                context={"timeout_seconds": self._shutdown_timeout},
            )

    def health(self) -> ClientHealth:
        """Report what the client knows about itself.

        Never raises and never blocks, so it is safe to call while diagnosing a
        client that is misbehaving -- which is the only time anyone will.
        """
        with self._state_lock:
            state, failure = self._state, self._failure
        return ClientHealth(
            state=state,
            thread_alive=self._thread is not None and self._thread.is_alive(),
            client_id=self._client_id,
            frames_received=self._frames,
            updates_queued=self._queue.qsize() if self._queue is not None else 0,
            queue_capacity=self._queue_capacity,
            queue_high_water=self._high_water,
            pending_requests=len(self._pending),
            malformed_frames=self._malformed,
            failure=failure,
        )

    # -- Sending ----------------------------------------------------------

    def send(self, request: dict[str, Any]) -> None:
        """Hand a request to TDLib without waiting for a reply.

        Synchronous because ``td_send`` is: it queues the request inside TDLib
        and returns. Making it ``async`` would suggest it waits for something.

        Raises:
            TdlibNotRunningError: If the client is not running.
        """
        client_id = self._require_running("send")
        self._library.send(client_id, json.dumps(request))

    async def request(
        self,
        payload: dict[str, Any],
        *,
        timeout: float | None = DEFAULT_REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """Send a request and wait for its reply.

        Correlated by an ``@extra`` this client generates. A caller supplying
        its own would collide with the registry, so any is replaced.

        Raises:
            TdlibNotRunningError: If the client is not running.
            TdlibRequestFailedError: If TDLib answered with an error.
            TimeoutError: If no reply arrived in time. The pending entry is
                removed, so a late reply is discarded rather than resolving a
                future nobody holds.
        """
        client_id = self._require_running("request")
        loop = asyncio.get_running_loop()

        extra = f"r{next(self._ids)}"
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[extra] = future

        try:
            self._library.send(client_id, json.dumps({**payload, _EXTRA: extra}))
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(extra, None)

        if reply.get(_TYPE) == _ERROR_TYPE:
            code = reply.get("code")
            message = reply.get("message", "")
            msg = f"TDLib refused {payload.get(_TYPE, 'a request')}: {code} {message}"
            raise TdlibRequestFailedError(
                msg,
                user_message="Telegram refused that request.",
                # TDLib's own message travels too. It is a constant such as
                # PHONE_CODE_INVALID -- never user data -- and without it a
                # caller can only report that something was refused.
                context={
                    "request_type": payload.get(_TYPE),
                    "code": code,
                    "message": message,
                },
            )
        return reply

    # -- Receiving --------------------------------------------------------

    async def receive(self) -> dict[str, Any] | None:
        """Return the next update, or ``None`` once the stream has ended.

        Cancellable: awaiting this and cancelling leaves the client usable and
        loses nothing, because a cancelled wait never removed an item.
        """
        queue, ended = self._queue, self._ended
        if queue is None or ended is None:
            return None

        while True:
            if not queue.empty():
                return queue.get_nowait()
            if ended.is_set():
                return None

            getter = asyncio.ensure_future(queue.get())
            finisher = asyncio.ensure_future(ended.wait())
            try:
                await asyncio.wait({getter, finisher}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                getter.cancel()
                finisher.cancel()
                raise

            finisher.cancel()
            if getter.done() and not getter.cancelled():
                return getter.result()
            getter.cancel()
            # The stream ended. Loop once more to drain anything that arrived
            # between the check above and the event being set.

    # -- The receive thread -----------------------------------------------

    def _receive_loop(self) -> None:
        """Read frames from TDLib until told to stop. Runs on ``tgassist-td``.

        The only caller of ``td_receive`` in this application. It touches no
        application state directly: every frame is handed to the loop.
        """
        try:
            while not self._stopping.is_set():
                raw = self._library.receive(self._receive_timeout)
                if raw is None:
                    continue
                self._frames += 1
                frame = self._parse(raw)
                if frame is not None:
                    self._dispatch(frame)
        except BaseException as exc:
            self._fail(exc)
        finally:
            self._signal_end()

    def _parse(self, raw: str) -> dict[str, Any] | None:
        """Turn a frame into a mapping, or count it as malformed.

        A frame that is not a JSON object is counted and dropped rather than
        raised. One unparseable frame must not end a stream carrying every
        other update, and the count makes it visible if it is not a one-off.
        """
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            self._malformed += 1
            logger.warning("tdlib_frame_unparseable", length=len(raw))
            return None
        if not isinstance(document, dict):
            self._malformed += 1
            logger.warning("tdlib_frame_not_an_object", length=len(raw))
            return None
        return document

    def _dispatch(self, frame: dict[str, Any]) -> None:
        """Hand a frame to the loop: a reply resolves a future, an update queues.

        Only ``@type`` is ever logged. A TDLib frame can carry an authorization
        code, a session key or message text, and none of that may reach a log
        (``SECURITY.md`` section 9).
        """
        loop = self._loop
        if loop is None:  # pragma: no cover - set before the thread starts
            return

        extra = frame.get(_EXTRA)
        if isinstance(extra, str) and extra in self._pending:
            loop.call_soon_threadsafe(self._resolve, extra, frame)
            return

        self._enqueue(loop, frame)

    def _enqueue(self, loop: asyncio.AbstractEventLoop, frame: dict[str, Any]) -> None:
        """Put an update on the queue, waiting if it is full.

        This is the backpressure. Blocking here means the next ``td_receive``
        is delayed, so TDLib buffers internally instead of this process growing
        without bound. The wait is polled so a stop request is still noticed.
        """
        try:
            handle = asyncio.run_coroutine_threadsafe(self._put(frame), loop)
        except RuntimeError:
            # The loop closed under us during shutdown.
            return

        while not self._stopping.is_set():
            try:
                handle.result(timeout=_BACKPRESSURE_POLL)
            except concurrent.futures.TimeoutError:
                continue
            except (RuntimeError, asyncio.CancelledError):
                return
            else:
                return
        handle.cancel()

    async def _put(self, frame: dict[str, Any]) -> None:
        """Place one update on the queue. Runs on the loop."""
        queue = self._queue
        if queue is None:  # pragma: no cover - created before the thread starts
            return
        await queue.put(frame)
        self._high_water = max(self._high_water, queue.qsize())

    def _resolve(self, extra: str, frame: dict[str, Any]) -> None:
        """Complete a pending request. Runs on the loop."""
        future = self._pending.pop(extra, None)
        if future is not None and not future.done():
            future.set_result(frame)

    def _fail(self, exc: BaseException) -> None:
        """Record that the receive thread died. Runs on ``tgassist-td``."""
        with self._state_lock:
            self._state = ClientState.FAILED
            self._failure = f"{type(exc).__name__}: {exc}"
        logger.error("tdlib_receive_thread_failed", error=type(exc).__name__)

    def _signal_end(self) -> None:
        """Release everything waiting on the stream. Runs on ``tgassist-td``.

        The end is announced through an event rather than a value on the queue,
        because a full queue -- exactly the state a stalled consumer leaves it
        in -- would swallow an in-band sentinel.
        """
        loop = self._loop
        if loop is None:  # pragma: no cover
            return
        try:
            loop.call_soon_threadsafe(self._on_ended)
        except RuntimeError:  # pragma: no cover - loop already closed
            return

    def _on_ended(self) -> None:
        """Set the end flag and fail anything still waiting. Runs on the loop."""
        if self._ended is not None:
            self._ended.set()
        with self._state_lock:
            failure = self._failure
        if failure is not None:
            self._release_waiters(f"The Telegram client stopped: {failure}")

    def _release_waiters(self, reason: str) -> None:
        """Fail every pending request so nothing waits on a dead client."""
        if self._ended is not None:
            self._ended.set()
        for extra, future in list(self._pending.items()):
            self._pending.pop(extra, None)
            if not future.done():
                future.set_exception(
                    TdlibNotRunningError(reason, user_message="The Telegram client stopped.")
                )

    def _require_running(self, operation: str) -> int:
        """Return the client identifier, or refuse because the client is not up."""
        with self._state_lock:
            state = self._state
        if state is not ClientState.RUNNING or self._client_id is None:
            msg = f"Cannot {operation} while the Telegram client is {state.value}"
            raise TdlibNotRunningError(
                msg,
                user_message="The Telegram client is not running.",
                context={"state": state.value, "operation": operation},
            )
        return self._client_id


__all__ = [
    "DEFAULT_QUEUE_CAPACITY",
    "DEFAULT_RECEIVE_TIMEOUT",
    "DEFAULT_REQUEST_TIMEOUT",
    "DEFAULT_SHUTDOWN_TIMEOUT",
    "THREAD_NAME",
    "ClientHealth",
    "ClientState",
    "TdjsonClient",
]
