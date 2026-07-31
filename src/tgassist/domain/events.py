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
from datetime import datetime


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


@dataclass(frozen=True, slots=True)
class MessagesIngested(DomainEvent):
    """Messages were stored for a chat, and the storing transaction committed.

    **One event per committed batch, not per message** (ADR-050). Delivery is
    synchronous (ADR-031), so a per-message event during a fifty-thousand-message
    backfill would run every handler fifty thousand times inside the sync loop
    and the backfill would proceed at the speed of the slowest subscriber. A live
    update is the degenerate case with ``count=1``, so a handler has one shape to
    deal with rather than two.

    Published **after** the commit, never inside it. A handler observing a fact
    that then rolled back would be acting on something that never happened; and
    because the bus is neither durable nor transactional (``EventBus`` contract
    points 4 and 6), a process that dies between the commit and the publication
    loses the event and keeps the messages. That is the right way round: anything
    that must survive is a database write.

    Attributes:
        account_id: Whose messages these are.
        chat_id: The local chat they were stored in.
        count: How many were written. Never zero -- a batch that stored nothing
            publishes nothing, because "nothing happened" is not a fact worth
            waking every subscriber for.
        oldest_sent_at: When the oldest message in the batch was sent, UTC. The
            field conversation segmentation reads: it says how far back the
            batch reached, and therefore which conversations a re-segmentation
            has to revisit. ``newest_sent_at`` cannot answer that -- a backfill
            page of a hundred messages from last year has a newest that is also
            last year.
        newest_sent_at: When the newest message in the batch was sent, UTC. What
            a handler recomputing recency needs, without reading the messages.
        source: Which producer wrote them, as stable text: ``backfill``,
            ``catch_up`` or ``live``. A handler that treats a fifty-thousand-row
            backfill like a single arriving message would do fifty thousand
            times the work it meant to.
    """

    account_id: int
    chat_id: int
    count: int
    oldest_sent_at: datetime
    newest_sent_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class MemoryProposalsCreated(DomainEvent):
    """Candidate facts were extracted from a conversation and stored.

    Published after the storing transaction commits, so nothing observes
    proposals that were then rolled back.

    **Nothing subscribes to it yet**, which is a departure from the rule the
    earlier slices followed -- do not shape an event before its first consumer.
    It is published because the consumer is known rather than guessed: the
    ``memory_proposals_pending`` notification (``DOMAIN_MODEL.md`` section 5.23)
    exists to tell a user their review queue has grown, and the alternative to
    an event is a component that polls the table.

    Attributes:
        account_id: Whose proposals these are.
        conversation_id: What they were extracted from.
        chat_id: The chat that conversation belongs to. Carried so a subscriber
            can name the person involved without a second query.
        count: How many were stored. Never zero -- a run that stored nothing
            publishes nothing.
        ai_call_id: The call that produced them. Provenance travels with the
            fact, so a handler never has to trust the fact alone.
    """

    account_id: int
    conversation_id: int
    chat_id: int
    count: int
    ai_call_id: int


@dataclass(frozen=True, slots=True)
class MemoryProposalAccepted(DomainEvent):
    """A person accepted a candidate fact, and a Memory now exists.

    Published after the deciding transaction commits. Carries the memory it
    produced, because the decision and its consequence happened together and a
    subscriber that had to go and look would be asking about state that is
    already true.

    Attributes:
        account_id: Whose decision this was.
        proposal_id: What was accepted.
        memory_id: The memory it became. Exactly one, always.
        contact_id: Who the fact is about, or ``None`` for a conversation with
            no single counterpart.
        category: What kind of fact it is.
    """

    account_id: int
    proposal_id: int
    memory_id: int
    contact_id: int | None
    category: str


@dataclass(frozen=True, slots=True)
class MemoryProposalRejected(DomainEvent):
    """A person declined a candidate fact, and nothing was remembered.

    Published for the same reason failures are recorded rather than only
    successes: an audit that contained only the facts a user kept could not show
    what the extractor is getting wrong, which is the measurement that decides
    whether a prompt needs rewriting.

    Attributes:
        account_id: Whose decision this was.
        proposal_id: What was rejected.
        category: What kind of fact it was.
    """

    account_id: int
    proposal_id: int
    category: str


