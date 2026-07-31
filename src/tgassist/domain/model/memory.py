"""The two halves of one lifecycle: a proposal, and what a person turns it into.

A candidate fact a model extracted from a conversation, waiting for a person to
decide about it. It is the mechanism that keeps hallucinated and injected
content out of permanent memory (ADR-019): the model proposes, and the user
decides.

Why a proposal is not a memory
------------------------------

The alternative -- writing what the model extracted straight into memory, and
letting the user correct it afterwards -- fails in a way that is hard to
recover from. A wrong memory is not merely wrong: it is *retrieved*, put into
later prompts, and used to justify later suggestions, so an error propagates
into work the user never connected to the extraction that caused it. By then the
question "why does it think that" has no visible answer.

Proposals invert the default. Nothing enters memory without a decision, so the
worst a bad extraction can do is waste a moment of review. That costs a review
queue, and the queue is the point rather than the price.

What the model may decide, and what it may not
----------------------------------------------

The model supplies exactly three things: a **category**, a **value**, and the
**evidence** it read them from, plus its own confidence. Everything else --
identifier, timestamps, which conversation, which AI call, which prompt version,
what status it starts in -- is decided by the application. A model that could
name its own identifier could overwrite a proposal; one that could set its own
status could approve itself. Neither is a hypothetical worth leaving open when
closing it costs nothing (ADR-058).

One decision, and it is final
-----------------------------

A proposal is created ``pending`` and is decided exactly once. :meth:`
MemoryProposal.decided` is the only transition either aggregate here has, and it
refuses any source state but ``pending`` -- so a proposal cannot be accepted
twice, rejected twice, or accepted after being rejected.

There is no undo and no reopen. That is a decision rather than an omission
(ADR-059). A reopened rejection would mean a memory could appear that a person
had already declined, and reversing an acceptance would have to decide what
becomes of a Memory that has since been read, quoted and acted upon. The
recoverable path is the ordinary one: reject, and the extractor will offer the
fact again only if you delete the proposal's record of it -- which nothing does.

What a Memory is, and is not
----------------------------

A :class:`Memory` is **user-approved knowledge**. A :class:`MemoryProposal` is
**model output**. Keeping them as different types, in different tables, is the
whole architecture of this feature: every query that asks "what do we know about
this person" reads a table nothing can write to without a person's decision.

A Memory is immutable and has no edit method. Correcting one means deleting it
and accepting a new proposal -- because a fact that could be edited in place
would lose the provenance that makes it checkable.

Deferred attributes
-------------------

``DOMAIN_MODEL.md`` sections 5.9 and 5.10 name several more, none implemented:

* ``importance``, ``is_pinned``, ``last_retrieved_at``, ``retrieval_count``,
  ``valid_from`` / ``valid_until`` -- every one of them exists to serve
  *retrieval*, which is Slice 9d. A column written by nobody is a column kept
  correct by nobody.
* ``conflicts_with_memory_id`` and ``MemoryRevision`` -- conflict detection and
  supersession. See :class:`MemoryKey` for why this slice cannot detect a
  contradiction, only an exact repeat.
* ``rejection_reason`` -- a rejection today is a decision, not an explanation.
* ``decided_by`` -- every decision in this milestone is a person's, because
  there is no other way to make one. The column would record a constant.
  Auto-approval is the feature that gives it a second value, and it can add it.
* The ``superseded``, ``expired`` and ``archived`` states -- each is a
  transition, and this milestone has exactly one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Final

from tgassist.domain.errors import DomainValidationError, InvalidStateTransitionError
from tgassist.domain.model.ai import PromptVersion
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ContactId,
    ConversationId,
    MemoryId,
    MemoryProposalId,
    require_positive_identifier,
)

#: The longest a proposed fact may be. A memory is a sentence, not a paragraph:
#: the value goes into later prompts, where length is a budget everything else
#: competes for.
MAX_VALUE_LENGTH: Final = 500

#: The longest supporting quotation. Longer than the value, because evidence is
#: quoted from a message and a message is not obliged to be brief.
MAX_EVIDENCE_LENGTH: Final = 2000

#: The longest comparison key. Shorter than the value it is derived from, so a
#: pair of very long facts differing only in their tail collapse to one key --
#: which is the conservative direction: they are reported as the same fact and a
#: person decides, rather than both being stored silently.
MAX_KEY_LENGTH: Final = 120

#: Everything that is not a letter, a digit or a space. Removed before a key is
#: formed, so that "Lives in Lisbon." and "lives in lisbon" are one fact.
_NOT_MEANINGFUL: Final = re.compile(r"[^\w\s]", re.UNICODE)

#: What a fact is worth when nobody has said. The middle of the range, so that
#: a later "this matters" and a later "this does not" have equal room.
NORMAL_IMPORTANCE: Final = 0.5

#: How close two importances must be to be the same one. Floats that made a
#: round trip through SQLite are not bit-identical to the constants they came
#: from, and a label that vanished after a round trip would be a puzzle.
_IMPORTANCE_TOLERANCE: Final = 1e-9

#: The names a person uses, and what each is worth. A command takes a name; the
#: column stores a number, because ranking compares numbers and a later
#: interface may well offer a slider.
IMPORTANCE_LEVELS: Final[dict[str, float]] = {
    "low": 0.25,
    "normal": NORMAL_IMPORTANCE,
    "high": 0.75,
    "critical": 1.0,
}


class MemoryCategory(StrEnum):
    """What kind of fact a proposal is about.

    A closed set, as ``DOMAIN_MODEL.md`` section 5.9 specifies. Closed because
    the categories are what a user filters, sorts and eventually auto-approves
    by, and a free-text category would make each of those a string comparison
    against whatever the model felt like writing that day. ``OTHER`` is the
    escape hatch, and a proposal that lands there is a signal the set needs
    revisiting rather than a failure.
    """

    IDENTITY = "identity"
    LOCATION = "location"
    OCCUPATION = "occupation"
    INTEREST = "interest"
    PREFERENCE = "preference"
    RELATIONSHIP = "relationship"
    IMPORTANT_DATE = "important_date"
    PLAN = "plan"
    SHARED_EXPERIENCE = "shared_experience"
    OPEN_QUESTION = "open_question"
    CONSTRAINT = "constraint"
    OTHER = "other"


class ProposalStatus(StrEnum):
    """Where a proposal stands.

    ``PENDING`` is the only value this milestone writes. The other two exist
    because the schema's check constraint has to name them and because Slice 9c
    writes them; until then, terminal means *unreachable*.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        """Whether a proposal in this state is finished with.

        A terminal proposal is never reconsidered: it is kept so that the same
        fact is not proposed again, which is why rejection is a decision worth
        storing rather than a row worth deleting.
        """
        return self is not ProposalStatus.PENDING


