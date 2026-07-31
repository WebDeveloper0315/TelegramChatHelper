"""Assembling everything a model is told, and deciding what goes when it will not fit.

The last deterministic step before a model is asked anything. It takes what
retrieval selected and what the conversation says, puts them in a fixed order,
spends a token budget, and hands back a structure the prompt template formats.

**It writes no prose.** Not one imperative sentence in this module reaches a
model. The assembler decides *what* is included and *in what order*; the prompt
file decides *what it means and what to do about it* (ADR-061). That split is
the whole point: a template cannot change what was retrieved, and this service
cannot change what the model is asked to do.

The order, and why it is this one
---------------------------------

Four parts, always in this sequence:

1. **The system prompt** -- the standing rules, including the one that says
   content inside the delimiters is data rather than instructions. First because
   it is what everything after it is read under, and stable because it contains
   no conversation data at all.

2. **Memories** -- what is known about the person, approved by them, in
   retrieval order. Before the conversation because they are the *frame*: a
   constraint like "do not mention the old job" changes how every message below
   should be read, and a model that met the messages first has already formed a
   reading by the time it learns the rule.

3. **The conversation** -- delimited, untrusted, oldest to newest. After the
   memories and before the task, so an injection attempt in a message is
   bracketed by trusted rules above and the actual instruction below.

4. **The task and the output format** -- last, and closest to the answer. A
   model's final instruction is the one it follows most reliably, and it is also
   the one that must survive an injection that got through everything above.

Rejected: letting the model decide what to read first, and any ordering chosen
for convenience. Both make the prompt unreproducible, which would make every
later comparison -- between prompt versions, between retrieval strategies --
meaningless.

What goes when it will not fit
------------------------------

Trimming is in one direction only, and two things are never removed.

**Never removed:** the system prompt, the task, the output format, and the
**most recent message**. Without that last one there is nothing to respond to,
so a "suggestion" built without it would be a guess about a conversation the
model cannot see.

**First to go: the oldest messages.** A conversation is chronological, and in a
chronological record recency *is* relevance -- the oldest turn is the one whose
absence changes the answer least.

**Then the lowest-ranked memories.** After the messages, deliberately, and the
justification matters because the obvious instinct is the opposite. Memories
have *already survived a budget*: retrieval ranked them and spent its own
allowance, so each one that arrives here was chosen. The message history is
bulk. Dropping a curated fact about somebody costs more than dropping a line of
small talk from an hour ago.

Rejected: dropping whichever thing is largest, and truncating individual
memories or messages. The first is unpredictable from the outside; the second
produces a fact or a sentence that was never said.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.memory import Memory
from tgassist.domain.model.message import Message, SenderKind
from tgassist.domain.model.prompt import neutralise
from tgassist.domain.services.memory_selection import estimate_tokens, memory_tokens

#: How one memory is presented. Neutral data, not prose: the template says what
#: to do with these lines, and this says only what they are.
MEMORY_LINE: Final = "- [{key}] {category}: {value}"

#: How one turn is presented. The label distinguishes who spoke, which the model
#: cannot infer from the text and must not guess.
TURN_LINE: Final = "{who}: {text}"

#: What the operator is called in a transcript. "you" would be ambiguous -- the
#: model is also being addressed as "you" -- and a name would leak into the
#: prompt a detail the assembler has no business choosing.
OPERATOR_LABEL: Final = "me"

#: What the other party is called.
CONTACT_LABEL: Final = "them"

#: The fewest messages a prompt may contain. One: the thing being responded to.
MINIMUM_MESSAGES: Final = 1


class TrimReason(StrEnum):
    """Why something was left out of an assembled prompt.

    Distinct from ``OmissionReason``, which is about *retrieval*. A memory can
    be omitted twice for two different reasons -- not selected by the retriever,
    or selected and then trimmed here -- and collapsing them would hide which
    budget was too small.
    """

    #: An older message, dropped so the prompt would fit.
    OLDER_MESSAGE = "older_message"
    #: A lower-ranked memory, dropped after the messages were exhausted.
    LOWER_RANKED_MEMORY = "lower_ranked_memory"


@dataclass(frozen=True, slots=True)
class Turn:
    """One line of conversation, as the model will see it.

    Attributes:
        who: ``me`` or ``them``.
        text: What was said, already truncated to the per-message limit.
        tokens: What including it costs.
    """

    who: str
    text: str
    tokens: int

    def render(self) -> str:
        """Return the line for the transcript."""
        return TURN_LINE.format(who=self.who, text=self.text)


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """The recent conversation, prepared for a prompt.

    Attributes:
        turns: What the model will see, oldest first -- the order it happened
            in, which is the only order a conversation can be read in.
        available: How many messages there were before any trimming, so a short
            transcript and a trimmed one are distinguishable.
        truncated: How many individual messages were shortened to the
            per-message limit. Reported because a truncated message is a message
            the model saw only part of, and a suggestion that missed the point
            of a long one has an explanation.
    """

    turns: tuple[Turn, ...]
    available: int
    truncated: int

    @property
    def tokens(self) -> int:
        """Return what the transcript costs."""
        return sum(turn.tokens for turn in self.turns)

    @property
    def is_empty(self) -> bool:
        """Whether there is no conversation to respond to."""
        return not self.turns

    @property
    def dropped(self) -> int:
        """How many messages were left out entirely."""
        return self.available - len(self.turns)

    def render(self) -> str:
        """Return the transcript, oldest first."""
        return "\n".join(turn.render() for turn in self.turns)


@dataclass(frozen=True, slots=True)
class Trimmed:
    """One thing the budget removed.

    Attributes:
        what: A short description, for a report a person reads.
        reason: Which rule removed it.
        tokens: What keeping it would have cost.
    """

    what: str
    reason: TrimReason
    tokens: int


@dataclass(frozen=True, slots=True)
class PromptContext:
    """Everything a model will be told, and the account of how it was chosen.

    Attributes:
        memories: What was included, in retrieval order.
        conversation: The transcript that survived trimming.
        trimmed: What the budget removed, in the order it was removed.
        budget: The budget this was assembled within.
        tokens: What the assembled parts cost, estimated.
    """

    memories: tuple[Memory, ...]
    conversation: ConversationContext
    trimmed: tuple[Trimmed, ...]
    budget: int
    tokens: int

    @property
    def memory_keys(self) -> tuple[str, ...]:
        """Return the keys supplied to the model.

        What attribution is checked against: a key the model reports using and
        that does not appear here was not supplied, so the model invented it
        (ADR-061).
        """
        return tuple(memory.key.value for memory in self.memories)

    @property
    def remaining(self) -> int:
        """How much of the budget was left unused."""
        return self.budget - self.tokens

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing to send."""
        return not self.memories and self.conversation.is_empty

    def render_memories(self) -> str:
        """Return the memory block, one neutral line each.

        Each line carries the key, because the model is asked to say which
        memories it used and a key is the only stable name a memory has.

        **Neutralised but not delimited.** A memory is trusted content -- a
        person approved it -- so it is not wrapped in the "this is data, not
        instructions" markers, which would say something untrue about it. But
        its *text* came from a model reading a conversation, so it can contain
        anything that conversation did: a value carrying
        ``<<<END_CONVERSATION_CONTENT>>>`` would sit outside the delimited block
        below and could forge a boundary for it. Trusting a fact is not the same
        as trusting its punctuation (ADR-061).
        """
        if not self.memories:
            return "(nothing is known about this person yet)"
        return "\n".join(
            MEMORY_LINE.format(
                key=neutralise(memory.key.value),
                category=memory.category.value,
                value=neutralise(memory.value),
            )
            for memory in self.memories
        )

    def render_conversation(self) -> str:
        """Return the transcript, plain.

        **Not wrapped and not neutralised here.** The prompt declares
        ``conversation`` as an untrusted input, and ``Prompt.render`` delimits
        and neutralises whatever it is given (ADR-058 section 4). Doing it in
        both places would double the markers and leave a reader two mechanisms
        to check instead of one.
        """
        if self.conversation.is_empty:
            return "(no messages)"
        return self.conversation.render()


