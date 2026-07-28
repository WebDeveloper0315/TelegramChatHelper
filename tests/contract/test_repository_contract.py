"""The repository contract, run against a real and a fake implementation.

Both are exercised by the identical suite in ``tests/support/repository_contract``.
That is the point: a fake that diverges from real behaviour would make every
use-case test built on it a false positive, and running one suite over both is
the only thing that prevents it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.fakes.unit_of_work import InMemoryUnitOfWork
from tests.support.repository_contract import RepositoryContract, RepositoryUnderTest
from tests.support.sample_aggregate import (
    SORT_FIELD,
    InMemoryWidgetRepository,
    SqlWidgetRepository,
    Widget,
    make_widget,
    sample_metadata,
)
from tgassist.domain.model.query import PageRequest
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import SqlAlchemyUnitOfWork, SqliteDatabase


@pytest.fixture
async def sql_repository(tmp_path: Path) -> AsyncIterator[RepositoryUnderTest[Widget, int]]:
    """The SQLAlchemy repository, inside an open transaction."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "contract.db"))
    await database.connect()
    await database.executor.run(sample_metadata.create_all, database.connection)
    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    try:
        repository = SqlWidgetRepository(uow)
        yield RepositoryUnderTest(
            add=repository.add,
            get=repository.get,
            page=repository.page,
            identity=lambda widget: widget.id,
            make=make_widget,
            uow=uow,
            soft_delete=repository.soft_delete,
            count=repository.count,
            sort_field=SORT_FIELD,
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_repository() -> RepositoryUnderTest[Widget, int]:
    """The in-memory repository, inside an open transaction."""
    uow = InMemoryUnitOfWork()
    await uow.begin()
    repository = InMemoryWidgetRepository()
    return RepositoryUnderTest(
        add=repository.add,
        get=repository.get,
        page=repository.page,
        identity=lambda widget: widget.id,
        make=make_widget,
        uow=uow,
        soft_delete=repository.soft_delete,
        count=repository.count,
        sort_field=SORT_FIELD,
    )


class TestSqlRepositoryContract(RepositoryContract[Widget, int]):
    """The SQLAlchemy implementation."""

    @pytest.fixture
    def subject(
        self, sql_repository: RepositoryUnderTest[Widget, int]
    ) -> RepositoryUnderTest[Widget, int]:
        return sql_repository


class TestInMemoryRepositoryContract(RepositoryContract[Widget, int]):
    """The in-memory fake."""

    @pytest.fixture
    def subject(
        self, memory_repository: RepositoryUnderTest[Widget, int]
    ) -> RepositoryUnderTest[Widget, int]:
        return memory_repository


class TestImplementationsAgree:
    """The two implementations must produce identical results.

    A contract suite proves each satisfies the rules independently. This proves
    they agree with each other, which is the property a use-case test actually
    relies on when it swaps one for the other.
    """

    async def test_pagination_order_matches(
        self,
        sql_repository: RepositoryUnderTest[Widget, int],
        memory_repository: RepositoryUnderTest[Widget, int],
    ) -> None:
        for index in range(15):
            widget = make_widget(index + 1)
            await sql_repository.add(widget)
            await memory_repository.add(widget)

        sql_ids = [w.id for w in (await sql_repository.page(PageRequest(limit=100))).items]
        memory_ids = [w.id for w in (await memory_repository.page(PageRequest(limit=100))).items]

        assert sql_ids == memory_ids

    async def test_paging_in_steps_matches(
        self,
        sql_repository: RepositoryUnderTest[Widget, int],
        memory_repository: RepositoryUnderTest[Widget, int],
    ) -> None:
        for index in range(15):
            widget = make_widget(index + 1)
            await sql_repository.add(widget)
            await memory_repository.add(widget)

        async def walk(subject: RepositoryUnderTest[Widget, int]) -> list[int]:
            collected: list[int] = []
            cursor: str | None = None
            while True:
                page = await subject.page(PageRequest(cursor=cursor, limit=4))
                collected.extend(w.id for w in page.items)
                if not page.has_more:
                    return collected
                cursor = page.next_cursor

        assert await walk(sql_repository) == await walk(memory_repository)
