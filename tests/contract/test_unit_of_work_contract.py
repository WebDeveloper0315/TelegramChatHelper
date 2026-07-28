"""Shared contract tests for the unit of work.

Run against both the SQLAlchemy implementation and the in-memory fake. The fake
is what use-case tests will run on for the rest of the project, so its
divergence from real transaction semantics would quietly invalidate every one of
them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select

from tests.fakes.unit_of_work import InMemoryUnitOfWork
from tgassist.domain.errors import TransactionFailedError
from tgassist.domain.events import DomainEvent
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import SqlAlchemyUnitOfWork, SqliteDatabase

_metadata = MetaData()
sample = Table(
    "contract_sample",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("value", String(32), nullable=False, unique=True),
)


class SampleEvent(DomainEvent):
    """An event for verifying publication timing."""


@pytest.fixture
async def sqlite_uow(tmp_path: Path) -> AsyncIterator[SqlAlchemyUnitOfWork]:
    """A unit of work over a real SQLite database with one table."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "contract.db"))
    await database.connect()
    await database.executor.run(_metadata.create_all, database.connection)
    try:
        yield SqlAlchemyUnitOfWork(database)
    finally:
        await database.close()


@pytest.fixture
def memory_uow() -> InMemoryUnitOfWork:
    """A unit of work over an in-memory dictionary."""
    return InMemoryUnitOfWork()


@pytest.fixture(params=["sqlite", "memory"])
def uow(request: pytest.FixtureRequest) -> UnitOfWork:
    """Every unit of work implementation."""
    name = "sqlite_uow" if request.param == "sqlite" else "memory_uow"
    resolved: UnitOfWork = request.getfixturevalue(name)
    return resolved


class TestUnitOfWorkContract:
    def test_satisfies_the_protocol(self, uow: UnitOfWork) -> None:
        assert isinstance(uow, UnitOfWork)

    async def test_starts_inactive(self, uow: UnitOfWork) -> None:
        assert not uow.is_active
        assert not uow.is_committed

    async def test_entering_begins_a_transaction(self, uow: UnitOfWork) -> None:
        async with uow:
            assert uow.is_active

    async def test_commit_marks_committed(self, uow: UnitOfWork) -> None:
        async with uow:
            await uow.commit()

        assert uow.is_committed
        assert not uow.is_active

    async def test_leaving_without_commit_rolls_back(self, uow: UnitOfWork) -> None:
        # No implicit commit: forgetting to commit must lose the work loudly
        # rather than persist a half-finished operation quietly.
        async with uow:
            pass

        assert not uow.is_committed

    async def test_rollback_is_safe_when_nothing_is_open(self, uow: UnitOfWork) -> None:
        await uow.rollback()

    async def test_rollback_is_safe_twice(self, uow: UnitOfWork) -> None:
        async with uow:
            await uow.rollback()
            await uow.rollback()

    async def test_commit_without_a_transaction_raises(self, uow: UnitOfWork) -> None:
        with pytest.raises(TransactionFailedError):
            await uow.commit()

    async def test_nesting_is_refused(self, uow: UnitOfWork) -> None:
        # Two overlapping boundaries mean neither is a boundary.
        async with uow:
            with pytest.raises(TransactionFailedError):
                async with uow:
                    pass

    async def test_events_are_withheld_until_commit(self, uow: UnitOfWork) -> None:
        async with uow:
            uow.add_event(SampleEvent())
            assert uow.collect_events() == ()

    async def test_events_are_released_after_commit(self, uow: UnitOfWork) -> None:
        async with uow:
            uow.add_event(SampleEvent())
            await uow.commit()

        assert len(uow.collect_events()) == 1

    async def test_events_are_discarded_on_rollback(self, uow: UnitOfWork) -> None:
        # Announcing a fact that was rolled back is the failure this prevents.
        async with uow:
            uow.add_event(SampleEvent())
            await uow.rollback()

        assert uow.collect_events() == ()

    async def test_events_are_released_only_once(self, uow: UnitOfWork) -> None:
        async with uow:
            uow.add_event(SampleEvent())
            await uow.commit()

        assert len(uow.collect_events()) == 1
        assert uow.collect_events() == ()

    async def test_events_do_not_survive_a_failed_transaction(self, uow: UnitOfWork) -> None:
        with pytest.raises(RuntimeError):  # noqa: PT012 - the work must precede the failure
            async with uow:
                uow.add_event(SampleEvent())
                raise RuntimeError("use case failed")

        assert uow.collect_events() == ()

    async def test_savepoint_rolls_back_only_its_own_work(self, uow: UnitOfWork) -> None:
        async with uow:
            with pytest.raises(RuntimeError):
                async with uow.savepoint():
                    raise RuntimeError("partial failure")
            # The enclosing transaction survives the savepoint's failure.
            assert uow.is_active
            await uow.commit()

        assert uow.is_committed

    async def test_savepoint_commits_cleanly(self, uow: UnitOfWork) -> None:
        async with uow:
            async with uow.savepoint():
                pass
            await uow.commit()

        assert uow.is_committed

    async def test_savepoint_outside_a_transaction_raises(self, uow: UnitOfWork) -> None:
        with pytest.raises(TransactionFailedError):
            async with uow.savepoint():
                pass

    async def test_can_be_reused_after_commit(self, uow: UnitOfWork) -> None:
        async with uow:
            await uow.commit()
        async with uow:
            await uow.commit()

        assert uow.is_committed


