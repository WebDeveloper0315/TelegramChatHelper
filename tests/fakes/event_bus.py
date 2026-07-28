"""Recording event bus fake.

Written as an independent implementation rather than a subclass of the
production bus. A subclass would inherit the behaviour the contract suite is
supposed to be verifying, so the suite would prove nothing about the fake.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from tgassist.domain.errors import EventDispatchError
from tgassist.domain.events import DomainEvent
from tgassist.domain.ports.event_bus import EventBus, EventHandler, Subscription


class RecordingEventBus(EventBus):
    """Delivers events like the real bus and remembers what was published.

    The recording is what makes assertions direct: a test can check that
    ``MessageIngested`` was published without subscribing a handler purely to
    observe it.
    """

    __slots__ = ("_depth", "_failures", "_handlers", "_max_depth", "_next_id", "published")

    def __init__(self, *, failure_threshold: int = 5, max_depth: int = 10) -> None:
        self._handlers: list[tuple[Subscription, EventHandler[Any]]] = []
        self._failures: dict[int, int] = {}
        self._next_id = 0
        self._failure_threshold = failure_threshold
        self._max_depth = max_depth
        self._depth = 0
        self.published: list[DomainEvent] = []

    def subscribe(
        self,
        event_type: type[Any],
        handler: EventHandler[Any],
        *,
        name: str,
    ) -> Subscription:
        if not name:
            msg = "A subscription name is required so a failing handler can be attributed"
            raise ValueError(msg)
        self._next_id += 1
        subscription = Subscription(id=self._next_id, event_type=event_type, name=name)
        self._handlers.append((subscription, handler))
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._handlers = [(s, h) for s, h in self._handlers if s.id != subscription.id]

    async def publish(self, event: DomainEvent) -> None:
        if self._depth >= self._max_depth:
            msg = f"Publish depth {self._max_depth} exceeded for {type(event).__name__}"
            raise EventDispatchError(msg, context={"event": type(event).__name__})

        self.published.append(event)
        self._depth += 1
        try:
            for subscription, handler in self._exact_then_base(event):
                await self._invoke(subscription, handler, event)
        finally:
            self._depth -= 1

    def _exact_then_base(self, event: DomainEvent) -> list[tuple[Subscription, EventHandler[Any]]]:
        exact = [(s, h) for s, h in self._handlers if s.event_type is type(event)]
        base = [
            (s, h)
            for s, h in self._handlers
            if s.event_type is not type(event) and isinstance(event, s.event_type)
        ]
        return exact + base

    async def _invoke(
        self,
        subscription: Subscription,
        handler: EventHandler[Any],
        event: DomainEvent,
    ) -> None:
        try:
            result = handler(event)
            if isinstance(result, Awaitable):
                await result
        except EventDispatchError:
            # The bus refusing is not a handler failing; it must propagate.
            raise
        except Exception:
            count = self._failures.get(subscription.id, 0) + 1
            self._failures[subscription.id] = count
            if count >= self._failure_threshold:
                self.unsubscribe(subscription)
        else:
            self._failures[subscription.id] = 0

    def events_of(self, event_type: type[DomainEvent]) -> list[DomainEvent]:
        """Return published events of a given type."""
        return [e for e in self.published if isinstance(e, event_type)]

    def clear(self) -> None:
        """Forget everything recorded so far, keeping subscriptions."""
        self.published.clear()

    def subscription_count(self, event_type: type[DomainEvent] | None = None) -> int:
        """Return the number of active subscriptions."""
        if event_type is not None:
            return sum(1 for s, _ in self._handlers if s.event_type is event_type)
        return len(self._handlers)