@dataclass(frozen=True, slots=True)
class MemoryCreated(DomainEvent):
    """A fact entered long-term memory.

    Distinct from :class:`MemoryProposalAccepted`, which is about a *decision*.
    This is about the memory itself, and it is the event a component that cares
    only about what is known -- a future index, a future export -- subscribes to
    without having to know that proposals exist.

    Both are published by the same transaction. Nothing subscribes to either
    yet, and neither is shaped for a guessed consumer: the shape is what the
    decision itself contains.

    Attributes:
        account_id: Whose memory this is.
        memory_id: The memory.
        contact_id: Who it is about, or ``None``.
        category: What kind of fact it is.
        source: How this application came to believe it.
    """

    account_id: int
    memory_id: int
    contact_id: int | None
    category: str
    source: str


@dataclass(frozen=True, slots=True)
class MemoriesRetrieved(DomainEvent):
    """Memories were selected into a context, and the retrieval was recorded.

    Published by ``BuildMemoryContext`` only -- the path that counts what it
    selects. ``GetMemoryContext`` publishes nothing, for the same reason it
    writes nothing: looking at what *would* be sent is not a retrieval, and an
    inspection that announced one would make the record of use disagree with
    the events describing it (ADR-060).

    **Nothing subscribes to it yet**, which is a departure from the rule the
    earlier slices followed. It is published because the consumer is known
    rather than guessed: whatever eventually reports on retrieval quality --
    which memories are used, which are never used, whether the ranking earns
    its place -- reads this, and the alternative is a component that polls the
    counters.

    Attributes:
        account_id: Whose memories these are.
        chat_id: The conversation the context was built for.
        contact_id: Who it was about, or ``None`` for a chat with no single
            counterpart.
        count: How many memories were selected. Never zero -- an empty context
            records nothing and announces nothing.
        candidates: How many were considered, so "one of two" and "one of
            ninety" are distinguishable without a second query.
        tokens: What the selected memories cost, estimated.
    """

    account_id: int
    chat_id: int
    contact_id: int | None
    count: int
    candidates: int
    tokens: int


@dataclass(frozen=True, slots=True)
class SuggestionsCreated(DomainEvent):
    """A model produced suggestions, and they were stored for review.

    Plural because a generator may one day produce several at once; today it
    produces one, and a batch of one needs no second event when that changes.

    Published after the storing transaction commits, so nothing observes
    suggestions that were then rolled back.

    Attributes:
        account_id: Whose suggestions these are.
        chat_id: The conversation they are about.
        suggestion_ids: What was stored. The identifiers rather than the
            entities, so a handler acts on what is current rather than on a
            snapshot that may already be stale.
        ai_call_id: The call that produced them.
        proposal_type: What kind of thing was suggested.
    """

    account_id: int
    chat_id: int
    suggestion_ids: tuple[int, ...]
    ai_call_id: int
    proposal_type: str


@dataclass(frozen=True, slots=True)
class SuggestionAccepted(DomainEvent):
    """A person agreed with a suggestion.

    **Agreement, not execution.** Nothing subscribes to this and nothing acts on
    it; a subscriber that sent a message on receiving one would be exactly the
    autonomous behaviour ADR-062 exists to prevent. The event says a person made
    a decision, and any component that eventually acts on that decision will be
    something a person switched on deliberately.

    Attributes:
        account_id: Whose decision this was.
        suggestion_id: What was accepted.
        chat_id: The conversation it is about.
        proposal_type: What kind of thing it was.
    """

    account_id: int
    suggestion_id: int
    chat_id: int
    proposal_type: str


@dataclass(frozen=True, slots=True)
class SuggestionDismissed(DomainEvent):
    """A person declined a suggestion, and nothing happened.

    Published for the same reason failures are recorded rather than only
    successes: a record containing only what was agreed with cannot show what
    the generator is getting wrong, which is the measurement that decides
    whether a prompt needs rewriting.

    Attributes:
        account_id: Whose decision this was.
        suggestion_id: What was dismissed.
        chat_id: The conversation it was about.
        proposal_type: What kind of thing it was.
    """

    account_id: int
    suggestion_id: int
    chat_id: int
    proposal_type: str