class TestSqliteUnitOfWorkPersistence:
    """Behaviour that requires a real database to observe."""

    async def test_committed_writes_are_visible(self, sqlite_uow: SqlAlchemyUnitOfWork) -> None:
        async with sqlite_uow as uow:
            await uow.database.executor.run(
                uow.connection.execute, insert(sample).values(id=1, value="kept")
            )
            await uow.commit()

        async with sqlite_uow as uow:
            rows = await uow.database.executor.run(
                lambda: uow.connection.execute(select(sample.c.value)).fetchall()
            )

        assert [row.value for row in rows] == ["kept"]

    async def test_rolled_back_writes_are_not_visible(
        self, sqlite_uow: SqlAlchemyUnitOfWork
    ) -> None:
        async with sqlite_uow as uow:
            await uow.database.executor.run(
                uow.connection.execute, insert(sample).values(id=1, value="discarded")
            )
            await uow.rollback()

        async with sqlite_uow as uow:
            rows = await uow.database.executor.run(
                lambda: uow.connection.execute(select(sample.c.value)).fetchall()
            )

        assert rows == []

    async def test_an_exception_rolls_back(self, sqlite_uow: SqlAlchemyUnitOfWork) -> None:
        with pytest.raises(RuntimeError):  # noqa: PT012 - the work must precede the failure
            async with sqlite_uow as uow:
                await uow.database.executor.run(
                    uow.connection.execute, insert(sample).values(id=1, value="lost")
                )
                raise RuntimeError("use case failed")

        async with sqlite_uow as uow:
            rows = await uow.database.executor.run(
                lambda: uow.connection.execute(select(sample.c.value)).fetchall()
            )

        assert rows == []

    async def test_savepoint_discards_only_its_own_writes(
        self, sqlite_uow: SqlAlchemyUnitOfWork
    ) -> None:
        # The bulk-import case: one bad record must not discard the batch.
        async with sqlite_uow as uow:
            await uow.database.executor.run(
                uow.connection.execute, insert(sample).values(id=1, value="good")
            )
            with pytest.raises(RuntimeError):  # noqa: PT012 - the work must precede the failure
                async with uow.savepoint():
                    await uow.database.executor.run(
                        uow.connection.execute, insert(sample).values(id=2, value="bad")
                    )
                    raise RuntimeError("record rejected")
            await uow.commit()

        async with sqlite_uow as uow:
            rows = await uow.database.executor.run(
                lambda: uow.connection.execute(select(sample.c.value)).fetchall()
            )

        assert [row.value for row in rows] == ["good"]
