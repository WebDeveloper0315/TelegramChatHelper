"""In-process event bus with synchronous delivery and isolated handler failures.

Delivery is synchronous: ``publish`` returns only once every handler has run.
The alternative -- scheduling handlers and returning immediately -- makes tests
nondeterministic, loses events on shutdown, and hides handler latency from the
caller who could actually do something about it. See ADR-031.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Final

from tgassist.domain.errors import EventDispatchError
from tgassist.domain.events import DomainEvent
from tgassist.domain.ports.event_bus import EventBus, EventHandler, Subscription
from tgassist.infrastructure.logging import get_logger

DEFAULT_FAILURE_THRESHOLD: Final = 5
"""Consecutive failures after which a handler is unsubscribed."""

DEFAULT_MAX_DEPTH: Final = 10
"""Publish depth beyond which an event cycle is assumed."""


@dataclass(slots=True)
class _Registration:
    """One subscribed handler and its failure history."""

    subscription: Subscription
    handler: EventHandler[Any]
    consecutive_failures: int = field(default=0)


class InProcessEventBus(EventBus):
    """Delivers events to handlers in the publishing process.

    Handler failures are isolated: a raising handler is logged and counted, and
    neither the publisher nor any other handler observes the exception. This is
    the mechanism behind "a faulty plugin must not crash the application"
    (ADR-025), so it is a hard guarantee rather than a convenience.
    """

    __slots__ = ("_depth", "_failure_threshold", "_log", "_max_depth", "_next_id", "_registry")

    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        """Create a bus.

        Args:
            failure_threshold: Consecutive failures after which a handler is
                unsubscribed. A permanently broken subscriber should degrade to
                absent rather than log on every event forever.
            max_depth: Maximum nesting of publish calls. Handlers may publish,
                but an unbounded cycle would exhaust the stack.
        """
        self._registry: dict[type[DomainEvent], list[_Registration]] = {}
        self._next_id = 0
        self._failure_threshold = failure_threshold
        self._max_depth = max_depth
        self._depth = 0
        self._log = get_logger(__name__)

    def subscribe(
        self,
        event_type: type[Any],
        handler: EventHandler[Any],
        *,
        name: str,
    ) -> Subscription:
        """Register a handler for an event type and its subclasses."""
        if not name:
            msg = "A subscription name is required so a failing handler can be attributed"
            raise ValueError(msg)

        self._next_id += 1
        subscription = Subscription(id=self._next_id, event_type=event_type, name=name)
        self._registry.setdefault(event_type, []).append(
            _Registration(subscription=subscription, handler=handler)
        )
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a handler. Unsubscribing twice is not an error."""
        registrations = self._registry.get(subscription.event_type)
        if not registrations:
            return
        remaining = [r for r in registrations if r.subscription.id != subscription.id]
        if remaining:
            self._registry[subscription.event_type] = remaining
        else:
            del self._registry[subscription.event_type]

    async def publish(self, event: DomainEvent) -> None:
        """Deliver an event to every matching handler and wait for completion."""
        if self._depth >= self._max_depth:
            msg = (
                f"Publish depth {self._max_depth} exceeded while dispatching "
                f"{type(event).__name__}; handlers are likely publishing in a cycle"
            )
            raise EventDispatchError(
                msg,
                user_message="An internal event loop was detected and stopped.",
                context={"event": type(event).__name__, "depth": self._depth},
            )

        self._depth += 1
        try:
            for registration in self._matching(type(event)):
                await self._invoke(registration, event)
        finally:
            self._depth -= 1

    def _matching(self, event_type: type[DomainEvent]) -> list[_Registration]:
        """Return handlers for the exact type first, then base-class handlers.

        Walking the method resolution order lets a component observe every event
        by subscribing to ``DomainEvent``. Exact-type handlers run first so that
        a broad observer -- a logger, an audit trail, a plugin -- sees the event
        after the components that act on it specifically.
        """
        matched: list[_Registration] = []
        for candidate in event_type.__mro__:
            if not (isinstance(candidate, type) and issubclass(candidate, DomainEvent)):
                continue
            matched.extend(self._registry.get(candidate, ()))
        return matched

    async def _invoke(self, registration: _Registration, event: DomainEvent) -> None:
        """Run one handler, absorbing and recording any failure."""
        try:
            result = registration.handler(event)
            if isinstance(result, Awaitable):
                await result
        except EventDispatchError:
            # The bus refusing is not a handler failing. Isolating it would hide
            # an event cycle -- a serious defect -- behind a log line, so it
            # propagates to the publisher whose operation could not complete.
            raise
        except Exception:
            # Deliberate catch-all. This is one of the three designated
            # isolation boundaries (docs/ERROR_HANDLING.md section 15): a
            # subscriber's failure must not propagate to an unrelated publisher.
            registration.consecutive_failures += 1
            self._log.exception(
                "event_handler_failed",
                handler=registration.subscription.name,
                event_type=type(event).__name__,
                consecutive_failures=registration.consecutive_failures,
            )
            if registration.consecutive_failures >= self._failure_threshold:
                self.unsubscribe(registration.subscription)
                self._log.error(
                    "event_handler_disabled",
                    handler=registration.subscription.name,
                    event_type=type(event).__name__,
                    threshold=self._failure_threshold,
                    detail="Handler unsubscribed after repeated failures.",
                )
        else:
            registration.consecutive_failures = 0

    def subscription_count(self, event_type: type[DomainEvent] | None = None) -> int:
        """Return the number of active subscriptions, for diagnostics and tests."""
        if event_type is not None:
            return len(self._registry.get(event_type, ()))
        return sum(len(registrations) for registrations in self._registry.values())
