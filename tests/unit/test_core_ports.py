"""Edge cases specific to individual port implementations.

Behaviour every implementation shares lives in ``tests/contract``. This file
covers what one implementation guarantees and another does not.
"""

from __future__ import annotations

import asyncio
import os
import pickle
import threading
import uuid as uuid_module
from datetime import UTC, datetime, timedelta, timezone

import pytest
import structlog
import structlog.testing

from tests.fakes import (
    EPOCH,
    AdvanceableClock,
    FixedClock,
    InMemorySecretStore,
    SequentialIdGenerator,
    UnavailableSecretStore,
)
from tgassist.application.container import Container
from tgassist.domain.errors import (
    EventDispatchError,
    ReadOnlySecretStoreError,
    SecretStoreUnavailableError,
)
from tgassist.domain.events import DomainEvent
from tgassist.domain.model.secret import MASK, SecretValue
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.secret_store import SecretStore
from tgassist.infrastructure.clock import SystemClock
from tgassist.infrastructure.config import AppConfig, LoadedConfig
from tgassist.infrastructure.events import InProcessEventBus
from tgassist.infrastructure.ids import UuidV7IdGenerator
from tgassist.infrastructure.logging import redact_entry
from tgassist.infrastructure.security import (
    ChainedSecretStore,
    EnvironmentSecretStore,
    KeyringSecretStore,
)


class SampleEvent(DomainEvent):
    """A concrete event for exercising the bus."""


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


class TestSystemClock:
    def test_tracks_real_time(self) -> None:
        before = datetime.now(UTC)

        observed = SystemClock().now()

        assert before <= observed <= datetime.now(UTC)

    def test_monotonic_is_independent_of_wall_time(self) -> None:
        clock = SystemClock()
        first = clock.monotonic()
        second = clock.monotonic()

        assert second >= first


class TestFixedClock:
    def test_time_does_not_move(self) -> None:
        clock = FixedClock()

        assert clock.now() == clock.now() == EPOCH

    def test_converts_a_non_utc_instant_to_utc(self) -> None:
        tokyo = datetime(2026, 3, 1, 9, 0, tzinfo=timezone(timedelta(hours=9)))

        clock = FixedClock(tokyo)

        assert clock.now().utcoffset() == timedelta(0)
        assert clock.now() == tokyo

    def test_rejects_a_naive_instant(self) -> None:
        # A naive datetime is the bug this whole port exists to prevent.
        with pytest.raises(ValueError, match="timezone-aware"):
            FixedClock(datetime(2026, 1, 1))  # noqa: DTZ001


class TestAdvanceableClock:
    def test_advance_moves_wall_and_monotonic_together(self) -> None:
        clock = AdvanceableClock()
        start_wall, start_mono = clock.now(), clock.monotonic()

        clock.advance(timedelta(minutes=5))

        assert clock.now() - start_wall == timedelta(minutes=5)
        assert clock.monotonic() - start_mono == pytest.approx(300.0)

    def test_advance_refuses_to_go_backwards(self) -> None:
        clock = AdvanceableClock()

        with pytest.raises(ValueError, match="backwards"):
            clock.advance(timedelta(seconds=-1))

    def test_set_moves_wall_time_without_moving_monotonic(self) -> None:
        # Models a system clock correction: the calendar jumps, elapsed time
        # does not. Anything measuring a duration must be unaffected.
        clock = AdvanceableClock()
        monotonic_before = clock.monotonic()

        clock.set(EPOCH - timedelta(days=1))

        assert clock.now() < EPOCH
        assert clock.monotonic() == monotonic_before


# ---------------------------------------------------------------------------
# IdGenerator
# ---------------------------------------------------------------------------


