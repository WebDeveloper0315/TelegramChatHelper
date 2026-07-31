"""The Suggestion aggregate: something a model proposed doing, awaiting a person.

The third thing in this project to follow the same shape, and the shape is the
point. A model extracts a fact and a person decides (ADR-058, ADR-059). A model
drafts a reply and a person decides. Nothing a model produces becomes an action,
a memory, or a message without somebody having said yes.

What a suggestion is
--------------------

**A draft, and a record that the draft was made.** Slice 9e produced suggestions
that existed for the length of one command and then vanished; there was nothing
to review because there was nothing to come back to. A stored suggestion is what
makes "observable before any action" true rather than aspirational: it can be
listed tomorrow, read alongside the AI call that produced it, and decided about
deliberately rather than in the moment it appeared.

**It is not an instruction.** Accepting one changes a status and nothing else.
There is no executor, no scheduler and no code path from this aggregate to
Telegram, and the fields are shaped so that adding one later is a decision
somebody has to make rather than a wire somebody can connect (ADR-062).

Three fields for three audiences
--------------------------------

* ``title`` -- one line, for a list. What is being suggested, at a glance.
* ``description`` -- what a **person** reads to decide. For a reply draft this
  is the draft itself, because that is the thing being judged. Deliberately
  plain text and deliberately type-agnostic: a reviewer must be able to decide
  about any kind of suggestion without any code understanding that kind.
* ``payload`` -- what a **machine** would need. JSON, type-specific, and read by
  nothing today. It is where a future executor finds the parts a person does not
  read: the model's confidence, which memories it cited, which prompt version
  asked.

That split is what keeps review possible as the kinds multiply. A new
``proposal_type`` needs new payload handling; it needs no new review.

One decision, and it is final
-----------------------------

Created ``pending``, decided exactly once. :meth:`Suggestion.decided` refuses
every source state but pending, and no method returns one to pending -- so a
suggestion cannot be accepted twice, dismissed twice, or accepted after being
dismissed.

There is no undo and no edit, for the reasons ADR-059 gave about memories and
one more that is specific here. An *editable* suggestion is a suggestion whose
recorded text is not what the model produced, which destroys the only thing the
record is for: knowing what the model actually suggested when somebody agreed
with it. A person who wants to send something different is writing their own
message, which is a different act and needs no aggregate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from tgassist.domain.errors import DomainValidationError, InvalidStateTransitionError
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ChatId,
    ConversationId,
    SuggestionId,
    require_positive_identifier,
)

#: The longest one-line summary. Long enough to be useful in a list, short
#: enough that a list of twenty is still readable.
MAX_TITLE_LENGTH: Final = 200

#: The longest body a person reads to decide. Bounded well below what a chat
#: platform accepts: a draft longer than this is one nobody will read before
#: agreeing to it, which defeats the review it exists for.
MAX_DESCRIPTION_LENGTH: Final = 4000

#: The longest machine payload. Generous, because it holds structure rather than
#: prose, and bounded because an unbounded column is one nothing can plan for.
MAX_PAYLOAD_LENGTH: Final = 16_000


class ProposalType(StrEnum):
    """What kind of thing is being suggested.

    One member today. It exists from the first row because the *review* is
    type-agnostic and the payload is not: a reviewer decides about a title and a
    description whatever this says, and only an executor needs to know which
    kind it is. Adding a kind is then a payload decision rather than a review
    decision (ADR-062).
    """

    #: A message the operator could send. Produced by Slice 9e.
    REPLY_DRAFT = "reply_draft"


class SuggestionStatus(StrEnum):
    """Where a suggestion stands.

    Three values and no others. ``superseded`` and ``expired`` are absent for
    the reason they are absent from ``ProposalStatus``: both are transitions,
    and this aggregate has exactly one.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"

    @property
    def is_terminal(self) -> bool:
        """Whether a suggestion in this state is finished with."""
        return self is not SuggestionStatus.PENDING