class MemorySource(StrEnum):
    """How this application came to believe a fact.

    Three ways are foreseen and **one exists**: today the only route into memory
    is a person accepting an AI proposal. The other two are named here and in
    the check constraint because they are a closed vocabulary that later
    retrieval ranking depends on -- ``DOMAIN_MODEL.md`` section 5.9 requires
    ``USER`` to outrank AI provenance -- and because a value added later would
    be a migration.
    """

    #: A person typed it. No route to this exists yet.
    USER = "user"
    #: A model proposed it and a person accepted it. The only one written today.
    AI_APPROVED = "ai_approved"
    #: A model proposed it and a rule accepted it. Auto-approval does not exist,
    #: and deliberately did not arrive with the slice that introduced review.
    AI_AUTO = "ai_auto"

    @property
    def needs_provenance(self) -> bool:
        """Whether a memory from this source must name what produced it.

        Anything a model had a hand in must be traceable back to the call that
        produced it. A fact a person typed needs no such trail: they are the
        provenance (``DOMAIN_MODEL.md`` section 5.9).
        """
        return self is not MemorySource.USER


@dataclass(frozen=True, slots=True)
class MemoryKey:
    """The form of a fact that decides whether two facts are the same one.

    Derived from the value by this application, and **never supplied by a
    model** (ADR-059). The rule is deliberately dull: fold the case, drop
    everything that is not a letter, a digit or a space, collapse the
    whitespace, truncate. "Lives in Lisbon." and "lives  in lisbon" produce one
    key; the value keeps its original form for a person to read.

    What that buys, and what it does not
    ------------------------------------

    It makes storing the same fact twice **structurally impossible** -- the key
    is part of a unique index -- which is what lets a user accept the same
    proposal from two overlapping conversations without acquiring two memories.

    It does **not** detect a contradiction. "Lives in Lisbon" and "Lives in
    Porto" have different keys, so both can be stored, and deciding which is
    true is conflict detection rather than deduplication (``DOMAIN_MODEL.md``
    section 6, ``MemoryConflictDetector``). That is a real limitation and it is
    the price of not letting a model name the subject of a fact; the reasoning
    is in ADR-059.

    Attributes:
        value: The normalised comparison form.
    """

    value: str

    def __post_init__(self) -> None:
        """Validate that the key is usable.

        Raises:
            DomainValidationError: If the key is empty or too long, which would
                mean it was constructed rather than derived.
        """
        if not self.value:
            msg = "A memory key cannot be empty"
            raise DomainValidationError(msg, user_message="That fact cannot be identified.")
        if len(self.value) > MAX_KEY_LENGTH:
            msg = f"A memory key may be at most {MAX_KEY_LENGTH} characters, got {len(self.value)}"
            raise DomainValidationError(msg, user_message="That fact cannot be identified.")

    @classmethod
    def of(cls, value: str) -> MemoryKey:
        """Derive the key for a fact.

        Deterministic and total: the same text always produces the same key, and
        every non-empty fact produces one.

        Raises:
            DomainValidationError: If nothing meaningful survives normalisation
                -- a "fact" made entirely of punctuation is not one.
        """
        folded = _NOT_MEANINGFUL.sub(" ", value).casefold()
        collapsed = " ".join(folded.split())
        if not collapsed:
            msg = f"Nothing identifiable in {value!r}"
            raise DomainValidationError(msg, user_message="That fact has no content to remember.")
        return cls(collapsed[:MAX_KEY_LENGTH].strip())

    def __str__(self) -> str:
        """Return the key."""
        return self.value


