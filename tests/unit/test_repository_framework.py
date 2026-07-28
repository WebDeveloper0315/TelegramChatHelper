"""Query value objects, cursors, pagination mechanics and mapping."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select

from tests.support.sample_aggregate import (
    SAMPLE_EPOCH,
    SqlWidgetRepository,
    Widget,
    WidgetMapper,
    make_widget,
    sample_metadata,
    widgets,
)
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    PageRequest,
    SortDirection,
    SortOrder,
    TimeWindow,
)
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import SqlAlchemyUnitOfWork, SqliteDatabase
from tgassist.infrastructure.persistence.cursor import Cursor
from tgassist.infrastructure.persistence.mapper import column_names
from tgassist.infrastructure.persistence.mappers import (
    from_stored_bool,
    from_stored_datetime,
    from_stored_json,
    row_to_dict,
    to_stored_bool,
    to_stored_datetime,
    to_stored_json,
)
from tgassist.infrastructure.persistence.pagination import KeysetPaginator


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[SqlWidgetRepository]:
    """A repository inside an open transaction."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "framework.db"))
    await database.connect()
    await database.executor.run(sample_metadata.create_all, database.connection)
    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    try:
        yield SqlWidgetRepository(uow)
    finally:
        await uow.rollback()
        await database.close()


# ---------------------------------------------------------------------------
# Query value objects
# ---------------------------------------------------------------------------


class TestSortOrder:
    def test_defaults_to_descending(self) -> None:
        assert SortOrder(field="created_at").direction is SortDirection.DESCENDING

    def test_rejects_an_empty_field(self) -> None:
        with pytest.raises(ValueError, match="field name"):
            SortOrder(field="")

    def test_named_constructors(self) -> None:
        assert SortOrder.newest_first("x").direction is SortDirection.DESCENDING
        assert SortOrder.oldest_first("x").direction is SortDirection.ASCENDING

    def test_direction_inverts(self) -> None:
        assert SortDirection.ASCENDING.inverted() is SortDirection.DESCENDING
        assert SortDirection.DESCENDING.inverted() is SortDirection.ASCENDING

    def test_is_immutable(self) -> None:
        order = SortOrder(field="x")

        # A frozen slots dataclass reports "cannot assign to field".
        with pytest.raises((AttributeError, TypeError), match=r"cannot assign|frozen|immutable"):
            order.field = "y"  # type: ignore[misc]


class TestPageRequest:
    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            (DEFAULT_PAGE_SIZE, DEFAULT_PAGE_SIZE),
            (0, 1),
            (-5, 1),
            (10, 10),
            (10_000, MAX_PAGE_SIZE),
        ],
    )
    def test_limit_is_clamped(self, requested: int, expected: int) -> None:
        assert PageRequest(limit=requested).effective_limit() == expected

    def test_continuing_from_preserves_shape(self) -> None:
        request = PageRequest(limit=7, sort=SortOrder.oldest_first("x"))

        moved = request.continuing_from("abc")

        assert moved.cursor == "abc"
        assert moved.limit == 7
        assert moved.sort == request.sort

    def test_first_page_clears_the_cursor(self) -> None:
        assert PageRequest(cursor="abc", limit=7).first_page().cursor is None


