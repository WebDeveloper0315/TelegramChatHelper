"""A toy aggregate used only to exercise the repository framework.

This is test scaffolding, not a business entity. It exists because a contract
suite with no implementation proves nothing, and the framework must be shown to
work before the first real aggregate is built on it. Its table is created
directly by the test fixture rather than by a migration, so it never reaches a
user's database.

``Widget`` is deliberately shaped like a real aggregate -- frozen, identified,
soft-deletable, with a non-unique sort column -- so that the framework is
exercised where it is actually subtle. A non-unique ``created_at`` in particular
is what makes the pagination tiebreaker matter.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    func,
    insert,
    select,
    update,
)

from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.infrastructure.persistence.cursor import Cursor
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.pagination import (
    SORT_KEY,
    TIEBREAK_KEY,
    KeysetPaginator,
)
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

SAMPLE_EPOCH = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

sample_metadata = MetaData()

widgets = Table(
    "sample_widgets",
    sample_metadata,
    Column("id", Integer, primary_key=True),
    Column("account_id", Integer, nullable=False),
    Column("name", String(64), nullable=False),
    # Deliberately not unique, and deliberately coarse: several widgets share a
    # timestamp, which is exactly the case a missing tiebreaker gets wrong.
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
)

SORT_FIELD = "created_at"


@dataclass(frozen=True, slots=True)
class Widget:
    """A minimal aggregate: identified, immutable, soft-deletable."""

    id: int
    account_id: int
    name: str
    created_at: datetime

    def renamed(self, name: str) -> Widget:
        """Return a copy with a new name, as an immutable entity must."""
        return replace(self, name=name)


class WidgetMapper(EntityMapper[Widget]):
    """Converts between :class:`Widget` and its row."""

    def to_domain(self, row: Any) -> Widget:
        """Build a widget from a row."""
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return Widget(
            id=row.id,
            account_id=row.account_id,
            name=row.name,
            created_at=created,
        )

    def to_params(self, entity: Widget) -> dict[str, Any]:
        """Build column values from a widget."""
        return {
            "id": entity.id,
            "account_id": entity.account_id,
            "name": entity.name,
            "created_at": entity.created_at,
        }


def make_widget(index: int, *, account_id: int = 1) -> Widget:
    """Build the nth distinct widget.

    Timestamps advance every third widget, so several share one -- the case a
    pagination tiebreaker exists to handle.
    """
    return Widget(
        id=index,
        account_id=account_id,
        name=f"widget-{index:03d}",
        created_at=SAMPLE_EPOCH + timedelta(minutes=index // 3),
    )


class SqlWidgetRepository(Repository[Widget]):
    """A repository built on the real infrastructure base."""

    __slots__ = ("_account_id", "_mapper", "_paginator")

    def __init__(self, uow: SqlAlchemyUnitOfWork, *, account_id: int = 1) -> None:
        """Bind to a transaction and an account scope."""
        super().__init__(uow)
        self._account_id = account_id
        self._mapper = WidgetMapper()
        self._paginator = KeysetPaginator(
            sort_column=widgets.c.created_at,
            tiebreak_column=widgets.c.id,
            sort_field=SORT_FIELD,
        )

    def _scope(self) -> Any:
        """Return the account-scoped, not-deleted base query."""
        return select(widgets).where(
            widgets.c.account_id == self._account_id,
            widgets.c.deleted_at.is_(None),
        )

    async def add(self, widget: Widget) -> None:
        """Persist a new widget."""
        await self.execute_write(
            insert(widgets).values(self._mapper.to_params(widget)),
            operation="add_widget",
            conflict_message="A widget with that identifier already exists.",
        )

    async def get(self, widget_id: int) -> Widget | None:
        """Return a widget by identifier, or ``None`` if absent or deleted."""
        row = await self.fetch_one(self._scope().where(widgets.c.id == widget_id))
        return self._mapper.to_domain(row) if row is not None else None

    async def page(self, request: PageRequest) -> Page[Widget]:
        """Return one page of widgets."""
        return await self.fetch_page(
            self._scope(),
            paginator=self._paginator,
            request=request,
            mapper=self._mapper.to_domain,
        )

    async def soft_delete(self, widget_id: int) -> None:
        """Mark a widget deleted. Idempotent."""
        await self.execute_write(
            update(widgets)
            .where(widgets.c.id == widget_id, widgets.c.account_id == self._account_id)
            .values(deleted_at=datetime.now(UTC)),
            operation="soft_delete_widget",
        )

    async def count(self) -> int:
        """Return the number of live widgets in scope."""
        total = await self.fetch_scalar(
            select(func.count())
            .select_from(widgets)
            .where(
                widgets.c.account_id == self._account_id,
                widgets.c.deleted_at.is_(None),
            )
        )
        return int(total or 0)


class InMemoryWidgetRepository:
    """The same repository over a dictionary.

    Written independently of the SQL one rather than wrapping it, so the shared
    contract suite genuinely tests it. It is also the template for every
    business fake: sort, tiebreak, exclude deleted, honour the cursor.
    """

    __slots__ = ("_account_id", "_deleted", "_storage")

    def __init__(self, *, account_id: int = 1) -> None:
        """Create an empty repository."""
        self._storage: dict[int, Widget] = {}
        self._deleted: set[int] = set()
        self._account_id = account_id

    async def add(self, widget: Widget) -> None:
        """Persist a new widget."""
        self._storage[widget.id] = widget

    async def get(self, widget_id: int) -> Widget | None:
        """Return a widget by identifier, or ``None`` if absent or deleted."""
        if widget_id in self._deleted:
            return None
        found = self._storage.get(widget_id)
        if found is None or found.account_id != self._account_id:
            return None
        # Returned as a distinct object, matching the no-identity-map contract.
        return replace(found)

    async def page(self, request: PageRequest) -> Page[Widget]:
        """Return one page of widgets."""
        descending = request.sort is None or request.sort.direction.is_descending
        live = [
            widget
            for widget in self._storage.values()
            if widget.id not in self._deleted and widget.account_id == self._account_id
        ]
        live.sort(key=lambda w: (w.created_at, w.id), reverse=descending)

        position = Cursor.decode(request.cursor)
        if position is not None and SORT_KEY in position and TIEBREAK_KEY in position:
            marker = (datetime.fromisoformat(str(position[SORT_KEY])), int(position[TIEBREAK_KEY]))
            live = [
                w
                for w in live
                if ((w.created_at, w.id) < marker if descending else (w.created_at, w.id) > marker)
            ]

        limit = request.effective_limit()
        page_items: Sequence[Widget] = live[:limit]
        has_more = len(live) > limit
        next_cursor = (
            Cursor.encode(
                {
                    SORT_KEY: page_items[-1].created_at.isoformat(),
                    TIEBREAK_KEY: page_items[-1].id,
                }
            )
            if has_more and page_items
            else None
        )
        return Page(items=list(page_items), next_cursor=next_cursor)

    async def soft_delete(self, widget_id: int) -> None:
        """Mark a widget deleted. Idempotent."""
        self._deleted.add(widget_id)

    async def count(self) -> int:
        """Return the number of live widgets in scope."""
        return sum(
            1
            for widget in self._storage.values()
            if widget.id not in self._deleted and widget.account_id == self._account_id
        )