@dataclass(frozen=True, slots=True)
class Suggestion:
    """Something a model proposed, awaiting a person's decision.

    Immutable, like every entity here, with exactly one transition. Accepting
    one **does nothing but record the decision** -- there is no executor, and
    nothing in this module knows how to act on anything (ADR-062).

    Attributes:
        id: Local identifier, assigned by the application.
        account_id: Whose suggestion this is.
        chat_id: The conversation it is about. Never null: a suggestion with no
            chat could not be reviewed in context, and every kind of suggestion
            foreseen is about a conversation with somebody.
        conversation_id: The bounded episode it was drawn from, when one is
            known. ``None`` today: Slice 9e reads a chat's recent messages
            rather than a segmented conversation, so nothing yet supplies this.
        ai_call_id: The call that produced it, and the whole of its provenance.
            Through it a suggestion leads back to the model, the prompt version,
            the token cost and the moment (ADR-057).
        proposal_type: What kind of thing is being suggested.
        title: One line, for a listing.
        description: What a person reads to decide. For a reply draft, the draft.
        payload: The machine-readable specifics, as JSON text. Read by nothing
            today.
        status: Where it stands.
        created_at: When it was generated, UTC.
        decided_at: When somebody decided, UTC, or ``None`` while pending. The
            two move together and cannot disagree.
    """

    id: SuggestionId
    account_id: AccountId
    chat_id: ChatId
    ai_call_id: AiCallId
    proposal_type: ProposalType
    title: str
    description: str
    payload: str
    status: SuggestionStatus
    created_at: datetime
    conversation_id: ConversationId | None = None
    decided_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate every invariant this entity is responsible for.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        require_positive_identifier(self.id, name="Suggestion id")
        require_positive_identifier(self.account_id, name="Account id")
        require_positive_identifier(self.chat_id, name="Chat id")
        require_positive_identifier(self.ai_call_id, name="AI call id")
        if self.conversation_id is not None:
            require_positive_identifier(self.conversation_id, name="Conversation id")

        _require_text(self.title, name="title", limit=MAX_TITLE_LENGTH)
        _require_text(self.description, name="description", limit=MAX_DESCRIPTION_LENGTH)

        # The payload is opaque to this aggregate -- it is the *type's* business
        # -- but it must be a JSON object, so that whatever eventually reads it
        # can rely on that much without a schema of its own. A blob that is not
        # even parseable would be a defect nobody discovers until an executor
        # exists.
        if len(self.payload) > MAX_PAYLOAD_LENGTH:
            msg = (
                f"A payload may be at most {MAX_PAYLOAD_LENGTH} characters, got {len(self.payload)}"
            )
            raise DomainValidationError(msg, user_message="That suggestion is too large.")
        try:
            parsed = json.loads(self.payload)
        except (json.JSONDecodeError, ValueError) as exc:
            msg = "A suggestion payload must be JSON"
            raise DomainValidationError(
                msg, user_message="That suggestion's details are unreadable.", cause=exc
            ) from exc
        if not isinstance(parsed, dict):
            msg = f"A suggestion payload must be a JSON object, got {type(parsed).__name__}"
            raise DomainValidationError(
                msg, user_message="That suggestion's details are the wrong shape."
            )

        _require_utc(self.created_at, name="created_at")
        if self.status.is_terminal and self.decided_at is None:
            msg = f"A {self.status.value} suggestion records when it was decided"
            raise DomainValidationError(msg, user_message="That decision is incomplete.")
        if not self.status.is_terminal and self.decided_at is not None:
            msg = "A pending suggestion has not been decided"
            raise DomainValidationError(msg, user_message="That decision is inconsistent.")
        if self.decided_at is not None:
            _require_utc(self.decided_at, name="decided_at")

    # -- Construction -----------------------------------------------------

    @classmethod
    def draft(  # noqa: PLR0913 - an entity factory takes one argument per field
        cls,
        *,
        suggestion_id: SuggestionId,
        account_id: AccountId,
        chat_id: ChatId,
        ai_call_id: AiCallId,
        proposal_type: ProposalType,
        title: str,
        description: str,
        payload: Any,
        now: datetime,
        conversation_id: ConversationId | None = None,
    ) -> Suggestion:
        """Build a pending suggestion.

        There is no ``status`` argument. A suggestion that could be created
        already accepted would be a decision nobody made, and the factory
        refusing to take one is how that is prevented rather than a rule to
        remember (ADR-062).

        Args:
            suggestion_id: Local identifier, from the application's generator.
            account_id: Whose suggestion this is.
            chat_id: The conversation it is about.
            ai_call_id: The call that produced it.
            proposal_type: What kind of thing is being suggested.
            title: One line, for a listing.
            description: What a person reads to decide.
            payload: The machine-readable specifics. Serialised with sorted keys,
                so the same suggestion always stores the same bytes and two runs
                are comparable.
            now: When it was generated, from the injected clock.
            conversation_id: The episode it was drawn from, if one is known.
        """
        return cls(
            id=suggestion_id,
            account_id=account_id,
            chat_id=chat_id,
            conversation_id=conversation_id,
            ai_call_id=ai_call_id,
            proposal_type=proposal_type,
            title=title.strip(),
            description=description.strip(),
            payload=json.dumps(payload, sort_keys=True),
            status=SuggestionStatus.PENDING,
            created_at=now,
        )

    # -- The one transition ------------------------------------------------

    def decided(self, status: SuggestionStatus, now: datetime) -> Suggestion:
        """Return this suggestion, decided.

        The only transition this aggregate has. It refuses every source state
        but ``pending``, so a suggestion cannot be accepted twice, dismissed
        twice, or accepted after being dismissed -- and there is no method that
        returns a *pending* one, so a decision cannot be undone (ADR-062).

        The entity refusing is one of two defences. The other is the repository,
        whose update names ``pending`` in its ``WHERE`` clause and reports
        whether it changed anything -- so two decisions racing cannot both win.

        Args:
            status: ``ACCEPTED`` or ``DISMISSED``.
            now: When the decision was made, from the injected clock.

        Raises:
            InvalidStateTransitionError: If this suggestion has already been
                decided, or if the target state is ``pending``.
            DomainValidationError: If the decision precedes the suggestion.
        """
        if not self.is_pending:
            msg = (
                f"Suggestion {int(self.id)} was already {self.status.value}; "
                f"a decision is made once"
            )
            raise InvalidStateTransitionError(
                msg,
                user_message="That suggestion has already been decided.",
                context={"suggestion_id": int(self.id), "status": self.status.value},
            )
        if not status.is_terminal:
            msg = f"{status.value} is not a decision"
            raise InvalidStateTransitionError(
                msg, user_message="That is not a decision a suggestion can be given."
            )
        _require_utc(now, name="decided_at")
        if now < self.created_at:
            msg = f"A suggestion cannot be decided before it was made: {now} < {self.created_at}"
            raise DomainValidationError(msg, user_message="That timestamp is inconsistent.")
        return replace(self, status=status, decided_at=now)

    # -- Derived state ----------------------------------------------------

    @property
    def is_pending(self) -> bool:
        """Whether this suggestion still awaits a decision."""
        return self.status is SuggestionStatus.PENDING

    @property
    def was_accepted(self) -> bool:
        """Whether a person agreed with this suggestion.

        Agreement, and nothing more. Accepting executes nothing (ADR-062).
        """
        return self.status is SuggestionStatus.ACCEPTED

    def details(self) -> dict[str, Any]:
        """Return the payload, parsed.

        Safe: the constructor refused anything that is not a JSON object.
        """
        parsed: dict[str, Any] = json.loads(self.payload)
        return parsed

    def __str__(self) -> str:
        """Render as ``type: title``, the form a listing uses."""
        return f"{self.proposal_type.value}: {self.title}"


def _require_text(value: str, *, name: str, limit: int) -> None:
    """Refuse text that is missing or too long.

    Raises:
        DomainValidationError: If the value is blank or exceeds the limit.
    """
    if not value.strip():
        msg = f"A suggestion must have a {name}"
        raise DomainValidationError(msg, user_message=f"That suggestion has no {name}.")
    if len(value) > limit:
        msg = f"A {name} may be at most {limit} characters, got {len(value)}"
        raise DomainValidationError(msg, user_message=f"That {name} is too long.")


def _require_utc(value: datetime, *, name: str) -> None:
    """Refuse a timestamp with no defined instant.

    Raises:
        DomainValidationError: If the value is naive.
    """
    if value.tzinfo is None:
        msg = f"{name} must be timezone-aware; naive datetimes have no defined instant"
        raise DomainValidationError(msg, user_message="That timestamp is ambiguous.")


__all__ = [
    "MAX_DESCRIPTION_LENGTH",
    "MAX_PAYLOAD_LENGTH",
    "MAX_TITLE_LENGTH",
    "ProposalType",
    "Suggestion",
    "SuggestionStatus",
]