@dataclass(frozen=True, slots=True)
class Confidence:
    """How sure the model says it is, from zero to one.

    A value object rather than a bare float, for one reason worth the type: a
    number outside the range is not a low confidence, it is a model that did not
    answer the question asked, and catching that at the boundary keeps a
    nonsense value from being compared against a threshold as though it meant
    something.

    **Self-reported and poorly calibrated.** ``AI_MODELS.md`` section 15 is
    explicit that a model's own confidence is not a probability. It is used here
    as a coarse filter -- below a threshold, discard -- and nothing more is
    claimed of it.

    Attributes:
        value: The reported confidence, ``0.0`` to ``1.0`` inclusive.
    """

    value: float

    def __post_init__(self) -> None:
        """Validate the range.

        Raises:
            DomainValidationError: If the value is outside ``[0, 1]`` or is not
                a real number.
        """
        if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
            msg = f"A confidence must be a number, got {type(self.value).__name__}"
            raise DomainValidationError(msg, user_message="That is not a confidence.")
        if not 0.0 <= float(self.value) <= 1.0:
            msg = f"A confidence must be between 0 and 1, got {self.value}"
            raise DomainValidationError(msg, user_message="That is not a confidence.")

    def is_at_least(self, threshold: float) -> bool:
        """Whether this confidence meets a threshold."""
        return self.value >= threshold

    def __str__(self) -> str:
        """Render as two decimal places, the form reports use."""
        return f"{self.value:.2f}"


