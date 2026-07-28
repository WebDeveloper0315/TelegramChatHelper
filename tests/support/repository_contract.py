"""The shared repository contract suite.

Every repository -- real and fake alike -- inherits from
:class:`RepositoryContract` and thereby runs the same assertions. A fake that
diverges from real behaviour would make every use-case test built on it a false
positive, and the only way to prevent that is to hold both to one suite.

Usage, once an aggregate exists::

    class TestSqlContactRepository(RepositoryContract[Contact, ContactId]):
        @pytest.fixture
        def subject(self, sqlite_uow) -> RepositoryUnderTest[Contact, ContactId]:
            return RepositoryUnderTest(
                repository=SqlContactRepository(sqlite_uow),
                make=lambda n: Contact(...),
                ...
            )

The suite is written against a small adapter rather than against a repository
interface directly, because repositories deliberately do not share one (see
``domain/ports/repository.py``). The adapter names the handful of operations the
contract can talk about; anything beyond it is that repository's own business
and gets its own tests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest, SortOrder
from tgassist.domain.ports.unit_of_work import UnitOfWork


@dataclass
class RepositoryUnderTest[T, ID]:
    """Adapts one repository to the vocabulary the contract suite speaks.

    Attributes:
        add: Persist a new entity.
        get: Fetch by identifier, returning ``None`` when absent.
        page: Fetch a page.
        identity: Extract an entity's identifier.
        make: Build the nth distinct entity. Must produce entities that sort in
            creation order, so pagination assertions have a defined expectation.
        soft_delete: Soft-delete by identifier, if the aggregate supports it.
        count: Count entities, if the aggregate supports it.
        uow: The unit of work the repository is enlisted in.
    """

    add: Callable[[T], Awaitable[None]]
    get: Callable[[ID], Awaitable[T | None]]
    page: Callable[[PageRequest], Awaitable[Page[T]]]
    identity: Callable[[T], ID]
    make: Callable[[int], T]
    uow: UnitOfWork
    soft_delete: Callable[[ID], Awaitable[None]] | None = None
    count: Callable[[], Awaitable[int]] | None = None
    sort_field: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class RepositoryContract[T, ID]:
    """Assertions every repository must satisfy.

    Subclasses provide a ``subject`` fixture returning a
    :class:`RepositoryUnderTest`.
    """

    # -- Identity ---------------------------------------------------------

    async def test_a_stored_entity_can_be_read_back(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        entity = subject.make(1)
        await subject.add(entity)

        found = await subject.get(subject.identity(entity))

        assert found is not None
        assert subject.identity(found) == subject.identity(entity)

    async def test_a_read_back_entity_equals_the_original(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        # The round-trip property: everything the mapper claims survives.
        entity = subject.make(1)
        await subject.add(entity)

        found = await subject.get(subject.identity(entity))

        assert found == entity

    async def test_an_absent_identifier_returns_none(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        # Absence is an ordinary state, not an error. Raising here would force
        # every caller into a try/except for the common case.
        stored = subject.make(1)
        await subject.add(stored)
        other = subject.make(999)

        assert await subject.get(subject.identity(other)) is None

    async def test_reads_are_snapshots_not_live_views(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        # There is no identity map (ADR-015): two reads give two equal objects,
        # not the same object. Callers must compare by identifier.
        entity = subject.make(1)
        await subject.add(entity)
        identifier = subject.identity(entity)

        first = await subject.get(identifier)
        second = await subject.get(identifier)

        assert first == second
        assert first is not second

    # -- Pagination -------------------------------------------------------

    async def test_a_page_respects_its_limit(self, subject: RepositoryUnderTest[T, ID]) -> None:
        for index in range(10):
            await subject.add(subject.make(index + 1))

        page = await subject.page(PageRequest(limit=4))

        assert len(page) == 4
        assert page.has_more

    async def test_the_last_page_has_no_cursor(self, subject: RepositoryUnderTest[T, ID]) -> None:
        for index in range(3):
            await subject.add(subject.make(index + 1))

        page = await subject.page(PageRequest(limit=10))

        assert not page.has_more
        assert page.next_cursor is None

    async def test_an_empty_repository_returns_an_empty_page(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        page = await subject.page(PageRequest(limit=10))

        assert len(page) == 0
        assert not page.has_more

    async def test_paging_visits_every_entity_exactly_once(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        # The property that makes keyset pagination worth its complexity: no
        # row skipped, no row repeated, however small the page.
        total = 25
        for index in range(total):
            await subject.add(subject.make(index + 1))

        seen = await self._walk(subject, PageRequest(limit=4))
        identifiers = [subject.identity(entity) for entity in seen]

        assert len(identifiers) == total
        assert len(set(identifiers)) == total

    async def test_paging_is_stable_across_page_sizes(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        for index in range(20):
            await subject.add(subject.make(index + 1))

        by_threes = await self._walk(subject, PageRequest(limit=3))
        by_sevens = await self._walk(subject, PageRequest(limit=7))
        in_one_go = await self._walk(subject, PageRequest(limit=100))

        ids = [[subject.identity(e) for e in batch] for batch in (by_threes, by_sevens, in_one_go)]
        assert ids[0] == ids[1] == ids[2]

    async def test_a_malformed_cursor_starts_from_the_beginning(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        # A stale bookmark should show the first page, not raise.
        for index in range(5):
            await subject.add(subject.make(index + 1))

        page = await subject.page(PageRequest(cursor="not-a-real-cursor", limit=10))

        assert len(page) == 5

    async def test_sorting_both_ways_reverses_the_order(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        if subject.sort_field is None:
            pytest.skip("This repository exposes no configurable sort field")

        for index in range(6):
            await subject.add(subject.make(index + 1))

        newest = await self._walk(
            subject, PageRequest(limit=100, sort=SortOrder.newest_first(subject.sort_field))
        )
        oldest = await self._walk(
            subject, PageRequest(limit=100, sort=SortOrder.oldest_first(subject.sort_field))
        )

        assert [subject.identity(e) for e in newest] == [
            subject.identity(e) for e in reversed(oldest)
        ]

    # -- Transaction participation ---------------------------------------

    async def test_writes_are_visible_within_the_transaction(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        entity = subject.make(1)
        await subject.add(entity)

        assert await subject.get(subject.identity(entity)) is not None

    async def test_the_repository_does_not_commit(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        # A repository that commits would destroy the unit of work's ability to
        # roll back a composite operation.
        await subject.add(subject.make(1))

        assert not subject.uow.is_committed

    # -- Optional capabilities -------------------------------------------

    async def test_counting_reflects_stored_entities(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        if subject.count is None:
            pytest.skip("This repository does not expose a count")

        assert await subject.count() == 0
        for index in range(4):
            await subject.add(subject.make(index + 1))

        assert await subject.count() == 4

    async def test_soft_deleted_entities_are_excluded(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        if subject.soft_delete is None:
            pytest.skip("This aggregate does not support soft deletion")

        entity = subject.make(1)
        await subject.add(entity)
        await subject.soft_delete(subject.identity(entity))

        assert await subject.get(subject.identity(entity)) is None
        assert len(await subject.page(PageRequest(limit=10))) == 0

    async def test_soft_deleting_twice_is_not_an_error(
        self, subject: RepositoryUnderTest[T, ID]
    ) -> None:
        if subject.soft_delete is None:
            pytest.skip("This aggregate does not support soft deletion")

        entity = subject.make(1)
        await subject.add(entity)
        identifier = subject.identity(entity)

        await subject.soft_delete(identifier)
        await subject.soft_delete(identifier)

    # -- Helpers ----------------------------------------------------------

    @staticmethod
    async def _walk(subject: RepositoryUnderTest[T, ID], request: PageRequest) -> list[T]:
        """Page through everything, guarding against a non-terminating cursor."""
        collected: list[T] = []
        cursor: str | None = None
        for _ in range(1000):
            page = await subject.page(request.continuing_from(cursor))
            collected.extend(page.items)
            if not page.has_more:
                return collected
            assert page.next_cursor != cursor, "Cursor did not advance; pagination would loop"
            cursor = page.next_cursor
        pytest.fail("Pagination did not terminate")
