"""Domain events: immutable facts about something that has happened.

Events are named in the past tense and carry only the data a handler needs to
react. They are published *after* the originating transaction commits, so no
handler ever observes a fact that is later rolled back.

Only the base class is defined here. The concrete event catalogue in
``docs/DOMAIN_MODEL.md`` section 7 arrives with the milestones that raise those
events; declaring them before anything publishes them would be a placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base class for every domain event.

    Subclasses are frozen dataclasses carrying their own payload. Immutability
    matters because one event instance is delivered to many handlers: a mutable
    event would let the first handler change what the others observe.

    The base deliberately carries no fields. An ``occurred_at`` on the base
    would force every subclass to thread a timestamp through its constructor,
    and the components that need one already have an injected ``Clock``.
    """

    @classmethod
    def event_name(cls) -> str:
        """Return the event's name, used in logs and subscriptions."""
        return cls.__name__


@dataclass(frozen=True, slots=True)
class AccountCreated(DomainEvent):
    """An account was added to this installation.

    Carries identifiers rather than the entity: an event is a fact about what
    happened, and embedding a whole entity would let a handler act on a snapshot
    that may already be stale by the time it runs.
    """

    account_id: int
    is_active: bool


@dataclass(frozen=True, slots=True)
class AccountActivated(DomainEvent):
    """The application switched to operating a different account."""

    account_id: int