class TestUuidV7IdGenerator:
    def test_uuid_reports_version_seven(self) -> None:
        value = uuid_module.UUID(UuidV7IdGenerator(SystemClock()).new_uuid())

        assert value.version == 7
        assert value.variant == uuid_module.RFC_4122

    def test_uuid_encodes_the_generation_time(self) -> None:
        clock = FixedClock(EPOCH)
        value = uuid_module.UUID(UuidV7IdGenerator(clock).new_uuid())

        timestamp_ms = value.int >> 80

        assert timestamp_ms == int(EPOCH.timestamp() * 1000)

    def test_identifiers_are_unique_under_a_frozen_clock(self) -> None:
        # The counter, not the clock, provides uniqueness within a millisecond.
        generator = UuidV7IdGenerator(FixedClock(EPOCH))

        generated = [generator.new_id() for _ in range(4000)]

        assert len(set(generated)) == 4000
        assert generated == sorted(generated)

    def test_counter_exhaustion_advances_the_logical_millisecond(self) -> None:
        # Drifting slightly ahead of wall time is always preferable to a
        # duplicate key.
        generator = UuidV7IdGenerator(FixedClock(EPOCH))
        expected_ms = int(EPOCH.timestamp() * 1000)

        generated = [generator.new_id() for _ in range(5000)]

        assert len(set(generated)) == 5000
        assert generated == sorted(generated)
        assert generated[-1] >> 12 > expected_ms

    def test_a_backwards_clock_never_produces_a_lower_identifier(self) -> None:
        clock = AdvanceableClock(EPOCH)
        generator = UuidV7IdGenerator(clock)
        before = generator.new_id()

        clock.set(EPOCH - timedelta(hours=1))
        after = generator.new_id()

        assert after > before

    def test_integer_and_uuid_identifiers_share_an_ordering(self) -> None:
        generator = UuidV7IdGenerator(SystemClock())
        first_int = generator.new_id()
        first_uuid = uuid_module.UUID(generator.new_uuid())
        second_int = generator.new_id()

        assert first_int >> 12 <= first_uuid.int >> 80
        assert second_int > first_int

    def test_is_thread_safe(self) -> None:
        generator = UuidV7IdGenerator(SystemClock())
        collected: list[list[int]] = []

        def worker() -> None:
            collected.append([generator.new_id() for _ in range(500)])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        flattened = [value for batch in collected for value in batch]
        assert len(set(flattened)) == len(flattened)


