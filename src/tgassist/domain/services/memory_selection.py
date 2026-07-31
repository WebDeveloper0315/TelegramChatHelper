"""Choosing which memories go into a prompt, deterministically.

The whole of retrieval, as a pure function. No embeddings, no similarity, no
model: given the same memories and the same budget, this returns the same
selection in the same order, every time.

Why deterministic first
-----------------------

Not because it is better than semantic retrieval. Because it is a **baseline**
(ADR-060). A vector search that beats this can be shown to beat it; one built
first would be compared against nothing, and "the embeddings improved retrieval"
would be a claim with no denominator.

It also establishes the parts a semantic version still needs and cannot borrow
from a model: the token budget, the order in which a context degrades when it
does not fit, and the record of what was left out.

The ranking, and why each key is where it is
--------------------------------------------

Five keys, applied in order. Every one of them is a fact already stored; none is
a weight somebody tuned.

1. **Category priority.** What kind of fact it is, ordered by what it costs to
   get wrong. A constraint the person asked for -- do not mention X -- is the
   most expensive thing to omit, because ignoring it is worse than saying
   nothing. An unanswered question is the most likely thing to need saying next.
   Time-sensitive facts come before durable ones, because a durable fact is
   equally true next week and a plan is not.

2. **Importance**, as the person who accepted the fact judged it. A human
   statement of what is worth knowing.

3. **Confidence**, as the model reported it. Below importance deliberately: a
   machine's estimate of whether something is *true* does not outrank a
   person's judgement of whether it *matters*, and self-reported confidence is
   poorly calibrated (``AI_MODELS.md`` section 15).

4. **Recency**, by when the fact was accepted -- newest first. Not by when it
   was last *retrieved*: ranking by that would make a retrieved memory rank
   higher and so be retrieved again, which is a feedback loop rather than a
   relevance signal.

5. **Identifier**, descending, as the tie-break. Arbitrary but total, so the
   order is never left to whatever the database returned.

Rejected: a weighted score summing normalised keys. It reads as principled and
is not -- the weights would be invented, no test could show one set was better
than another, and a change to one weight would silently reorder everything. A
lexicographic order is explainable one comparison at a time: *this came first
because it is a constraint and that is a preference*.

Budgeting comes after ranking, and never changes it
---------------------------------------------------

Ranking decides priority; the budget decides fit. A memory too long for what is
left is **skipped**, and the walk continues -- so one 400-token fact near the
top does not empty the context beneath it. What that cannot do is reorder
anything: the selected memories appear in rank order, and every omission is
reported with its reason.

The alternative, stopping at the first thing that does not fit, is simpler and
wastes most of the budget for one long memory. The other alternative --
shortening a memory to make it fit -- is refused outright: a truncated fact is a
different fact, and this module has no business inventing one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.memory import Memory, MemoryCategory

#: Characters per token, for estimating a context's size. The rule of thumb
#: every provider's documentation gives. Deliberately an estimate: the exact
#: count depends on the model's tokeniser, which is the provider's business and
#: cannot be consulted before the call -- a budget that needed it could not be
#: enforced.
#:
#: **The error, stated.** Against a byte-pair tokeniser this *over*-estimates
#: ordinary English prose by roughly 5-15% (English averages nearer 4.5
#: characters per token), and *under*-estimates text that tokenises badly:
#: non-Latin scripts, long unbroken identifiers and heavy punctuation can reach
#: two or three times the estimate in the worst cases.
#:
#: Acceptable for two reasons. The error is **systematic rather than random**,
#: so the same text always costs the same and a budget decision stays
#: reproducible. And the budget is small relative to any context window -- a few
#: hundred tokens against tens of thousands -- so even a threefold
#: under-estimate does not approach a limit that would truncate a request. What
#: it can do is make a context slightly larger or smaller than intended, which
#: is a cost question rather than a correctness one (ADR-060).
CHARS_PER_TOKEN: Final = 4

#: What one memory costs beyond its text: the line that introduces it, the
#: category label, the separators. Counted so a context of many short facts is
#: not systematically under-estimated.
TOKENS_PER_MEMORY: Final = 4

#: How memories are ordered by kind, lowest first. Ordered by what it costs to
#: get each wrong rather than by how interesting it is -- see the module
#: docstring. Every member of ``MemoryCategory`` appears exactly once, which the
#: tests assert: a category missing from here would sort as though it were the
#: least important thing known about somebody.
CATEGORY_PRIORITY: Final[dict[MemoryCategory, int]] = {
    # What the person asked for. Ignoring one is worse than saying nothing.
    MemoryCategory.CONSTRAINT: 0,
    # Something they asked that is still unanswered -- the likeliest thing to
    # need saying next.
    MemoryCategory.OPEN_QUESTION: 1,
    # Time-sensitive: useful now and useless later.
    MemoryCategory.PLAN: 2,
    MemoryCategory.IMPORTANT_DATE: 3,
    # Who they are.
    MemoryCategory.IDENTITY: 4,
    MemoryCategory.RELATIONSHIP: 5,
    # Durable context.
    MemoryCategory.OCCUPATION: 6,
    MemoryCategory.LOCATION: 7,
    # How to talk to them.
    MemoryCategory.PREFERENCE: 8,
    # What to talk about.
    MemoryCategory.INTEREST: 9,
    MemoryCategory.SHARED_EXPERIENCE: 10,
    # Unclassified, and last: the model reaches for it when nothing else fits.
    MemoryCategory.OTHER: 11,
}


class OmissionReason(StrEnum):
    """Why a candidate did not make it into a context.

    Every omission has one. A selection that silently dropped things would make
    a short context indistinguishable from a small budget.
    """

    #: The budget had no room left for this memory's text.
    OVER_BUDGET = "over_budget"
    #: The context had already reached its cap on how many memories to include.
    OVER_LIMIT = "over_limit"


def estimate_tokens(text: str) -> int:
    """Return the estimated token cost of a string.

    Deterministic by construction: the same text always costs the same, so a
    budget decision is reproducible and a test can assert on it.
    """
    return max(1, -(-len(text) // CHARS_PER_TOKEN))


def memory_tokens(memory: Memory) -> int:
    """Return the estimated cost of putting one memory in a context.

    Its text plus the overhead of presenting it. Counted through one function so
    the budget the selector enforces and the size a caller reports cannot
    disagree.
    """
    return estimate_tokens(memory.value) + TOKENS_PER_MEMORY


def ordering_key(memory: Memory) -> tuple[int, float, float, float, int]:
    """Return the sort key for one memory.

    Lower sorts first. Importance, confidence and recency are negated so that
    *more* of each sorts earlier while the whole key stays ascending -- one
    direction is easier to reason about than five.
    """
    return (
        CATEGORY_PRIORITY.get(memory.category, len(CATEGORY_PRIORITY)),
        -memory.importance.value,
        -memory.confidence.value,
        -memory.created_at.timestamp(),
        -int(memory.id),
    )


def rank(memories: Sequence[Memory]) -> tuple[Memory, ...]:
    """Return memories in retrieval order.

    A total order: two memories can only compare equal if they have the same
    identifier, so the result does not depend on the order they arrived in.
    """
    return tuple(sorted(memories, key=ordering_key))


@dataclass(frozen=True, slots=True)
class Omitted:
    """One candidate that was ranked but not included.

    Attributes:
        memory: What was left out.
        reason: Why.
        rank: Where it placed, counting from one. Kept because "it was fourth
            and the budget ran out" and "it was ninetieth" are different things
            to know about a budget.
    """

    memory: Memory
    reason: OmissionReason
    rank: int


@dataclass(frozen=True, slots=True)
class Selection:
    """What one selection chose, and what it did not.

    Attributes:
        selected: The memories to use, in rank order.
        omitted: Everything ranked and not selected, in rank order.
        tokens: The estimated cost of the selected memories.
        budget: The budget it was measured against.
        candidates: How many memories were considered.
    """

    selected: tuple[Memory, ...]
    omitted: tuple[Omitted, ...]
    tokens: int
    budget: int
    candidates: int

    @property
    def is_empty(self) -> bool:
        """Whether nothing was selected."""
        return not self.selected

    @property
    def remaining(self) -> int:
        """How much of the budget was left unused."""
        return self.budget - self.tokens

    @property
    def dropped_for_budget(self) -> int:
        """How many memories the budget excluded.

        The number that says whether the budget is too small, as opposed to the
        person simply not being well known yet.
        """
        return sum(1 for item in self.omitted if item.reason is OmissionReason.OVER_BUDGET)


@dataclass(frozen=True, slots=True)
class SelectionRules:
    """What bounds a selection.

    Attributes:
        token_budget: The most a context may cost, estimated.
        max_memories: The most it may contain, however small they are. A context
            of forty true facts is worse than one of eight: the model has to
            weigh them all, and the eight that matter get diluted.
    """

    token_budget: int = 800
    max_memories: int = 20

    def __post_init__(self) -> None:
        """Validate the bounds.

        Raises:
            DomainValidationError: If either bound is not positive. A budget of
                zero is not a small context, it is a caller that meant to switch
                retrieval off and should say so.
        """
        if self.token_budget < 1:
            msg = f"A token budget must be positive, got {self.token_budget}"
            raise DomainValidationError(msg, user_message="That context budget is not usable.")
        if self.max_memories < 1:
            msg = f"A memory limit must be positive, got {self.max_memories}"
            raise DomainValidationError(msg, user_message="That context limit is not usable.")


class MemorySelector:
    """Chooses which of a contact's memories belong in a context.

    Pure: no repository, no clock, no configuration beyond the rules it is
    given. That is what lets the ranking be tested exhaustively without a
    database, and what will let a semantic selector be compared against it on
    the same inputs (ADR-060).
    """

    __slots__ = ("_rules",)

    def __init__(self, rules: SelectionRules | None = None) -> None:
        """Take the bounds this selector enforces."""
        self._rules = rules if rules is not None else SelectionRules()

    @property
    def rules(self) -> SelectionRules:
        """Return the bounds in force."""
        return self._rules

    def select(self, memories: Sequence[Memory]) -> Selection:
        """Rank the candidates and take as many as fit.

        Args:
            memories: The candidates. Expected to be one contact's live
                memories; this does not check, because scoping is the
                repository's job and duplicating it here would be a second place
                to get it wrong.

        Returns:
            What was chosen, what was not, and why.
        """
        ranked = rank(memories)
        selected: list[Memory] = []
        omitted: list[Omitted] = []
        spent = 0

        for position, memory in enumerate(ranked, start=1):
            if len(selected) >= self._rules.max_memories:
                omitted.append(Omitted(memory, OmissionReason.OVER_LIMIT, position))
                continue

            cost = memory_tokens(memory)
            if spent + cost > self._rules.token_budget:
                # Skipped, not stopped: one long fact near the top must not
                # empty the context beneath it. Nothing is reordered by this --
                # the walk is still in rank order.
                omitted.append(Omitted(memory, OmissionReason.OVER_BUDGET, position))
                continue

            selected.append(memory)
            spent += cost

        return Selection(
            selected=tuple(selected),
            omitted=tuple(omitted),
            tokens=spent,
            budget=self._rules.token_budget,
            candidates=len(ranked),
        )


__all__ = [
    "CATEGORY_PRIORITY",
    "CHARS_PER_TOKEN",
    "TOKENS_PER_MEMORY",
    "MemorySelector",
    "OmissionReason",
    "Omitted",
    "Selection",
    "SelectionRules",
    "estimate_tokens",
    "memory_tokens",
    "ordering_key",
    "rank",
]
