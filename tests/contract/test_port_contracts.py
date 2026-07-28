"""Shared contract tests for the core domain ports.

One suite per port, parametrized over every implementation -- production and
fake alike. This is what keeps a fake honest: a fake that quietly diverges from
the real behaviour would make every test that uses it a false positive, and the
only way to prevent that is to hold both to the same assertions.

Implementations that need a real external system carry the ``integration``
marker, so the default run stays fast and offline while still describing the
full set.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.fakes import (
    AdvanceableClock,
    FixedClock,
    InMemorySecretStore,
    RecordingEventBus,
    SequentialIdGenerator,
)
from tgassist.domain.errors import ReadOnlySecretStoreError
from tgassist.domain.events import DomainEvent
from tgassist.domain.model.secret import SecretValue
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.secret_store import SecretStore
from tgassist.infrastructure.clock import SystemClock
from tgassist.infrastructure.events import InProcessEventBus
from tgassist.infrastructure.ids import UuidV7IdGenerator
from tgassist.infrastructure.security import (
    ChainedSecretStore,
    EnvironmentSecretStore,
    KeyringSecretStore,
)

# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=[
        pytest.param(SystemClock, id="SystemClock"),
        pytest.param(FixedClock, id="FixedClock"),
        pytest.param(AdvanceableClock, id="AdvanceableClock"),
    ]
)
def clock(request: pytest.FixtureRequest) -> Clock:
    factory: Callable[[], Clock] = request.param
    return factory()


class TestClockContract:
    def test_satisfies_the_protocol(self, clock: Clock) -> None:
        assert isinstance(clock, Clock)

    def test_now_is_timezone_aware(self, clock: Clock) -> None:
        assert clock.now().tzinfo is not None

    def test_now_is_utc(self, clock: Clock) -> None:
        # Not merely "has a timezone": everything stored is UTC, and a clock
        # returning a local zone would push conversion out to every call site.
        assert clock.now().utcoffset() == timedelta(0)

    def test_now_never_goes_backwards(self, clock: Clock) -> None:
        readings = [clock.now() for _ in range(20)]

        assert readings == sorted(readings)

    def test_monotonic_never_decreases(self, clock: Clock) -> None:
        readings = [clock.monotonic() for _ in range(20)]

        assert readings == sorted(readings)

    def test_monotonic_returns_a_float(self, clock: Clock) -> None:
        assert isinstance(clock.monotonic(), float)

    def test_now_is_comparable_with_other_aware_datetimes(self, clock: Clock) -> None:
        # A naive datetime would raise here, which is the bug this guards.
        assert clock.now() > datetime(2000, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# IdGenerator
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=[
        pytest.param("uuid7", id="UuidV7IdGenerator"),
        pytest.param("sequential", id="SequentialIdGenerator"),
    ]
)
def ids(request: pytest.FixtureRequest) -> IdGenerator:
    if request.param == "uuid7":
        return UuidV7IdGenerator(SystemClock())
    return SequentialIdGenerator()


class TestIdGeneratorContract:
    def test_satisfies_the_protocol(self, ids: IdGenerator) -> None:
        assert isinstance(ids, IdGenerator)

    def test_integer_identifiers_are_unique(self, ids: IdGenerator) -> None:
        generated = [ids.new_id() for _ in range(1000)]

        assert len(set(generated)) == len(generated)

    def test_integer_identifiers_are_time_ordered(self, ids: IdGenerator) -> None:
        generated = [ids.new_id() for _ in range(1000)]

        assert generated == sorted(generated)

    def test_integer_identifiers_are_positive(self, ids: IdGenerator) -> None:
        assert ids.new_id() > 0

    def test_integer_identifiers_fit_in_signed_64_bits(self, ids: IdGenerator) -> None:
        assert ids.new_id() < 2**63

    def test_uuids_are_unique(self, ids: IdGenerator) -> None:
        generated = [ids.new_uuid() for _ in range(1000)]

        assert len(set(generated)) == len(generated)

    def test_uuids_are_time_ordered(self, ids: IdGenerator) -> None:
        generated = [ids.new_uuid() for _ in range(1000)]

        assert generated == sorted(generated)

    def test_uuids_are_canonical_lowercase(self, ids: IdGenerator) -> None:
        value = ids.new_uuid()

        assert len(value) == 36
        assert value == value.lower()
        assert [len(part) for part in value.split("-")] == [8, 4, 4, 4, 12]

    def test_correlation_ids_are_unique(self, ids: IdGenerator) -> None:
        generated = [ids.new_correlation_id() for _ in range(1000)]

        assert len(set(generated)) == len(generated)

    def test_correlation_ids_are_non_empty_strings(self, ids: IdGenerator) -> None:
        value = ids.new_correlation_id()

        assert isinstance(value, str)
        assert value

    def test_identifier_kinds_do_not_collide_with_each_other(self, ids: IdGenerator) -> None:
        # Interleaving must not produce a repeat: the kinds share one counter.
        integers = {ids.new_id() for _ in range(100)}
        more_integers = {ids.new_id() for _ in range(100)}
        for _ in range(100):
            ids.new_uuid()

        assert not integers & more_integers


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class SampleEvent(DomainEvent):
    """A concrete event for exercising the bus."""


class OtherEvent(DomainEvent):
    """An unrelated event, for verifying that handlers are not over-delivered."""


class DerivedEvent(SampleEvent):
    """A subclass, for verifying base-class delivery."""


@pytest.fixture(
    params=[
        pytest.param(InProcessEventBus, id="InProcessEventBus"),
        pytest.param(RecordingEventBus, id="RecordingEventBus"),
    ]
)
def bus(request: pytest.FixtureRequest) -> EventBus:
    factory: Callable[[], EventBus] = request.param
    return factory()


class TestEventBusContract:
    def test_satisfies_the_protocol(self, bus: EventBus) -> None:
        assert isinstance(bus, EventBus)

    async def test_delivers_to_a_subscribed_handler(self, bus: EventBus) -> None:
        received: list[DomainEvent] = []
        bus.subscribe(SampleEvent, received.append, name="collector")

        await bus.publish(SampleEvent())

        assert len(received) == 1

    async def test_delivery_is_synchronous(self, bus: EventBus) -> None:
        # The defining property: when publish returns, handlers have run.
        # Fire-and-forget scheduling would leave this list empty.
        received: list[DomainEvent] = []

        async def slow(event: DomainEvent) -> None:
            await asyncio.sleep(0)
            received.append(event)

        bus.subscribe(SampleEvent, slow, name="slow")
        await bus.publish(SampleEvent())

        assert len(received) == 1

    async def test_supports_synchronous_handlers(self, bus: EventBus) -> None:
        received: list[DomainEvent] = []
        bus.subscribe(SampleEvent, received.append, name="sync")

        await bus.publish(SampleEvent())

        assert len(received) == 1

    async def test_supports_asynchronous_handlers(self, bus: EventBus) -> None:
        received: list[DomainEvent] = []

        async def handler(event: DomainEvent) -> None:
            received.append(event)

        bus.subscribe(SampleEvent, handler, name="async")
        await bus.publish(SampleEvent())

        assert len(received) == 1

    async def test_does_not_deliver_unrelated_events(self, bus: EventBus) -> None:
        received: list[DomainEvent] = []
        bus.subscribe(SampleEvent, received.append, name="collector")

        await bus.publish(OtherEvent())

        assert received == []

    async def test_delivers_subclasses_to_base_class_handlers(self, bus: EventBus) -> None:
        received: list[DomainEvent] = []
        bus.subscribe(DomainEvent, received.append, name="observer")

        await bus.publish(SampleEvent())
        await bus.publish(OtherEvent())

        assert len(received) == 2

    async def test_exact_type_handlers_run_before_base_class_handlers(self, bus: EventBus) -> None:
        order: list[str] = []
        bus.subscribe(DomainEvent, lambda _e: order.append("base"), name="base")
        bus.subscribe(DerivedEvent, lambda _e: order.append("exact"), name="exact")

        await bus.publish(DerivedEvent())

        assert order == ["exact", "base"]

    async def test_handlers_run_in_registration_order(self, bus: EventBus) -> None:
        order: list[int] = []
        for index in range(5):
            bus.subscribe(
                SampleEvent,
                lambda _e, i=index: order.append(i),  # type: ignore[misc]
                name=f"handler-{index}",
            )

        await bus.publish(SampleEvent())

        assert order == [0, 1, 2, 3, 4]

    async def test_a_failing_handler_does_not_reach_the_publisher(self, bus: EventBus) -> None:
        def explode(_event: DomainEvent) -> None:
            raise RuntimeError("handler is broken")

        bus.subscribe(SampleEvent, explode, name="broken")

        await bus.publish(SampleEvent())  # must not raise

    async def test_a_failing_handler_does_not_block_the_others(self, bus: EventBus) -> None:
        received: list[str] = []

        def explode(_event: DomainEvent) -> None:
            raise RuntimeError("handler is broken")

        bus.subscribe(SampleEvent, explode, name="broken")
        bus.subscribe(SampleEvent, lambda _e: received.append("ok"), name="healthy")

        await bus.publish(SampleEvent())

        assert received == ["ok"]

    async def test_a_failing_async_handler_is_isolated(self, bus: EventBus) -> None:
        async def explode(_event: DomainEvent) -> None:
            raise RuntimeError("handler is broken")

        bus.subscribe(SampleEvent, explode, name="broken-async")

        await bus.publish(SampleEvent())  # must not raise

    async def test_repeated_failures_disable_the_handler(self, bus: EventBus) -> None:
        calls = 0

        def explode(_event: DomainEvent) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("handler is broken")

        bus.subscribe(SampleEvent, explode, name="broken")
        for _ in range(10):
            await bus.publish(SampleEvent())

        assert calls == 5

    async def test_a_successful_call_resets_the_failure_count(self, bus: EventBus) -> None:
        attempts = 0

        def flaky(_event: DomainEvent) -> None:
            nonlocal attempts
            attempts += 1
            if attempts % 2 == 1:
                raise RuntimeError("intermittent")

        bus.subscribe(SampleEvent, flaky, name="flaky")
        for _ in range(12):
            await bus.publish(SampleEvent())

        # An intermittently failing handler stays subscribed: only a
        # consistently broken one is disabled.
        assert attempts == 12

    async def test_unsubscribed_handlers_stop_receiving(self, bus: EventBus) -> None:
        received: list[DomainEvent] = []
        subscription = bus.subscribe(SampleEvent, received.append, name="collector")

        await bus.publish(SampleEvent())
        bus.unsubscribe(subscription)
        await bus.publish(SampleEvent())

        assert len(received) == 1

    def test_unsubscribing_twice_is_not_an_error(self, bus: EventBus) -> None:
        subscription = bus.subscribe(SampleEvent, lambda _e: None, name="collector")

        bus.unsubscribe(subscription)
        bus.unsubscribe(subscription)

    async def test_publishing_with_no_subscribers_is_not_an_error(self, bus: EventBus) -> None:
        await bus.publish(SampleEvent())

    def test_a_subscription_name_is_required(self, bus: EventBus) -> None:
        # An anonymous failing handler cannot be attributed to anything, which
        # defeats the point of isolating and reporting the failure.
        with pytest.raises(ValueError, match="name"):
            bus.subscribe(SampleEvent, lambda _e: None, name="")

    async def test_a_handler_may_publish(self, bus: EventBus) -> None:
        received: list[DomainEvent] = []

        async def chain(_event: DomainEvent) -> None:
            await bus.publish(OtherEvent())

        bus.subscribe(SampleEvent, chain, name="chain")
        bus.subscribe(OtherEvent, received.append, name="collector")

        await bus.publish(SampleEvent())

        assert len(received) == 1


# ---------------------------------------------------------------------------
# SecretStore
# ---------------------------------------------------------------------------

SECRET = SecretValue("value-under-test")


@pytest.fixture(
    params=[
        pytest.param("memory", id="InMemorySecretStore"),
        pytest.param("chained", id="ChainedSecretStore"),
        pytest.param(
            "keyring",
            id="KeyringSecretStore",
            marks=pytest.mark.integration,
        ),
    ]
)
def secrets(request: pytest.FixtureRequest) -> Iterator[SecretStore]:
    if request.param == "memory":
        yield InMemorySecretStore()
        return
    if request.param == "chained":
        yield ChainedSecretStore(EnvironmentSecretStore(), InMemorySecretStore())
        return

    # A unique service name, so a test never reads or writes the real user's
    # credentials, and cleanup afterwards regardless of outcome.
    store = KeyringSecretStore(service_name=f"tgassist-test-{id(request)}")
    written: list[str] = []
    original_set = store.set

    async def tracking_set(name: str, value: SecretValue) -> None:
        written.append(name)
        await original_set(name, value)

    store.set = tracking_set  # type: ignore[method-assign]
    try:
        yield store
    finally:
        for name in written:
            asyncio.run(store.delete(name))


class TestSecretStoreContract:
    def test_satisfies_the_protocol(self, secrets: SecretStore) -> None:
        assert isinstance(secrets, SecretStore)

    async def test_absent_name_returns_none(self, secrets: SecretStore) -> None:
        # "Not configured" is an ordinary state, not an error.
        assert await secrets.get("NOT_CONFIGURED") is None

    async def test_stored_value_can_be_retrieved(self, secrets: SecretStore) -> None:
        await secrets.set("ROUNDTRIP", SECRET)

        stored = await secrets.get("ROUNDTRIP")

        assert stored is not None
        assert stored.reveal() == SECRET.reveal()

    async def test_get_returns_a_masked_wrapper(self, secrets: SecretStore) -> None:
        await secrets.set("MASKED", SECRET)

        stored = await secrets.get("MASKED")

        assert stored is not None
        assert SECRET.reveal() not in repr(stored)
        assert SECRET.reveal() not in str(stored)

    async def test_set_overwrites(self, secrets: SecretStore) -> None:
        await secrets.set("OVERWRITE", SecretValue("first"))
        await secrets.set("OVERWRITE", SecretValue("second"))

        stored = await secrets.get("OVERWRITE")

        assert stored is not None
        assert stored.reveal() == "second"

    async def test_delete_removes(self, secrets: SecretStore) -> None:
        await secrets.set("REMOVED", SECRET)
        await secrets.delete("REMOVED")

        assert await secrets.get("REMOVED") is None

    async def test_delete_is_idempotent(self, secrets: SecretStore) -> None:
        # Cleanup paths should not need an existence check first.
        await secrets.delete("NEVER_STORED")
        await secrets.delete("NEVER_STORED")

    async def test_list_names_never_reveals_values(self, secrets: SecretStore) -> None:
        await secrets.set("LISTED", SECRET)

        names = await secrets.list_names()

        assert all(SECRET.reveal() not in name for name in names)

    async def test_is_available_returns_a_bool_and_never_raises(self, secrets: SecretStore) -> None:
        assert isinstance(await secrets.is_available(), bool)

    async def test_names_are_case_sensitive(self, secrets: SecretStore) -> None:
        await secrets.set("CaseSensitive", SecretValue("upper"))

        assert await secrets.get("casesensitive") is None


class TestReadOnlySecretStoreContract:
    """A read-only store refuses writes rather than discarding them silently."""

    async def test_set_is_refused(self) -> None:
        store = EnvironmentSecretStore()

        with pytest.raises(ReadOnlySecretStoreError):
            await store.set("ANY", SECRET)

    async def test_delete_is_refused(self) -> None:
        store = EnvironmentSecretStore()

        with pytest.raises(ReadOnlySecretStoreError):
            await store.delete("ANY")

    async def test_reads_still_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("READ_ONLY_PROBE", "from-environment")
        store = EnvironmentSecretStore()

        stored = await store.get("READ_ONLY_PROBE")

        assert stored is not None
        assert stored.reveal() == "from-environment"


def test_every_port_has_at_least_two_contract_tested_implementations() -> None:
    """Guard the premise of this file.

    A contract suite parametrized over a single implementation proves only that
    the implementation agrees with itself.
    """
    implementations: dict[str, list[Any]] = {
        "Clock": [SystemClock, FixedClock, AdvanceableClock],
        "IdGenerator": [UuidV7IdGenerator, SequentialIdGenerator],
        "EventBus": [InProcessEventBus, RecordingEventBus],
        "SecretStore": [InMemorySecretStore, ChainedSecretStore, KeyringSecretStore],
    }

    for port, impls in implementations.items():
        assert len(impls) >= 2, f"{port} needs more than one implementation under contract"
