"""The bridge between TDLib's thread and the asyncio application.

Deterministic without a Telegram account, a network or the real library: the
fake blocks in ``receive`` exactly as ``td_receive`` does, so the client's
thread behaves here as it will in production.

The one thing these cannot prove is that the real ``tdjson`` honours the same
contract. ``TestRealTdjsonBridge`` at the end does that, and skips when no
verified binary is present.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.tdjson import FakeTdjson
from tgassist.domain.errors import (
    TdlibNotRunningError,
    TdlibRequestFailedError,
)
from tgassist.infrastructure.config import load_config
from tgassist.infrastructure.telegram import LoaderSettings, TdjsonLoader
from tgassist.infrastructure.telegram.client import (
    THREAD_NAME,
    ClientState,
    TdjsonClient,
)

# Short enough that a test never waits on a timeout it does not care about,
# long enough that a loaded machine does not fail spuriously.
FAST_RECEIVE = 0.05
FAST_SHUTDOWN = 5.0


def build(library: FakeTdjson, *, capacity: int = 16) -> TdjsonClient:
    """Build a client with test-scale timings."""
    return TdjsonClient(
        library,
        queue_capacity=capacity,
        receive_timeout=FAST_RECEIVE,
        shutdown_timeout=FAST_SHUTDOWN,
    )


@pytest.fixture
async def library() -> FakeTdjson:
    """A scriptable fake TDLib."""
    return FakeTdjson()


@pytest.fixture
async def client(library: FakeTdjson) -> AsyncIterator[TdjsonClient]:
    """A started client, always closed afterwards."""
    started = build(library)
    await started.start()
    try:
        yield started
    finally:
        await started.close()


async def drain(client: TdjsonClient, count: int, *, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Collect ``count`` updates, failing rather than hanging."""

    async def collect() -> list[dict[str, Any]]:
        received: list[dict[str, Any]] = []
        while len(received) < count:
            update = await client.receive()
            if update is None:
                break
            received.append(update)
        return received

    return await asyncio.wait_for(collect(), timeout)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_a_new_client_is_stopped(self, library: FakeTdjson) -> None:
        assert build(library).state is ClientState.STOPPED

    async def test_starting_reaches_running(self, client: TdjsonClient) -> None:
        assert client.state is ClientState.RUNNING

    async def test_starting_creates_a_tdlib_client(self, client: TdjsonClient) -> None:
        assert client.health().client_id is not None

    async def test_the_receive_thread_is_named(self, client: TdjsonClient) -> None:
        # Named so a stack dump or profiler attributes the blocking call to the
        # component that owns it.
        assert client.state is ClientState.RUNNING
        assert THREAD_NAME in {thread.name for thread in threading.enumerate()}

    async def test_closing_reaches_stopped(self, library: FakeTdjson) -> None:
        started = build(library)
        await started.start()

        await started.close()

        assert started.state is ClientState.STOPPED

    async def test_closing_stops_the_thread(self, library: FakeTdjson) -> None:
        started = build(library)
        await started.start()

        await started.close()

        assert THREAD_NAME not in {thread.name for thread in threading.enumerate()}
        assert not started.health().thread_alive

    async def test_closing_twice_is_not_an_error(self, library: FakeTdjson) -> None:
        # Shutdown paths run from error handlers, where a second call is normal.
        started = build(library)
        await started.start()

        await started.close()
        await started.close()

        assert started.state is ClientState.STOPPED

    async def test_closing_without_starting_is_not_an_error(self, library: FakeTdjson) -> None:
        await build(library).close()

    async def test_restart_is_refused(self, library: FakeTdjson) -> None:
        # Deliberately unsupported: a closed client's TDLib identifier is dead,
        # and nothing needs the edge.
        started = build(library)
        await started.start()
        await started.close()

        with pytest.raises(TdlibNotRunningError):
            await started.start()

    async def test_starting_twice_is_refused(self, client: TdjsonClient) -> None:
        with pytest.raises(TdlibNotRunningError):
            await client.start()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


