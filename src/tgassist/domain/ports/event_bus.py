"""Event bus port: decoupled publish and subscribe between components.

The bus is what lets ingest announce ``MessageIngested`` without knowing that
anything recomputes relationship metrics, and what lets a plugin observe the
same fact without the core knowing the plugin exists.

Delivery is **synchronous**: :meth:`EventBus.publish` returns only after every
handler has run. The method is ``async`` because handlers perform I/O, not
because delivery is deferred. See ADR-031 for why this replaced the
fire-and-forget scheduling originally specified.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar, runtime_checkable

from tgassist.domain.events import DomainEvent

E_co = TypeVar("E_co", bound=DomainEvent, covariant=True)
E = TypeVar("E", bound=DomainEvent)

EventHandler = Callable[[E], Awaitable[None] | None]
"""A handler may be a plain function or a coroutine function.

Both are supported because most handlers are trivial and forcing every one to
be ``async`` would add ceremony without adding capability.
"""


@dataclass(frozen=True, slots=True)
class Subscription:
    """A handle returned by :meth:`EventBus.subscribe`.

    Attributes:
        id: Opaque identifier, unique within a bus.
        event_type: The event class the handler was registered for.
        name: Human-readable name used in logs, so a misbehaving handler can be
            attributed to the component or plugin that registered it.
    """

    id: int
    event_type: type[DomainEvent]
    name: str


@runtime_checkable
class EventBus(Protocol):
    """Publishes events to registered handlers.

    Contract, guaranteed by every implementation and verified by the shared
    contract test suite:

    1. **Synchronous delivery.** :meth:`publish` returns only once every
       matching handler has completed. A caller that awaits ``publish`` can rely
       on the handlers having run.
    2. **Registration order.** Handlers for one event type are invoked in the
       order they subscribed.
    3. **Subclass delivery.** A handler registered for a base class also
       receives subclass instances, so a component can observe every event by
       subscribing to :class:`DomainEvent`. Base-class handlers run after
       handlers registered for the exact type.
    4. **Failure isolation.** A handler that raises is logged and its failure
       recorded; the exception never reaches the publisher or the other
       handlers. One broken subscriber must not break an unrelated workflow, and
       this is what makes "a faulty plugin cannot crash the application" true.
    5. **Automatic disabling.** A handler that fails repeatedly is unsubscribed,
       so a permanently broken subscriber degrades to absent rather than
       generating unbounded noise.
    6. **At-most-once and non-durable.** Events are not persisted and do not
       survive a restart. Anything that must survive is a database write, not an
       event.
    7. **Handlers must be idempotent.** No ordering is guaranteed between
       different publishers.
    8. **Publishing from a handler is allowed** up to a bounded depth, after
       which the bus refuses rather than recursing without limit.
    9. Events are immutable, so a handler cannot alter what later handlers see.
    """

    async def publish(self, event: DomainEvent) -> None:
        """Deliver an event to every matching handler and wait for completion."""
        ...

    def subscribe(
        self,
        event_type: type[E],
        handler: EventHandler[E],
        *,
        name: str,
    ) -> Subscription:
        """Register a handler for an event type and its subclasses.

        Args:
            event_type: The event class to observe.
            handler: A function or coroutine function taking the event.
            name: Identifies the handler in logs. Required, because an anonymous
                failing handler cannot be attributed to anything.

        Returns:
            A handle for :meth:`unsubscribe`.
        """
        ...

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a handler. Unsubscribing twice is not an error."""
        ...