class TestTimeWindow:
    def test_rejects_naive_instants(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            TimeWindow(datetime(2026, 1, 1), datetime(2026, 1, 2))  # noqa: DTZ001

    def test_rejects_an_inverted_window(self) -> None:
        later = SAMPLE_EPOCH + timedelta(days=1)

        with pytest.raises(ValueError, match="cannot end before"):
            TimeWindow(later, SAMPLE_EPOCH)

    def test_reports_duration(self) -> None:
        window = TimeWindow(SAMPLE_EPOCH, SAMPLE_EPOCH + timedelta(hours=2))

        assert window.duration_seconds == pytest.approx(7200.0)

    def test_containment_is_inclusive(self) -> None:
        window = TimeWindow(SAMPLE_EPOCH, SAMPLE_EPOCH + timedelta(hours=1))

        assert window.contains(SAMPLE_EPOCH)
        assert window.contains(SAMPLE_EPOCH + timedelta(hours=1))
        assert not window.contains(SAMPLE_EPOCH + timedelta(hours=2))

    def test_rejects_a_naive_containment_test(self) -> None:
        window = TimeWindow(SAMPLE_EPOCH, SAMPLE_EPOCH + timedelta(hours=1))

        with pytest.raises(ValueError, match="naive"):
            window.contains(datetime(2026, 1, 1))  # noqa: DTZ001


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------


class TestCursor:
    def test_round_trips_scalars(self) -> None:
        token = Cursor.encode({"s": 42, "t": "x"})

        assert Cursor.decode(token) == {"s": 42, "t": "x"}

    def test_datetimes_round_trip_losslessly(self) -> None:
        # Regression: a generic str() fallback rendered a datetime in a form
        # that parsed back differently, and the resulting cursor skipped rows.
        moment = datetime(2026, 3, 1, 9, 30, 15, 123456, tzinfo=UTC)

        decoded = Cursor.decode(Cursor.encode({"s": moment}))

        assert decoded is not None
        assert datetime.fromisoformat(decoded["s"]) == moment

    def test_absent_and_malformed_tokens_decode_to_none(self) -> None:
        assert Cursor.decode(None) is None
        assert Cursor.decode("") is None
        assert Cursor.decode("not-a-cursor!!") is None

    def test_is_url_safe(self) -> None:
        token = Cursor.encode({"value": "a/b+c=d"})

        assert "/" not in token
        assert "+" not in token

    def test_is_opaque_but_not_secret(self) -> None:
        # Deliberately not a security boundary: it discourages hand-crafting,
        # nothing more.
        assert Cursor.encode({"s": 1}) != "s=1"


# ---------------------------------------------------------------------------
# Pagination mechanics
# ---------------------------------------------------------------------------


class TestKeysetPagination:
    async def test_rejects_an_unsupported_sort_field(self, repository: SqlWidgetRepository) -> None:
        # Ignoring the request would return correctly shaped results in the
        # wrong order, which is worse than an error.
        with pytest.raises(ValueError, match="can only be ordered by"):
            await repository.page(PageRequest(sort=SortOrder.newest_first("nonexistent")))

    async def test_ties_on_the_sort_column_are_not_skipped(
        self, repository: SqlWidgetRepository
    ) -> None:
        # The reason a unique tiebreaker is mandatory. make_widget deliberately
        # gives every three widgets the same timestamp.
        for index in range(9):
            await repository.add(make_widget(index + 1))

        seen: list[int] = []
        cursor: str | None = None
        while True:
            page = await repository.page(PageRequest(cursor=cursor, limit=2))
            seen.extend(w.id for w in page.items)
            if not page.has_more:
                break
            cursor = page.next_cursor

        assert sorted(seen) == list(range(1, 10))

    async def test_ascending_and_descending_are_exact_reverses(
        self, repository: SqlWidgetRepository
    ) -> None:
        for index in range(9):
            await repository.add(make_widget(index + 1))

        newest = await repository.page(
            PageRequest(limit=100, sort=SortOrder.newest_first("created_at"))
        )
        oldest = await repository.page(
            PageRequest(limit=100, sort=SortOrder.oldest_first("created_at"))
        )

        assert [w.id for w in newest] == [w.id for w in reversed(list(oldest))]

    async def test_a_new_row_does_not_shift_later_pages(
        self, repository: SqlWidgetRepository
    ) -> None:
        # Offset pagination would skip a row here. Keyset positions by value,
        # so an insert before the cursor cannot displace the page boundary.
        for index in range(6):
            await repository.add(make_widget(index + 1))

        first = await repository.page(PageRequest(limit=3))
        await repository.add(make_widget(100))
        second = await repository.page(PageRequest(cursor=first.next_cursor, limit=3))

        assert not ({w.id for w in first} & {w.id for w in second})

    async def test_paginator_applies_a_lookahead_row(self) -> None:
        paginator = KeysetPaginator(
            sort_column=widgets.c.created_at,
            tiebreak_column=widgets.c.id,
            sort_field="created_at",
        )

        statement = paginator.apply(select(widgets), PageRequest(limit=5))

        assert statement._limit == 6

    async def test_account_scope_is_enforced(self, repository: SqlWidgetRepository) -> None:
        await repository.add(make_widget(1, account_id=1))
        await repository.add(make_widget(2, account_id=2))

        page = await repository.page(PageRequest(limit=10))

        assert [w.id for w in page] == [1]


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


class TestMapping:
    def test_round_trip_preserves_every_field(self) -> None:
        # The property that catches a column added to the schema and forgotten
        # in the mapper, which otherwise looks like a field reverting to default.
        mapper = WidgetMapper()
        original = make_widget(7)

        params = mapper.to_params(original)
        restored = mapper.to_domain(_FakeRow(params))

        assert restored == original

    def test_round_trip_preserves_identity(self) -> None:
        mapper = WidgetMapper()
        original = make_widget(42)

        restored = mapper.to_domain(_FakeRow(mapper.to_params(original)))

        assert restored.id == original.id

    def test_mapper_covers_every_writable_column(self) -> None:
        # Fails the moment a migration adds a column the mapper does not write.
        mapper = WidgetMapper()
        written = column_names(mapper.to_params(make_widget(1)))
        declared = {c.name for c in widgets.columns} - {"deleted_at"}

        assert declared == written

    def test_mapper_is_pure(self) -> None:
        mapper = WidgetMapper()
        widget = make_widget(1)

        assert mapper.to_params(widget) == mapper.to_params(widget)

    def test_batch_mapping_matches_singular(self) -> None:
        mapper = WidgetMapper()
        entities = [make_widget(i) for i in range(1, 4)]

        assert mapper.to_params_many(entities) == [mapper.to_params(e) for e in entities]

    async def test_round_trip_survives_the_database(self, repository: SqlWidgetRepository) -> None:
        original = make_widget(3)
        await repository.add(original)

        assert await repository.get(original.id) == original


class _FakeRow:
    """A row-like object for testing a mapper without a database."""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def __getattr__(self, name: str) -> object:
        return self._values[name]


class TestValueConversion:
    """The converters every mapper uses for types SQLite has no native form for."""

    def test_datetime_round_trips_in_utc(self) -> None:
        value = datetime(2026, 3, 1, 9, 30, tzinfo=timezone(timedelta(hours=9)))

        restored = from_stored_datetime(to_stored_datetime(value))

        assert restored == value
        assert restored is not None
        assert restored.utcoffset() == timedelta(0)

    def test_naive_datetimes_are_refused(self) -> None:
        # The rule the Clock port enforces, applied again at the storage
        # boundary: a naive value records an instant that cannot be interpreted.
        with pytest.raises(ValueError, match="naive"):
            to_stored_datetime(datetime(2026, 1, 1))  # noqa: DTZ001

    def test_naive_stored_values_are_read_as_utc(self) -> None:
        # Tolerated on read for rows written before the rule existed.
        restored = from_stored_datetime("2026-01-01T12:00:00")

        assert restored is not None
        assert restored.utcoffset() == timedelta(0)

    def test_none_passes_through(self) -> None:
        assert to_stored_datetime(None) is None
        assert from_stored_datetime(None) is None
        assert to_stored_bool(None) is None
        assert from_stored_bool(None) is None
        assert to_stored_json(None) is None
        assert from_stored_json(None) is None

    def test_bool_round_trips(self) -> None:
        assert from_stored_bool(to_stored_bool(True)) is True
        assert from_stored_bool(to_stored_bool(False)) is False

    def test_json_round_trips(self) -> None:
        value = {"b": 1, "a": [1, 2, {"c": None}]}

        assert from_stored_json(to_stored_json(value)) == value

    def test_json_encoding_is_stable(self) -> None:
        # Content fingerprints and cache keys depend on equal structures
        # producing byte-identical text; an unsorted dump would invalidate
        # caches at random.
        assert to_stored_json({"b": 1, "a": 2}) == to_stored_json({"a": 2, "b": 1})

    def test_json_preserves_non_ascii(self) -> None:
        assert from_stored_json(to_stored_json({"k": "日本語"})) == {"k": "日本語"}

    def test_row_to_dict_detaches_from_the_result(self) -> None:
        row = cast("Any", _FakeMappingRow({"a": 1}))

        assert row_to_dict(row) == {"a": 1}


class _FakeMappingRow:
    """A row exposing the mapping accessor row_to_dict uses."""

    def __init__(self, values: dict[str, object]) -> None:
        self._mapping = values


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


class TestPage:
    def test_empty_page(self) -> None:
        page: Page[Widget] = Page.empty()

        assert not page
        assert len(page) == 0
        assert not page.has_more

    def test_has_more_tracks_the_cursor(self) -> None:
        assert not Page(items=["a"]).has_more
        assert Page(items=["a"], next_cursor="x").has_more

    def test_iterates(self) -> None:
        assert list(Page(items=["a", "b"])) == ["a", "b"]