class TestSequentialIdGenerator:
    def test_starts_at_one(self) -> None:
        assert SequentialIdGenerator().new_id() == 1

    def test_honours_a_custom_start(self) -> None:
        assert SequentialIdGenerator(start=100).new_id() == 100

    def test_rejects_a_non_positive_start(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            SequentialIdGenerator(start=0)

    def test_reset_restores_the_initial_state(self) -> None:
        generator = SequentialIdGenerator()
        first = generator.new_id()
        generator.new_id()

        generator.reset()

        assert generator.new_id() == first


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class TestInProcessEventBus:
    async def test_a_publish_cycle_is_refused(self) -> None:
        bus = InProcessEventBus(max_depth=5)

        async def republish(_event: DomainEvent) -> None:
            await bus.publish(SampleEvent())

        bus.subscribe(SampleEvent, republish, name="cycle")

        with pytest.raises(EventDispatchError):
            await bus.publish(SampleEvent())

    async def test_depth_is_released_after_a_refusal(self) -> None:
        # Otherwise one cycle would poison the bus for every later publish.
        bus = InProcessEventBus(max_depth=3)

        async def republish(_event: DomainEvent) -> None:
            await bus.publish(SampleEvent())

        subscription = bus.subscribe(SampleEvent, republish, name="cycle")
        with pytest.raises(EventDispatchError):
            await bus.publish(SampleEvent())
        bus.unsubscribe(subscription)

        received: list[DomainEvent] = []
        bus.subscribe(SampleEvent, received.append, name="collector")
        await bus.publish(SampleEvent())

        assert len(received) == 1

    async def test_failure_threshold_is_configurable(self) -> None:
        calls = 0
        bus = InProcessEventBus(failure_threshold=2)

        def explode(_event: DomainEvent) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("broken")

        bus.subscribe(SampleEvent, explode, name="broken")
        for _ in range(10):
            await bus.publish(SampleEvent())

        assert calls == 2

    def test_subscription_count_tracks_registration(self) -> None:
        bus = InProcessEventBus()
        assert bus.subscription_count() == 0

        subscription = bus.subscribe(SampleEvent, lambda _e: None, name="one")
        bus.subscribe(SampleEvent, lambda _e: None, name="two")
        assert bus.subscription_count() == 2
        assert bus.subscription_count(SampleEvent) == 2

        bus.unsubscribe(subscription)
        assert bus.subscription_count() == 1

    async def test_a_handler_failure_is_logged_with_its_name(self) -> None:
        # Attribution is the point of requiring a subscription name: the user
        # must be able to tell which component or plugin to disable.
        bus = InProcessEventBus()

        def explode(_event: DomainEvent) -> None:
            raise RuntimeError("broken")

        bus.subscribe(SampleEvent, explode, name="the-culprit")

        with structlog.testing.capture_logs() as records:
            await bus.publish(SampleEvent())

        assert any(record.get("handler") == "the-culprit" for record in records)


# ---------------------------------------------------------------------------
# SecretValue
# ---------------------------------------------------------------------------


class TestSecretValue:
    def test_repr_is_masked(self) -> None:
        assert "hunter2" not in repr(SecretValue("hunter2"))

    def test_str_is_masked(self) -> None:
        assert str(SecretValue("hunter2")) == MASK

    def test_f_string_interpolation_is_masked(self) -> None:
        secret = SecretValue("hunter2")

        assert "hunter2" not in f"{secret}"
        assert "hunter2" not in f"{secret!r}"
        assert "hunter2" not in f"{secret:>40}"

    def test_reveal_returns_the_value(self) -> None:
        assert SecretValue("hunter2").reveal() == "hunter2"

    def test_equality_compares_content(self) -> None:
        assert SecretValue("a") == SecretValue("a")
        assert SecretValue("a") != SecretValue("b")

    def test_is_not_equal_to_a_bare_string(self) -> None:
        # Otherwise a comparison against a literal would silently succeed and
        # invite treating the wrapper as interchangeable with the raw value.
        assert SecretValue("a") != "a"

    def test_is_hashable(self) -> None:
        assert len({SecretValue("a"), SecretValue("a"), SecretValue("b")}) == 2

    def test_length_is_available_without_revealing(self) -> None:
        assert len(SecretValue("hunter2")) == 7

    def test_empty_secret_is_falsy(self) -> None:
        assert not SecretValue("")
        assert SecretValue("x")

    def test_rejects_a_non_string(self) -> None:
        with pytest.raises(TypeError):
            SecretValue(None)

    def test_cannot_be_pickled(self) -> None:
        # Serialising a secret is nearly always an accident: a cached object, a
        # multiprocessing argument, a persisted session.
        with pytest.raises(TypeError, match="cannot be serialised"):
            pickle.dumps(SecretValue("hunter2"))

    def test_survives_the_logging_redaction_path_unrevealed(self) -> None:
        redacted = redact_entry("harmless_name", SecretValue("hunter2"), allow_content=True)

        assert "hunter2" not in str(redacted)


# ---------------------------------------------------------------------------
# SecretStore implementations
# ---------------------------------------------------------------------------


class TestEnvironmentSecretStore:
    async def test_empty_variable_is_treated_as_absent(self) -> None:
        os.environ["EMPTY_PROBE"] = ""
        try:
            assert await EnvironmentSecretStore().get("EMPTY_PROBE") is None
        finally:
            del os.environ["EMPTY_PROBE"]

    async def test_list_names_is_empty(self) -> None:
        # The environment offers no way to tell which variables are meant as
        # this application's secrets; guessing by prefix would be wrong twice.
        assert await EnvironmentSecretStore().list_names() == []

    async def test_is_always_available(self) -> None:
        assert await EnvironmentSecretStore().is_available() is True


class TestChainedSecretStore:
    async def test_requires_at_least_one_store(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            ChainedSecretStore()

    async def test_earlier_stores_win(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECEDENCE_PROBE", "from-environment")
        backing = InMemorySecretStore()
        await backing.set("PRECEDENCE_PROBE", SecretValue("from-store"))
        chain = ChainedSecretStore(EnvironmentSecretStore(), backing)

        found = await chain.get("PRECEDENCE_PROBE")

        assert found is not None
        assert found.reveal() == "from-environment"

    async def test_writes_skip_read_only_stores(self) -> None:
        backing = InMemorySecretStore()
        chain = ChainedSecretStore(EnvironmentSecretStore(), backing)

        await chain.set("WRITTEN", SecretValue("value"))

        assert await backing.get("WRITTEN") is not None

    async def test_write_fails_when_no_store_accepts_it(self) -> None:
        chain = ChainedSecretStore(EnvironmentSecretStore())

        with pytest.raises(ReadOnlySecretStoreError):
            await chain.set("ANY", SecretValue("value"))

    async def test_delete_clears_every_writable_store(self) -> None:
        # A stale value in a lower-priority store must not resurface after the
        # higher-priority one is cleared.
        first, second = InMemorySecretStore(), InMemorySecretStore()
        await first.set("DUPLICATED", SecretValue("one"))
        await second.set("DUPLICATED", SecretValue("two"))
        chain = ChainedSecretStore(first, second)

        await chain.delete("DUPLICATED")

        assert await chain.get("DUPLICATED") is None

    async def test_list_names_is_the_union(self) -> None:
        first, second = InMemorySecretStore(), InMemorySecretStore()
        await first.set("A", SecretValue("1"))
        await second.set("B", SecretValue("2"))

        assert sorted(await ChainedSecretStore(first, second).list_names()) == ["A", "B"]

    async def test_is_available_when_any_store_is(self) -> None:
        chain = ChainedSecretStore(UnavailableSecretStore(), InMemorySecretStore())

        assert await chain.is_available() is True

    async def test_is_unavailable_when_none_are(self) -> None:
        chain = ChainedSecretStore(UnavailableSecretStore(), UnavailableSecretStore())

        assert await chain.is_available() is False


class TestKeyringSecretStore:
    async def test_reports_a_backend_name(self) -> None:
        assert isinstance(KeyringSecretStore().backend_name(), str)

    async def test_is_available_returns_a_bool_without_raising(self) -> None:
        assert isinstance(await KeyringSecretStore().is_available(), bool)

    async def test_absent_name_returns_none(self) -> None:
        store = KeyringSecretStore(service_name="tgassist-test-absent")

        assert await store.get("DEFINITELY_NOT_STORED") is None

    async def test_delete_of_an_absent_name_is_silent(self) -> None:
        # Backends disagree on whether this raises; the adapter normalises it.
        store = KeyringSecretStore(service_name="tgassist-test-absent")

        await store.delete("DEFINITELY_NOT_STORED")


# ---------------------------------------------------------------------------
# Composition root wiring
# ---------------------------------------------------------------------------


class TestContainerWiring:
    def test_provides_every_core_port(self, container: Container) -> None:

        assert isinstance(container.clock, Clock)
        assert isinstance(container.ids, IdGenerator)
        assert isinstance(container.events, EventBus)
        assert isinstance(container.secrets, SecretStore)

    def test_ports_are_singletons_within_a_container(self, container: Container) -> None:
        # A new instance per access would mean two components subscribing to
        # "the" event bus and never hearing each other.
        assert container.clock is container.clock
        assert container.events is container.events

    def test_injected_doubles_replace_the_defaults(self) -> None:

        clock = FixedClock()
        container = Container(
            LoadedConfig(config=AppConfig()),
            clock=clock,
            ids=SequentialIdGenerator(),
            secrets=InMemorySecretStore(),
        )

        assert container.clock is clock
        assert container.ids.new_id() == 1

    def test_the_identifier_generator_uses_the_injected_clock(self) -> None:
        # Fixing the clock must fix the identifiers, or deterministic tests of
        # anything that generates identifiers become impossible.

        container = Container(LoadedConfig(config=AppConfig()), clock=FixedClock(EPOCH))

        assert container.ids.new_id() >> 12 == int(EPOCH.timestamp() * 1000)

    async def test_verify_secret_store_passes_when_available(self) -> None:

        container = Container(LoadedConfig(config=AppConfig()), secrets=InMemorySecretStore())

        assert await container.verify_secret_store() is True

    async def test_verify_secret_store_fails_closed_when_required(self) -> None:
        # SECURITY.md section 7: refuse to start rather than store a Telegram
        # session unencrypted.

        config = AppConfig.model_validate({"security": {"require_secret_store": True}})
        container = Container(LoadedConfig(config=config), secrets=UnavailableSecretStore())

        with pytest.raises(SecretStoreUnavailableError):
            await container.verify_secret_store()

    async def test_verify_secret_store_reports_without_raising_when_optional(self) -> None:

        config = AppConfig.model_validate({"security": {"require_secret_store": False}})
        container = Container(LoadedConfig(config=config), secrets=UnavailableSecretStore())

        assert await container.verify_secret_store() is False


def test_event_loop_is_not_required_to_build_a_container() -> None:
    """Construction must be synchronous.

    The composition root runs before any event loop exists, so a port whose
    construction required one could never be wired.
    """
    container = Container(LoadedConfig(config=AppConfig()))

    assert asyncio.run(container.secrets.is_available()) in {True, False}
