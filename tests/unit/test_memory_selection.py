"""The ranking, exhaustively.

The selector is a pure function, so this file can assert on it completely: every
ranking key in isolation, every key's precedence over the next, the budget, the
cap, and the guarantee the whole thing rests on -- that the same memories in a
different order produce the same selection.

That completeness is the point of having built retrieval deterministically
first. A semantic selector will be compared against these same inputs, and the
comparison is only meaningful because this side of it is fully pinned down
(ADR-060).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ContactId,
    ConversationId,
    MemoryId,
    MemoryProposalId,
)
from tgassist.domain.model.memory import (
    Confidence,
    Importance,
    Memory,
    MemoryCategory,
    MemoryKey,
    MemorySource,
)
from tgassist.domain.services.memory_selection import (
    CATEGORY_PRIORITY,
    TOKENS_PER_MEMORY,
    MemorySelector,
    OmissionReason,
    SelectionRules,
    estimate_tokens,
    memory_tokens,
    rank,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

ACCOUNT = AccountId(1)
CONTACT = ContactId(101)


def make_memory(  # noqa: PLR0913 - one argument per ranking key a test varies
    memory_id: int,
    *,
    category: MemoryCategory = MemoryCategory.OTHER,
    value: str = "a fact",
    confidence: float = 0.5,
    importance: float = 0.5,
    offset_minutes: int = 0,
    retrieval_count: int = 0,
) -> Memory:
    """Build a memory with one ranking key varied at a time."""
    created_at = NOW + timedelta(minutes=offset_minutes)
    return Memory(
        id=MemoryId(memory_id),
        account_id=ACCOUNT,
        contact_id=CONTACT,
        category=category,
        key=MemoryKey.of(f"{value} {memory_id}"),
        value=value,
        confidence=Confidence(confidence),
        source=MemorySource.AI_APPROVED,
        proposal_id=MemoryProposalId(500 + memory_id),
        conversation_id=ConversationId(301),
        ai_call_id=AiCallId(401),
        created_at=created_at,
        importance=Importance(importance),
        retrieval_count=retrieval_count,
        last_retrieved_at=created_at + timedelta(hours=1) if retrieval_count else None,
    )


def ids(memories: Any) -> list[int]:
    """Return identifiers, for readable assertions."""
    return [int(memory.id) for memory in memories]


# ---------------------------------------------------------------------------
# The category order
# ---------------------------------------------------------------------------


class TestCategoryPriority:
    def test_every_category_has_a_priority(self) -> None:
        # A category missing from the table would sort as though it were the
        # least important thing known about somebody -- silently.
        assert set(CATEGORY_PRIORITY) == set(MemoryCategory)

    def test_the_priorities_are_distinct(self) -> None:
        # Two categories sharing a rank would push the decision down to
        # importance, which is a different claim from "these are equally
        # urgent".
        assert len(set(CATEGORY_PRIORITY.values())) == len(CATEGORY_PRIORITY)

    def test_a_constraint_outranks_everything(self) -> None:
        # Ignoring something the person asked for is worse than saying nothing.
        assert CATEGORY_PRIORITY[MemoryCategory.CONSTRAINT] == min(CATEGORY_PRIORITY.values())

    def test_the_unclassified_come_last(self) -> None:
        assert CATEGORY_PRIORITY[MemoryCategory.OTHER] == max(CATEGORY_PRIORITY.values())

    def test_time_sensitive_facts_outrank_durable_ones(self) -> None:
        # A durable fact is equally true next week; a plan is not.
        assert CATEGORY_PRIORITY[MemoryCategory.PLAN] < CATEGORY_PRIORITY[MemoryCategory.LOCATION]

    def test_an_open_question_outranks_an_interest(self) -> None:
        assert (
            CATEGORY_PRIORITY[MemoryCategory.OPEN_QUESTION]
            < CATEGORY_PRIORITY[MemoryCategory.INTEREST]
        )

    def test_category_decides_before_anything_else(self) -> None:
        # A certain, important interest still comes after an uncertain,
        # unimportant constraint.
        interest = make_memory(1, category=MemoryCategory.INTEREST, confidence=1.0, importance=1.0)
        constraint = make_memory(
            2, category=MemoryCategory.CONSTRAINT, confidence=0.1, importance=0.0
        )

        assert ids(rank([interest, constraint])) == [2, 1]


# ---------------------------------------------------------------------------
# The remaining keys, each in isolation
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_importance_orders_within_a_category(self) -> None:
        low = make_memory(1, importance=0.25)
        high = make_memory(2, importance=0.75)

        assert ids(rank([low, high])) == [2, 1]

    def test_importance_outranks_confidence(self) -> None:
        # A person's judgement of what is worth knowing outranks a machine's
        # estimate of what is true -- and self-reported confidence is poorly
        # calibrated (ADR-060).
        certain = make_memory(1, importance=0.25, confidence=1.0)
        important = make_memory(2, importance=0.75, confidence=0.1)

        assert ids(rank([certain, important])) == [2, 1]

    def test_confidence_orders_when_importance_ties(self) -> None:
        unsure = make_memory(1, confidence=0.3)
        sure = make_memory(2, confidence=0.9)

        assert ids(rank([unsure, sure])) == [2, 1]

    def test_confidence_outranks_recency(self) -> None:
        recent = make_memory(1, confidence=0.3, offset_minutes=100)
        sure = make_memory(2, confidence=0.9, offset_minutes=0)

        assert ids(rank([recent, sure])) == [2, 1]

    def test_recency_orders_when_confidence_ties(self) -> None:
        older = make_memory(1, offset_minutes=0)
        newer = make_memory(2, offset_minutes=10)

        assert ids(rank([older, newer])) == [2, 1]

    def test_the_identifier_breaks_a_total_tie(self) -> None:
        # Arbitrary but total, so the order never depends on what the database
        # happened to return.
        first = make_memory(1)
        second = make_memory(2)

        assert ids(rank([first, second])) == [2, 1]

    def test_retrieval_history_is_not_a_ranking_key(self) -> None:
        # The feedback loop this avoids: ranking by last retrieval would make a
        # retrieved memory rank higher and so be retrieved again.
        never = make_memory(1, retrieval_count=0)
        often = make_memory(2, retrieval_count=99)

        assert ids(rank([never, often])) == ids(rank([often, never])) == [2, 1]


class TestStability:
    def test_the_input_order_does_not_matter(self) -> None:
        # The whole of "deterministic": the same set always produces the same
        # sequence, whatever order it arrived in.
        memories = [
            make_memory(1, category=MemoryCategory.PLAN),
            make_memory(2, category=MemoryCategory.CONSTRAINT),
            make_memory(3, category=MemoryCategory.INTEREST),
            make_memory(4, category=MemoryCategory.PLAN, importance=0.9),
        ]

        forwards = ids(rank(memories))
        backwards = ids(rank(list(reversed(memories))))

        assert forwards == backwards == [2, 4, 1, 3]

    def test_ranking_twice_gives_the_same_answer(self) -> None:
        memories = [make_memory(index) for index in range(1, 8)]

        assert ids(rank(memories)) == ids(rank(memories))

    def test_ranking_nothing_gives_nothing(self) -> None:
        assert rank([]) == ()

    def test_ranking_one_gives_one(self) -> None:
        assert ids(rank([make_memory(1)])) == [1]


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


class TestTokenEstimation:
    def test_it_counts_four_characters_to_a_token(self) -> None:
        assert estimate_tokens("12345678") == 2

    def test_it_rounds_up(self) -> None:
        # A budget that rounded down would be exceeded by every odd string.
        assert estimate_tokens("123456789") == 3

    def test_nothing_still_costs_something(self) -> None:
        assert estimate_tokens("") == 1

    def test_a_memory_costs_more_than_its_text(self) -> None:
        # The line that introduces it, the category label, the separators. A
        # context of many short facts would otherwise be under-estimated.
        memory = make_memory(1, value="12345678")

        assert memory_tokens(memory) == 2 + TOKENS_PER_MEMORY

    def test_the_estimate_is_deterministic(self) -> None:
        assert estimate_tokens("some text") == estimate_tokens("some text")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


class TestSelection:
    def test_everything_fits_when_the_budget_is_generous(self) -> None:
        memories = [make_memory(index) for index in range(1, 5)]

        selection = MemorySelector().select(memories)

        assert len(selection.selected) == 4
        assert not selection.omitted

    def test_nothing_selects_nothing(self) -> None:
        selection = MemorySelector().select([])

        assert selection.is_empty
        assert selection.tokens == 0
        assert selection.candidates == 0

    def test_the_selected_are_in_rank_order(self) -> None:
        memories = [
            make_memory(1, category=MemoryCategory.INTEREST),
            make_memory(2, category=MemoryCategory.CONSTRAINT),
        ]

        selection = MemorySelector().select(memories)

        assert ids(selection.selected) == [2, 1]

    def test_it_reports_what_it_cost(self) -> None:
        memory = make_memory(1, value="12345678")

        selection = MemorySelector().select([memory])

        assert selection.tokens == memory_tokens(memory)
        assert selection.remaining == selection.budget - selection.tokens

    def test_it_counts_the_candidates_it_saw(self) -> None:
        selection = MemorySelector(SelectionRules(token_budget=10)).select(
            [make_memory(index, value="x" * 40) for index in range(1, 6)]
        )

        assert selection.candidates == 5


class TestTheTokenBudget:
    def test_what_does_not_fit_is_omitted(self) -> None:
        rules = SelectionRules(token_budget=memory_tokens(make_memory(1)) + 1)
        memories = [make_memory(1), make_memory(2)]

        selection = MemorySelector(rules).select(memories)

        assert len(selection.selected) == 1
        assert selection.omitted[0].reason is OmissionReason.OVER_BUDGET

    def test_an_omission_says_where_it_ranked(self) -> None:
        # "It was fourth and the budget ran out" and "it was ninetieth" are
        # different things to know about a budget.
        rules = SelectionRules(token_budget=memory_tokens(make_memory(1)))
        memories = [make_memory(1), make_memory(2)]

        selection = MemorySelector(rules).select(memories)

        assert selection.omitted[0].rank == 2

    def test_a_long_memory_does_not_empty_the_context_beneath_it(self) -> None:
        # Skipped, not stopped. The alternative wastes the whole budget on the
        # first thing that does not fit.
        rules = SelectionRules(token_budget=30)
        long_one = make_memory(1, category=MemoryCategory.CONSTRAINT, value="x" * 200)
        short_one = make_memory(2, category=MemoryCategory.PLAN, value="short")

        selection = MemorySelector(rules).select([long_one, short_one])

        assert ids(selection.selected) == [2]
        assert ids([item.memory for item in selection.omitted]) == [1]

    def test_but_skipping_does_not_reorder_what_was_selected(self) -> None:
        # Ranking decides priority; the budget decides fit. Neither changes the
        # other's answer.
        rules = SelectionRules(token_budget=40)
        memories = [
            make_memory(1, category=MemoryCategory.CONSTRAINT, value="short"),
            make_memory(2, category=MemoryCategory.PLAN, value="x" * 200),
            make_memory(3, category=MemoryCategory.INTEREST, value="short"),
        ]

        selection = MemorySelector(rules).select(memories)

        assert ids(selection.selected) == [1, 3]

    def test_nothing_is_ever_shortened_to_fit(self) -> None:
        # A truncated fact is a different fact.
        rules = SelectionRules(token_budget=5)
        memory = make_memory(1, value="x" * 200)

        selection = MemorySelector(rules).select([memory])

        assert selection.is_empty
        assert selection.omitted[0].memory.value == "x" * 200

    def test_the_budget_is_never_exceeded(self) -> None:
        rules = SelectionRules(token_budget=25)
        memories = [make_memory(index, value="x" * 20) for index in range(1, 10)]

        selection = MemorySelector(rules).select(memories)

        assert selection.tokens <= 25

    def test_it_counts_what_the_budget_dropped(self) -> None:
        # The number that says whether the budget is too small, as opposed to
        # the person simply not being well known yet.
        rules = SelectionRules(token_budget=10, max_memories=1)
        memories = [make_memory(index, value="x" * 40) for index in range(1, 4)]

        selection = MemorySelector(rules).select(memories)

        assert selection.dropped_for_budget == 3


class TestTheMemoryCap:
    def test_a_context_holds_no_more_than_the_cap(self) -> None:
        # Forty true facts are worse than eight: the eight that matter get
        # diluted.
        rules = SelectionRules(max_memories=2)
        memories = [make_memory(index) for index in range(1, 6)]

        selection = MemorySelector(rules).select(memories)

        assert len(selection.selected) == 2

    def test_the_rest_say_why(self) -> None:
        rules = SelectionRules(max_memories=2)
        memories = [make_memory(index) for index in range(1, 6)]

        selection = MemorySelector(rules).select(memories)

        assert all(item.reason is OmissionReason.OVER_LIMIT for item in selection.omitted)

    def test_the_cap_keeps_the_highest_ranked(self) -> None:
        rules = SelectionRules(max_memories=1)
        memories = [
            make_memory(1, category=MemoryCategory.INTEREST),
            make_memory(2, category=MemoryCategory.CONSTRAINT),
        ]

        selection = MemorySelector(rules).select(memories)

        assert ids(selection.selected) == [2]


class TestTheRules:
    def test_a_budget_of_zero_is_refused(self) -> None:
        # Not a small context: a caller that meant to switch retrieval off and
        # should say so.
        with pytest.raises(DomainValidationError, match="budget must be positive"):
            SelectionRules(token_budget=0)

    def test_a_cap_of_zero_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="limit must be positive"):
            SelectionRules(max_memories=0)

    def test_the_defaults_are_usable(self) -> None:
        rules = SelectionRules()

        assert rules.token_budget > 0
        assert rules.max_memories > 0

    def test_a_selector_reports_its_rules(self) -> None:
        rules = SelectionRules(token_budget=42, max_memories=3)

        assert MemorySelector(rules).rules == rules


class TestManyCategories:
    def test_one_memory_of_every_kind_ranks_by_category(self) -> None:
        # The ordering read end to end, which is the closest thing to a
        # specification of what a context looks like.
        memories = [
            make_memory(index, category=category)
            for index, category in enumerate(MemoryCategory, start=1)
        ]

        selection = MemorySelector(SelectionRules(max_memories=len(MemoryCategory))).select(
            memories
        )

        assert [memory.category for memory in selection.selected] == sorted(
            MemoryCategory, key=lambda category: CATEGORY_PRIORITY[category]
        )

    def test_the_first_is_always_the_constraint(self) -> None:
        memories = [
            make_memory(index, category=category, confidence=0.99, importance=0.99)
            for index, category in enumerate(MemoryCategory, start=1)
            if category is not MemoryCategory.CONSTRAINT
        ]
        memories.append(
            make_memory(99, category=MemoryCategory.CONSTRAINT, confidence=0.01, importance=0.0)
        )

        selection = MemorySelector().select(memories)

        assert selection.selected[0].category is MemoryCategory.CONSTRAINT