class TestSending:
    async def test_send_reaches_the_library(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        client.send({"@type": "getOption", "name": "version"})

        assert library.sent[-1]["@type"] == "getOption"

    async def test_send_carries_the_client_id(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        client.send({"@type": "close"})

        assert library.sent[-1]["@client_id"] == client.health().client_id

    async def test_sending_before_start_is_refused(self, library: FakeTdjson) -> None:
        with pytest.raises(TdlibNotRunningError, match="stopped"):
            build(library).send({"@type": "close"})

    async def test_sending_after_close_is_refused(self, library: FakeTdjson) -> None:
        started = build(library)
        await started.start()
        await started.close()

        with pytest.raises(TdlibNotRunningError):
            started.send({"@type": "close"})


class TestRequestResponse:
    async def test_a_request_gets_its_reply(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        library.reply_to("getOption", {"@type": "optionValueString", "value": "1.8.66"})

        reply = await asyncio.wait_for(
            client.request({"@type": "getOption", "name": "version"}), 2.0
        )

        assert reply["value"] == "1.8.66"

    async def test_the_request_carries_a_generated_extra(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        # Correlation is by an identifier this client issues, so two concurrent
        # requests cannot be confused for one another.
        library.reply_to("getOption", {"@type": "ok"})

        await asyncio.wait_for(client.request({"@type": "getOption"}), 2.0)

        assert library.sent[-1]["@extra"]

    async def test_a_callers_own_extra_is_replaced(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        # A caller-supplied value would collide with the registry.
        library.reply_to("getOption", {"@type": "ok"})

        await asyncio.wait_for(client.request({"@type": "getOption", "@extra": "mine"}), 2.0)

        assert library.sent[-1]["@extra"] != "mine"

    async def test_concurrent_requests_get_their_own_replies(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        # The fake replies with the request's own @extra, so this exercises the
        # real correlation rather than arrival order.
        library.reply_to("getOption", {"@type": "optionValueString", "value": "a"})
        library.reply_to("getMe", {"@type": "user", "value": "b"})

        first, second = await asyncio.wait_for(
            asyncio.gather(
                client.request({"@type": "getOption"}),
                client.request({"@type": "getMe"}),
            ),
            2.0,
        )

        assert first["value"] == "a"
        assert second["value"] == "b"

    async def test_a_tdlib_error_becomes_a_typed_error(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        library.reply_to("getMe", {"@type": "error", "code": 401, "message": "UNAUTHORIZED"})

        with pytest.raises(TdlibRequestFailedError, match="401") as excinfo:
            await asyncio.wait_for(client.request({"@type": "getMe"}), 2.0)

        assert excinfo.value.context["code"] == 401

    async def test_a_request_that_is_never_answered_times_out(self, client: TdjsonClient) -> None:
        with pytest.raises(TimeoutError):
            await client.request({"@type": "getMe"}, timeout=0.2)

    async def test_a_timed_out_request_is_forgotten(self, client: TdjsonClient) -> None:
        # Otherwise a late reply resolves a future nobody holds, and the
        # registry grows for the life of the process.
        with pytest.raises(TimeoutError):
            await client.request({"@type": "getMe"}, timeout=0.2)

        assert client.health().pending_requests == 0

    async def test_a_reply_to_a_forgotten_request_becomes_an_update(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        # It is no longer correlated, so it must not be dropped silently.
        with pytest.raises(TimeoutError):
            await client.request({"@type": "getMe"}, timeout=0.2)
        extra = library.sent[-1]["@extra"]
        library.push({"@type": "user", "@extra": extra})

        received = await drain(client, 1)

        assert received[0]["@type"] == "user"

    async def test_requesting_before_start_is_refused(self, library: FakeTdjson) -> None:
        with pytest.raises(TdlibNotRunningError):
            await build(library).request({"@type": "getMe"})


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------


class TestReceiving:
    async def test_an_update_arrives_in_asyncio(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        library.push({"@type": "updateOption", "name": "version"})

        received = await drain(client, 1)

        assert received[0]["@type"] == "updateOption"

    async def test_updates_arrive_in_order(self, client: TdjsonClient, library: FakeTdjson) -> None:
        for index in range(5):
            library.push({"@type": "updateOption", "index": index})

        received = await drain(client, 5)

        assert [update["index"] for update in received] == [0, 1, 2, 3, 4]

    async def test_a_reply_is_not_delivered_as_an_update(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        # Otherwise every awaited request would also appear in the update
        # stream, and consumers would have to filter what they never asked for.
        library.reply_to("getOption", {"@type": "optionValueString", "value": "x"})
        await asyncio.wait_for(client.request({"@type": "getOption"}), 2.0)
        library.push({"@type": "updateOption"})

        received = await drain(client, 1)

        assert received[0]["@type"] == "updateOption"

    async def test_receive_returns_none_once_closed(self, library: FakeTdjson) -> None:
        started = build(library)
        await started.start()
        await started.close()

        assert await asyncio.wait_for(started.receive(), 2.0) is None

    async def test_a_blocked_receive_is_released_by_close(self, library: FakeTdjson) -> None:
        # The end signal has to reach a consumer that is already waiting, or
        # shutdown deadlocks against its own reader.
        started = build(library)
        await started.start()
        waiting = asyncio.ensure_future(started.receive())
        await asyncio.sleep(0)

        await started.close()

        assert await asyncio.wait_for(waiting, 2.0) is None

    async def test_updates_queued_before_close_are_still_delivered(
        self, library: FakeTdjson
    ) -> None:
        # Shutdown must not discard what has already been received.
        started = build(library)
        await started.start()
        library.push({"@type": "updateOption"})
        await asyncio.sleep(0.2)

        await started.close()

        assert (await asyncio.wait_for(started.receive(), 2.0)) is not None
        assert (await asyncio.wait_for(started.receive(), 2.0)) is None

    async def test_receive_is_cancellable(self, client: TdjsonClient) -> None:
        waiting = asyncio.ensure_future(client.receive())
        await asyncio.sleep(0)

        waiting.cancel()

        with pytest.raises(asyncio.CancelledError):
            await waiting

    async def test_a_cancelled_receive_loses_nothing(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        # A cancelled wait must not consume an update it never returned.
        waiting = asyncio.ensure_future(client.receive())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

        library.push({"@type": "updateOption", "index": 0})
        received = await drain(client, 1)

        assert received[0]["index"] == 0


# ---------------------------------------------------------------------------
# Queue and backpressure
# ---------------------------------------------------------------------------


class TestQueueBehaviour:
    async def test_the_queue_fills_to_capacity(self, library: FakeTdjson) -> None:
        started = build(library, capacity=4)
        await started.start()
        try:
            for index in range(4):
                library.push({"@type": "updateOption", "index": index})
            await asyncio.sleep(0.3)

            assert started.health().updates_queued == 4
            assert started.health().is_saturated
        finally:
            await started.close()

    async def test_the_receive_thread_blocks_rather_than_overfilling(
        self, library: FakeTdjson
    ) -> None:
        # The backpressure ADR-048 specifies: TDLib buffers internally instead
        # of this process growing an unbounded Python queue.
        started = build(library, capacity=4)
        await started.start()
        try:
            for index in range(20):
                library.push({"@type": "updateOption", "index": index})
            await asyncio.sleep(0.3)

            assert started.health().updates_queued <= 4
        finally:
            await started.close()

    async def test_draining_lets_the_thread_resume(self, library: FakeTdjson) -> None:
        started = build(library, capacity=4)
        await started.start()
        try:
            for index in range(10):
                library.push({"@type": "updateOption", "index": index})

            received = await drain(started, 10, timeout=5.0)

            assert [update["index"] for update in received] == list(range(10))
        finally:
            await started.close()

    async def test_the_high_water_mark_is_reported(self, library: FakeTdjson) -> None:
        # A queue that filled once and drained looks identical to one that never
        # filled, unless the peak is remembered.
        started = build(library, capacity=4)
        await started.start()
        try:
            for index in range(4):
                library.push({"@type": "updateOption", "index": index})
            await asyncio.sleep(0.3)
            await drain(started, 4)

            assert started.health().updates_queued == 0
            assert started.health().queue_high_water == 4
        finally:
            await started.close()


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


class TestMalformedFrames:
    async def test_unparseable_json_does_not_end_the_stream(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        # One bad frame must not cost every update behind it.
        library.push("{not json at all")
        library.push({"@type": "updateOption", "index": 1})

        received = await drain(client, 1)

        assert received[0]["index"] == 1

    async def test_a_non_object_frame_does_not_end_the_stream(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        library.push("[1, 2, 3]")
        library.push({"@type": "updateOption", "index": 1})

        received = await drain(client, 1)

        assert received[0]["index"] == 1

    async def test_malformed_frames_are_counted(
        self, client: TdjsonClient, library: FakeTdjson
    ) -> None:
        # Counted rather than silent: one is noise, a stream of them is a bug.
        library.push("{broken")
        library.push("also broken")
        library.push({"@type": "updateOption"})
        await drain(client, 1)

        assert client.health().malformed_frames == 2


class TestReceiveThreadFailure:
    async def test_a_dying_thread_puts_the_client_in_failed(self, library: FakeTdjson) -> None:
        started = build(library)
        await started.start()
        try:
            library.fail_receive(OSError("the library went away"))
            await asyncio.sleep(0.3)

            assert started.state is ClientState.FAILED
        finally:
            await started.close()

    async def test_the_failure_is_reported(self, library: FakeTdjson) -> None:
        started = build(library)
        await started.start()
        try:
            library.fail_receive(OSError("the library went away"))
            await asyncio.sleep(0.3)

            failure = started.health().failure
            assert failure is not None
            assert "OSError" in failure
        finally:
            await started.close()

    async def test_a_dying_thread_releases_a_waiting_receiver(self, library: FakeTdjson) -> None:
        # Otherwise a consumer waits forever on a stream that has no producer.
        started = build(library)
        await started.start()
        try:
            waiting = asyncio.ensure_future(started.receive())
            await asyncio.sleep(0)
            library.fail_receive(OSError("gone"))

            assert await asyncio.wait_for(waiting, 2.0) is None
        finally:
            await started.close()

    async def test_a_dying_thread_fails_pending_requests(self, library: FakeTdjson) -> None:
        started = build(library)
        await started.start()
        try:
            pending = asyncio.ensure_future(started.request({"@type": "getMe"}))
            await asyncio.sleep(0)
            library.fail_receive(OSError("gone"))

            with pytest.raises(TdlibNotRunningError):
                await asyncio.wait_for(pending, 2.0)
        finally:
            await started.close()

    async def test_closing_a_failed_client_keeps_it_failed(self, library: FakeTdjson) -> None:
        # "Never started" and "died" need different responses, so close must not
        # erase the distinction.
        started = build(library)
        await started.start()
        library.fail_receive(OSError("gone"))
        await asyncio.sleep(0.3)

        await started.close()

        assert started.state is ClientState.FAILED


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_it_counts_frames(self, client: TdjsonClient, library: FakeTdjson) -> None:
        for _ in range(3):
            library.push({"@type": "updateOption"})
        await drain(client, 3)

        assert client.health().frames_received == 3

    async def test_it_reports_pending_requests(self, client: TdjsonClient) -> None:
        pending = asyncio.ensure_future(client.request({"@type": "getMe"}, timeout=1.0))
        await asyncio.sleep(0.05)

        assert client.health().pending_requests == 1

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    async def test_it_never_raises_on_a_stopped_client(self, library: FakeTdjson) -> None:
        # Health is called precisely when something is wrong, so it must work
        # in every state including the ones nobody planned for.
        stopped = build(library)

        health = stopped.health()

        assert health.state is ClientState.STOPPED
        assert health.client_id is None


# ---------------------------------------------------------------------------
# The real library
# ---------------------------------------------------------------------------


@pytest.fixture
def real_library() -> object:
    """Load the verified `tdjson`, or skip.

    Deterministic: it loads a local file and exchanges one message that TDLib
    answers from memory. No network, no account, no Telegram state.
    """
    loader = TdjsonLoader(LoaderSettings(data_dir=_default_data_dir()))
    runtime = loader.inspect()
    if not runtime.is_usable:
        pytest.skip(f"no verified tdjson: {runtime.problem}")
    library, _ = loader.load()
    return library


def _default_data_dir() -> Path:
    """Return the data directory the application would use."""
    return load_config().config.paths.data_dir


class TestRealTdjsonBridge:
    """The bridge against real native code.

    Skipped where no verified binary is recorded, which is every machine that
    has not built one -- including CI. What it proves is the one thing the fake
    cannot: that the real library honours the contract the fake models.
    """

    async def test_a_round_trip_through_the_real_library(self, real_library: object) -> None:
        client = TdjsonClient(
            real_library,  # type: ignore[arg-type]
            queue_capacity=64,
            receive_timeout=0.2,
            shutdown_timeout=FAST_SHUTDOWN,
        )
        await client.start()
        try:
            # getOption for a static value is answered from memory, with no
            # network and no authorization, so this stays deterministic.
            reply = await client.request({"@type": "getOption", "name": "version"}, timeout=10.0)

            assert reply["@type"] == "optionValueString"
            assert reply["value"]
        finally:
            await client.close()

        assert client.state is ClientState.STOPPED
        assert not client.health().thread_alive


class TestThreadOwnership:
    """``td_receive`` has exactly one caller, and that is enforced.

    ADR-048's central claim is that one thread owns the blocking call. A second
    caller anywhere would break it silently -- ``td_receive`` from two threads
    is undefined behaviour, not an error -- so the constraint is asserted here
    rather than trusted to review.
    """

    def test_only_the_client_calls_receive_on_the_library(self) -> None:
        # Matches the *library* call specifically. An earlier version searched
        # for ".receive(" and so also caught consumers of the client's own
        # async queue, which is a different call with none of the same danger.
        source_root = Path(__file__).resolve().parents[2] / "src" / "tgassist"
        callers = {
            path.relative_to(source_root).as_posix()
            for path in source_root.rglob("*.py")
            if "_library.receive(" in path.read_text(encoding="utf-8")
        }

        assert callers == {"infrastructure/telegram/client.py"}

    def test_only_one_component_consumes_the_client_stream(self) -> None:
        # The client's queue has one item per update, so a second consumer would
        # not duplicate the stream -- it would *split* it, and each consumer
        # would silently miss whatever the other took first.
        source_root = Path(__file__).resolve().parents[2] / "src" / "tgassist"
        consumers = {
            path.relative_to(source_root).as_posix()
            for path in source_root.rglob("*.py")
            if "_client.receive(" in path.read_text(encoding="utf-8")
        }

        assert consumers == {"infrastructure/telegram/gateway.py"}

    def test_the_client_calls_it_only_from_the_receive_loop(self) -> None:
        # Within the client, the call must sit in the thread body. Anywhere
        # else would mean the loop thread could reach it.
        source = (
            Path(__file__).resolve().parents[2] / "src/tgassist/infrastructure/telegram/client.py"
        ).read_text(encoding="utf-8")
        body = source[source.index("def _receive_loop") : source.index("def _parse")]

        assert source.count("self._library.receive(") == 1
        assert "self._library.receive(" in body

    def test_the_loop_thread_only_sends(self) -> None:
        # td_send is thread-safe by TDLib's contract, which is why requests go
        # straight from the loop and only receipt needs a thread.
        source = (
            Path(__file__).resolve().parents[2] / "src/tgassist/infrastructure/telegram/client.py"
        ).read_text(encoding="utf-8")

        assert "self._library.send(" in source