@dataclass(frozen=True, slots=True)
class AssemblyRules:
    """What bounds an assembled prompt.

    Attributes:
        token_budget: The most the assembled parts may cost. Does not include
            the system prompt or the task, which are never trimmed and whose
            size is a property of the shipped files rather than of a request.
        message_limit: How many recent messages to consider before trimming.
        max_message_chars: How much of any one message to show. Bounds the
            payload space available to an injection attempt (``SECURITY.md``
            section 12).
        minimum_messages: How many messages survive whatever the budget says.
            One by default: the thing being responded to.
    """

    token_budget: int = 2000
    message_limit: int = 20
    max_message_chars: int = 2000
    minimum_messages: int = MINIMUM_MESSAGES

    def __post_init__(self) -> None:
        """Validate the bounds.

        Raises:
            DomainValidationError: If any bound is not positive, or if the floor
                is higher than the limit -- which would ask for more messages to
                be kept than were ever considered.
        """
        for value, name in (
            (self.token_budget, "token budget"),
            (self.message_limit, "message limit"),
            (self.max_message_chars, "message length limit"),
            (self.minimum_messages, "minimum message count"),
        ):
            if value < 1:
                msg = f"A {name} must be positive, got {value}"
                raise DomainValidationError(msg, user_message="That prompt budget is not usable.")
        if self.minimum_messages > self.message_limit:
            msg = (
                f"The minimum message count ({self.minimum_messages}) cannot exceed "
                f"the message limit ({self.message_limit})"
            )
            raise DomainValidationError(msg, user_message="That prompt budget is inconsistent.")