@dataclass(frozen=True, slots=True)
class Importance:
    """How much a fact matters, as a person judged it.

    Distinct from :class:`Confidence`, and the distinction is the point.
    Confidence is a *machine's* estimate of whether a fact is true, and is
    poorly calibrated (``AI_MODELS.md`` section 15). Importance is a *person's*
    statement of whether it is worth knowing. A model can be certain about
    something nobody needs, and unsure about the one thing that matters.

    Retrieval ranks by importance **before** confidence, for that reason
    (ADR-060).

    Set when a proposal is accepted -- the moment somebody is looking at the
    fact and can say -- and not changed afterwards. Whether it should stay
    immutable is an open question: a fact's importance genuinely changes with
    circumstances, and the answer needs a user interface to be worth having.

    Attributes:
        value: ``0.0`` to ``1.0`` inclusive.
    """

    value: float

    def __post_init__(self) -> None:
        """Validate the range.

        Raises:
            DomainValidationError: If the value is outside ``[0, 1]`` or is not
                a real number.
        """
        if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
            msg = f"An importance must be a number, got {type(self.value).__name__}"
            raise DomainValidationError(msg, user_message="That is not an importance.")
        if not 0.0 <= float(self.value) <= 1.0:
            msg = f"An importance must be between 0 and 1, got {self.value}"
            raise DomainValidationError(msg, user_message="That is not an importance.")

    @classmethod
    def normal(cls) -> Importance:
        """Return the importance a fact has when nobody said otherwise."""
        return cls(NORMAL_IMPORTANCE)

    @property
    def label(self) -> str:
        """Return the name a person would use, for listings and commands."""
        for name, weight in IMPORTANCE_LEVELS.items():
            if abs(self.value - weight) < _IMPORTANCE_TOLERANCE:
                return name
        return f"{self.value:.2f}"

    def __str__(self) -> str:
        """Render as two decimal places."""
        return f"{self.value:.2f}"


@dataclass(frozen=True, slots=True)
class Evidence:
    """The text a proposal was read from.

    Required, never optional. A proposal without evidence is a claim with no
    source, and the only way to check an extraction without re-running it is to
    read what it was based on. ``PROMPTS.md`` section 9.4 and ``AI_MODELS.md``
    section 13.7 both say the same thing from the other side: a proposal with no
    supporting quotation is discarded before the user sees it.

    Attributes:
        quote: A verbatim extract from the conversation.
    """

    quote: str

    def __post_init__(self) -> None:
        """Validate that there is a quotation and that it is not enormous.

        Raises:
            DomainValidationError: If the quote is blank or too long.
        """
        if not self.quote.strip():
            msg = "A proposal must quote what it was read from"
            raise DomainValidationError(
                msg, user_message="That proposal cites nothing and cannot be checked."
            )
        if len(self.quote) > MAX_EVIDENCE_LENGTH:
            msg = f"Evidence may be at most {MAX_EVIDENCE_LENGTH} characters, got {len(self.quote)}"
            raise DomainValidationError(msg, user_message="That quotation is too long.")

    def __str__(self) -> str:
        """Return the quotation."""
        return self.quote


@dataclass(frozen=True, slots=True)
class MemoryProposal:
    """One candidate fact, awaiting a decision.

    Immutable, like every entity here. Unlike most, it has no method that
    returns a changed one: there is no transition in this milestone, which is
    what makes *pending* the only state a stored proposal can be in.

    Attributes:
        id: Local identifier, assigned by the application.
        account_id: Whose proposal this is.
        conversation_id: The conversation it was extracted from. Present rather
            than a message identifier because extraction reads a whole episode:
            a fact is often assembled from several messages, and pointing at one
            of them would be a guess about which.
        ai_call_id: The recorded call that produced it. This is the whole of
            provenance -- through it, a proposal leads back to the model, the
            prompt version, the token cost and the moment it happened
            (ADR-057).
        category: What kind of fact it is.
        value: The fact itself, in the model's words.
        confidence: How sure the model said it was.
        evidence: What it was read from.
        prompt: Which prompt at which revision produced it. Duplicated from the
            AI call deliberately: the question "which proposals came from the
            prompt we changed last week" is asked of *this* table, and joining
            through an audit table to answer it would make the audit table load
            bearing for a routine query.
        status: Where it stands. ``PENDING`` until somebody decides, and then
            never anything else.
        created_at: When it was extracted, UTC.
        decided_at: When somebody decided, UTC, or ``None`` while pending. The
            two move together and cannot disagree: the invariant below refuses
            a decided proposal with no timestamp and a pending one with a
            timestamp, so "is it decided" has one answer however it is asked.
    """

    id: MemoryProposalId
    account_id: AccountId
    conversation_id: ConversationId
    ai_call_id: AiCallId
    category: MemoryCategory
    value: str
    confidence: Confidence
    evidence: Evidence
    prompt: PromptVersion
    status: ProposalStatus
    created_at: datetime
    decided_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate every invariant this entity is responsible for.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        require_positive_identifier(self.id, name="Memory proposal id")
        require_positive_identifier(self.account_id, name="Account id")
        require_positive_identifier(self.conversation_id, name="Conversation id")
        require_positive_identifier(self.ai_call_id, name="AI call id")

        if not self.value.strip():
            msg = "A proposal must propose something"
            raise DomainValidationError(msg, user_message="That proposal is empty.")
        if len(self.value) > MAX_VALUE_LENGTH:
            msg = f"A value may be at most {MAX_VALUE_LENGTH} characters, got {len(self.value)}"
            raise DomainValidationError(msg, user_message="That proposed fact is too long.")

        _require_utc(self.created_at, name="created_at")

        # One fact, one representation. A decided proposal with no timestamp
        # could not be audited, and a pending one with a timestamp would make
        # "has this been decided" depend on which field you asked.
        if self.status.is_terminal and self.decided_at is None:
            msg = f"A {self.status.value} proposal records when it was decided"
            raise DomainValidationError(msg, user_message="That proposal's decision is incomplete.")
        if not self.status.is_terminal and self.decided_at is not None:
            msg = "A pending proposal has not been decided"
            raise DomainValidationError(
                msg, user_message="That proposal's decision is inconsistent."
            )
        if self.decided_at is not None:
            _require_utc(self.decided_at, name="decided_at")

    # -- Construction -----------------------------------------------------

    @classmethod
    def propose(  # noqa: PLR0913 - an entity factory takes one argument per field
        cls,
        *,
        proposal_id: MemoryProposalId,
        account_id: AccountId,
        conversation_id: ConversationId,
        ai_call_id: AiCallId,
        category: MemoryCategory,
        value: str,
        confidence: Confidence,
        evidence: Evidence,
        prompt: PromptVersion,
        now: datetime,
    ) -> MemoryProposal:
        """Build a pending proposal.

        There is no ``status`` argument. A proposal that could be created in a
        decided state would be a decision nobody made, and the factory refusing
        to take one is how that is prevented rather than a rule to remember.

        Args:
            proposal_id: Local identifier, from the application's generator.
            account_id: Whose proposal this is.
            conversation_id: What it was extracted from.
            ai_call_id: The call that produced it.
            category: What kind of fact it is.
            value: The fact itself.
            confidence: What the model reported.
            evidence: The quotation supporting it.
            prompt: Which prompt at which revision.
            now: When it was extracted, from the injected clock.
        """
        return cls(
            id=proposal_id,
            account_id=account_id,
            conversation_id=conversation_id,
            ai_call_id=ai_call_id,
            category=category,
            value=value,
            confidence=confidence,
            evidence=evidence,
            prompt=prompt,
            status=ProposalStatus.PENDING,
            created_at=now,
        )

    def decided(self, status: ProposalStatus, now: datetime) -> MemoryProposal:
        """Return this proposal, decided.

        The only transition either aggregate in this module has, and the only
        one it will get. It refuses every source state but ``pending``, so a
        proposal cannot be accepted twice, rejected twice, or accepted after
        being rejected -- and there is no method that returns a *pending* one,
        so a decision cannot be undone (ADR-059).

        The entity refusing is one of two defences. The other is the repository,
        whose update names ``pending`` in its ``WHERE`` clause and raises when it
        matches nothing -- so two decisions racing cannot both win.

        Args:
            status: ``ACCEPTED`` or ``REJECTED``.
            now: When the decision was made, from the injected clock.

        Raises:
            InvalidStateTransitionError: If this proposal has already been
                decided, or if the target state is ``pending``.
        """
        if not self.is_pending:
            msg = (
                f"Memory proposal {int(self.id)} was already {self.status.value}; "
                f"a decision is made once"
            )
            raise InvalidStateTransitionError(
                msg,
                user_message="That proposal has already been decided.",
                context={"proposal_id": int(self.id), "status": self.status.value},
            )
        if not status.is_terminal:
            msg = f"{status.value} is not a decision"
            raise InvalidStateTransitionError(
                msg, user_message="That is not a decision a proposal can be given."
            )
        _require_utc(now, name="decided_at")
        if now < self.created_at:
            msg = f"A proposal cannot be decided before it was made: {now} < {self.created_at}"
            raise DomainValidationError(msg, user_message="That timestamp is inconsistent.")
        return replace(self, status=status, decided_at=now)

    # -- Derived state ----------------------------------------------------

    @property
    def is_pending(self) -> bool:
        """Whether this proposal still awaits a decision."""
        return self.status is ProposalStatus.PENDING

    def __str__(self) -> str:
        """Render as ``category: value``, the form a listing uses."""
        return f"{self.category.value}: {self.value}"