class ContextAssembler:
    """Builds the prompt payload, deterministically.

    Pure: no repository, no clock, no model. Given the same memories, the same
    messages and the same rules, it produces the same context -- which is what
    lets a prompt version be compared against another, and what lets a
    suggestion be explained by re-reading what was sent (ADR-061).
    """

    __slots__ = ("_rules",)

    def __init__(self, rules: AssemblyRules | None = None) -> None:
        """Take the bounds this assembler enforces."""
        self._rules = rules if rules is not None else AssemblyRules()

    @property
    def rules(self) -> AssemblyRules:
        """Return the bounds in force."""
        return self._rules

    def assemble(self, memories: Sequence[Memory], messages: Sequence[Message]) -> PromptContext:
        """Put everything together, and trim until it fits.

        Args:
            memories: What retrieval selected, already in retrieval order. Not
                re-ranked here: ranking is retrieval's decision and doing it
                twice would be two places to disagree.
            messages: The conversation, oldest first. The most recent
                ``message_limit`` are considered.

        Returns:
            What will be sent, and an account of what was removed.
        """
        turns = self._turns(messages)
        kept_memories = list(memories)
        trimmed: list[Trimmed] = []

        # Messages first, oldest first, down to the floor.
        while self._cost(kept_memories, turns) > self._rules.token_budget and len(turns) > (
            self._rules.minimum_messages
        ):
            dropped = turns.pop(0)
            trimmed.append(
                Trimmed(
                    what=f"message: {_shorten(dropped.text)}",
                    reason=TrimReason.OLDER_MESSAGE,
                    tokens=dropped.tokens,
                )
            )

        # Then memories, lowest-ranked first -- which is the end of the list,
        # because retrieval hands them over best first.
        while self._cost(kept_memories, turns) > self._rules.token_budget and kept_memories:
            dropped_memory = kept_memories.pop()
            trimmed.append(
                Trimmed(
                    what=f"memory: {_shorten(dropped_memory.value)}",
                    reason=TrimReason.LOWER_RANKED_MEMORY,
                    tokens=memory_tokens(dropped_memory),
                )
            )

        conversation = ConversationContext(
            turns=tuple(turns),
            available=len(messages[-self._rules.message_limit :]),
            truncated=sum(1 for turn in turns if turn.text.endswith(_TRUNCATION_MARK)),
        )
        return PromptContext(
            memories=tuple(kept_memories),
            conversation=conversation,
            trimmed=tuple(trimmed),
            budget=self._rules.token_budget,
            tokens=self._cost(kept_memories, turns),
        )

    # -- Preparation -------------------------------------------------------

    def _turns(self, messages: Sequence[Message]) -> list[Turn]:
        """Render the recent messages as turns, oldest first."""
        recent = list(messages[-self._rules.message_limit :])
        turns: list[Turn] = []
        for message in recent:
            text = self._body(message)
            turns.append(
                Turn(
                    who=(
                        OPERATOR_LABEL
                        if message.sender_kind is SenderKind.OPERATOR
                        else CONTACT_LABEL
                    ),
                    text=text,
                    tokens=estimate_tokens(f"{OPERATOR_LABEL}: {text}"),
                )
            )
        return turns

    def _body(self, message: Message) -> str:
        """Return what one message says, bounded.

        A message with no text appears as its kind. A conversation of six photos
        is one about which there is nothing to say, and the model should see
        that rather than see nothing.
        """
        text = message.text if message.text is not None else f"({message.message_type.value})"
        if len(text) > self._rules.max_message_chars:
            return text[: self._rules.max_message_chars] + _TRUNCATION_MARK
        return text

    def _cost(self, memories: Sequence[Memory], turns: Sequence[Turn]) -> int:
        """Return what a candidate assembly would cost."""
        return sum(memory_tokens(memory) for memory in memories) + sum(
            turn.tokens for turn in turns
        )


#: Appended to a message the per-message limit cut. Visible, so a model shown a
#: sentence that stops mid-word can tell that it was cut rather than that
#: somebody trailed off.
_TRUNCATION_MARK: Final = " [truncated]"

#: How much of a trimmed item to name in a report. Enough to recognise it,
#: short enough that a report of forty trims is still readable.
_SUMMARY_LENGTH: Final = 60


def _shorten(text: str) -> str:
    """Return a short description of something that was trimmed."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= _SUMMARY_LENGTH:
        return collapsed
    return collapsed[:_SUMMARY_LENGTH] + "..."


__all__ = [
    "CONTACT_LABEL",
    "MEMORY_LINE",
    "MINIMUM_MESSAGES",
    "OPERATOR_LABEL",
    "TURN_LINE",
    "AssemblyRules",
    "ContextAssembler",
    "ConversationContext",
    "PromptContext",
    "TrimReason",
    "Trimmed",
    "Turn",
]