@dataclass(frozen=True, slots=True)
class Memory:
    """A fact a person has approved for long-term retention.

    The other half of the lifecycle, and a different kind of thing from the
    proposal it came from. A proposal is what a model said; a Memory is what a
    person decided to believe. Every later feature that asks "what do we know
    about this person" reads *this*, and nothing can write here without somebody
    having said yes.

    **Immutable, with no edit method.** Correcting a memory means deleting it
    and accepting a new proposal. An edit in place would keep the identifier and
    the provenance while changing the fact, so the AI call it cites would no
    longer be the call that produced what it now says -- and the provenance is
    the only thing that makes a stored claim checkable (ADR-059).

    **Deleted softly, and only softly.** ``deleted_at`` is a timestamp rather
    than a flag because retention has to ask "deleted before when", which a
    boolean cannot answer (the same reasoning ``Contact`` follows). A deleted
    memory stops being retrieved and stops occupying its key, so the fact can be
    accepted again.

    Attributes:
        id: Local identifier, assigned by the application.
        account_id: Whose memory this is.
        contact_id: Who the fact is about, or ``None`` when it came from a
            conversation with no single counterpart -- a group chat. Nullable
            for that reason alone; a fact about somebody in particular always
            names them.
        category: What kind of fact it is, from the proposal.
        key: The comparison form. What makes storing the same fact twice
            impossible, and what a person sees when told two facts collided.
        value: The fact, in the words the model used and the person approved.
        confidence: What the model reported when it proposed this. Kept as
            recorded rather than raised to certainty on acceptance: a person
            accepting a fact is saying "this is worth keeping", not "the model
            was certain", and the two are different claims.
        source: How this application came to believe it.
        proposal_id: The proposal a person accepted. Unique among memories, so
            "exactly one memory per accepted proposal" is a constraint rather
            than a rule somebody has to keep. ``None`` only when the chat it
            came from has since been deleted.
        conversation_id: The conversation the fact was read from, or ``None``
            for the same reason.
        ai_call_id: The model invocation that produced it. Through it a memory
            leads back to the model, the prompt version and the cost -- the
            whole of its provenance (ADR-057). The three move together: a
            deletion takes all of the trail or none of it.
        importance: How much the fact matters, as the person who accepted it
            judged. Ranked **above** confidence, because a person's judgement of
            what is worth knowing outranks a machine's estimate of what is true
            (ADR-060).
        created_at: When it was accepted, UTC. Not when it was proposed: this is
            the moment a person made it true for this application.
        deleted_at: When it was forgotten, UTC, or ``None``.
        retrieval_count: How many times this memory has been selected into a
            context. Bookkeeping *about* the fact, not part of it -- which is
            why it can change while the memory stays immutable (ADR-060).
        last_retrieved_at: When it was last selected, UTC, or ``None`` if never.
            Deliberately **not** a ranking input: ranking by it would make
            retrieved memories rank higher and so be retrieved more, which is a
            feedback loop rather than a relevance signal.
    """

    id: MemoryId
    account_id: AccountId
    contact_id: ContactId | None
    category: MemoryCategory
    key: MemoryKey
    value: str
    confidence: Confidence
    source: MemorySource
    proposal_id: MemoryProposalId | None
    conversation_id: ConversationId | None
    ai_call_id: AiCallId | None
    created_at: datetime
    importance: Importance = field(default_factory=Importance.normal)
    deleted_at: datetime | None = None
    retrieval_count: int = 0
    last_retrieved_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate every invariant this entity is responsible for.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        require_positive_identifier(self.id, name="Memory id")
        require_positive_identifier(self.account_id, name="Account id")
        if self.contact_id is not None:
            require_positive_identifier(self.contact_id, name="Contact id")
        if self.proposal_id is not None:
            require_positive_identifier(self.proposal_id, name="Memory proposal id")
        if self.conversation_id is not None:
            require_positive_identifier(self.conversation_id, name="Conversation id")
        if self.ai_call_id is not None:
            require_positive_identifier(self.ai_call_id, name="AI call id")

        if not self.value.strip():
            msg = "A memory must say something"
            raise DomainValidationError(msg, user_message="That memory is empty.")
        if len(self.value) > MAX_VALUE_LENGTH:
            msg = f"A value may be at most {MAX_VALUE_LENGTH} characters, got {len(self.value)}"
            raise DomainValidationError(msg, user_message="That fact is too long.")

        # Provenance is all or nothing. Anything a model had a hand in is
        # traceable when it is created -- a memory whose source cannot be shown
        # is a defect (AI_MODELS.md section 13.3) -- but a trail can be *lost*
        # afterwards, when the chat it came from is deleted and the proposal,
        # conversation and AI call go with it. That is deliberate: a memory is
        # user-approved knowledge and does not stop being known because the
        # exchange it came from was removed (ADR-059).
        #
        # What is refused is a *partial* trail, which would mean a deletion that
        # took some of the origin and left the rest -- a state nothing can
        # produce and nothing could interpret.
        trail = (self.proposal_id, self.conversation_id, self.ai_call_id)
        if any(part is not None for part in trail) and not all(part is not None for part in trail):
            msg = "A memory's provenance is complete or absent, never partial"
            raise DomainValidationError(msg, user_message="That memory's origin is inconsistent.")

        if self.retrieval_count < 0:
            msg = f"A memory cannot have been retrieved {self.retrieval_count} times"
            raise DomainValidationError(msg, user_message="That memory's history is invalid.")
        # The two move together. A count with no timestamp could not answer
        # "when was this last used", and a timestamp with no count would make
        # "has this ever been used" depend on which field was asked.
        if (self.retrieval_count > 0) != (self.last_retrieved_at is not None):
            msg = "A memory's retrieval count and last retrieval must agree"
            raise DomainValidationError(
                msg, user_message="That memory's retrieval history is inconsistent."
            )

        self._require_consistent_dates()

    def _require_consistent_dates(self) -> None:
        """Refuse a memory whose timestamps cannot all be true.

        Raises:
            DomainValidationError: If a timestamp is naive, or if something
                happened to this memory before it existed.
        """
        _require_utc(self.created_at, name="created_at")
        for value, what in (
            (self.last_retrieved_at, "retrieved"),
            (self.deleted_at, "deleted"),
        ):
            if value is None:
                continue
            _require_utc(value, name=f"{what}_at")
            if value < self.created_at:
                msg = f"A memory cannot be {what} before it existed: {value} < {self.created_at}"
                raise DomainValidationError(msg, user_message="That memory has inconsistent dates.")

    # -- Construction -----------------------------------------------------

    @classmethod
    def approved(
        cls,
        *,
        memory_id: MemoryId,
        proposal: MemoryProposal,
        contact_id: ContactId | None,
        now: datetime,
        importance: Importance | None = None,
    ) -> Memory:
        """Build the memory an accepted proposal becomes.

        Takes the **proposal**, not a set of fields. There is exactly one way a
        memory comes to exist in this milestone, and a factory that accepted
        loose values would be a second way -- one where the category, the value
        and the provenance could disagree with the thing a person actually
        approved.

        The key is derived here, from the value, by this application. A model
        never names it (ADR-059).

        Provenance is complete by construction: every field of it comes from the
        proposal, so a memory this factory builds can always be traced back to
        the call that produced it. It can *lose* that trail later, if the chat
        it came from is deleted -- see the invariant on this class.

        Args:
            memory_id: Local identifier, from the application's generator.
            proposal: The proposal a person accepted.
            contact_id: Who the fact is about, resolved from the conversation's
                chat. ``None`` for a chat with no single counterpart.
            now: When the decision was made, from the injected clock.
            importance: How much the person accepting says it matters. Defaults
                to normal, which is what accepting without saying means.

        Raises:
            DomainValidationError: If the proposal's value yields no key.
        """
        return cls(
            id=memory_id,
            account_id=proposal.account_id,
            contact_id=contact_id,
            category=proposal.category,
            key=MemoryKey.of(proposal.value),
            value=proposal.value,
            confidence=proposal.confidence,
            source=MemorySource.AI_APPROVED,
            proposal_id=proposal.id,
            conversation_id=proposal.conversation_id,
            ai_call_id=proposal.ai_call_id,
            created_at=now,
            importance=importance if importance is not None else Importance.normal(),
        )

    # -- Derived state ----------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Whether this memory is still remembered."""
        return self.deleted_at is None

    @property
    def was_retrieved(self) -> bool:
        """Whether this memory has ever been selected into a context."""
        return self.retrieval_count > 0

    def __str__(self) -> str:
        """Render as ``category: value``, the form a listing uses."""
        return f"{self.category.value}: {self.value}"


def _require_utc(value: datetime, *, name: str) -> None:
    """Refuse a timestamp with no defined instant.

    Raises:
        DomainValidationError: If the value is naive.
    """
    if value.tzinfo is None:
        msg = f"{name} must be timezone-aware; naive datetimes have no defined instant"
        raise DomainValidationError(msg, user_message="That timestamp is ambiguous.")


__all__ = [
    "IMPORTANCE_LEVELS",
    "MAX_EVIDENCE_LENGTH",
    "MAX_KEY_LENGTH",
    "MAX_VALUE_LENGTH",
    "NORMAL_IMPORTANCE",
    "Confidence",
    "Evidence",
    "Importance",
    "Memory",
    "MemoryCategory",
    "MemoryKey",
    "MemoryProposal",
    "MemorySource",
    "ProposalStatus",
]
