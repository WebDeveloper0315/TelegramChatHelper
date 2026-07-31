"""AI call mapper and repository.

Stores one row per model invocation, including the failures.

**Append-only.** There is no update and no delete, because an AI call records
what happened at an instant and none of it becomes untrue later. It is also the
record a user consults to find out what their AI spending was, and a record that
can be edited is not one (ADR-057).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, insert, select

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.ai import (
    AiCall,
    AiModel,
    AiOutcome,
    AiVendor,
    Cost,
    DataBoundary,
    FinishReason,
    PromptVersion,
    TokenUsage,
)
from tgassist.domain.model.identifiers import AccountId, AiCallId, ChatId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.ai_call_repository import AiCallRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.mappers import from_stored_datetime
from tgassist.infrastructure.persistence.pagination import KeysetPaginator
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import ai_calls
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

SORT_FIELD = "created_at"


class AiCallMapper(EntityMapper[AiCall]):
    """Converts between :class:`AiCall` and its row.

    The model's *rates* are deliberately not stored and therefore not restored:
    a row records what a call cost, not what it would cost today. A reconstructed
    :class:`AiModel` carries no prices, which means ``cost_of`` on it returns
    ``None`` -- correct, because the stored cost is the answer and recomputing
    one from current rates would silently rewrite history.
    """

    def to_domain(self, row: Any) -> AiCall:
        """Build a call from a row."""
        created_at = from_stored_datetime(_as_iso(row.created_at))
        if created_at is None:  # pragma: no cover - schema forbids
            msg = "An AI call row is missing its timestamp"
            raise DomainValidationError(msg, user_message="That AI call record is incomplete.")

        return AiCall(
            id=AiCallId(row.id),
            account_id=AccountId(row.account_id),
            chat_id=ChatId(row.chat_id) if row.chat_id is not None else None,
            model=AiModel(
                vendor=AiVendor(row.vendor),
                identifier=row.model_identifier,
                data_boundary=DataBoundary(row.data_boundary),
            ),
            prompt=PromptVersion(prompt_id=row.prompt_id, version=row.prompt_version),
            task_kind=row.task_kind,
            usage=TokenUsage(
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
            ),
            cost=(
                Cost(amount=Decimal(row.estimated_cost), currency=row.cost_currency)
                if row.estimated_cost is not None
                else None
            ),
            outcome=AiOutcome(row.outcome),
            finish_reason=(
                FinishReason(row.finish_reason) if row.finish_reason is not None else None
            ),
            latency_ms=int(row.latency_ms),
            response_digest=row.response_digest,
            response_text=row.response_text,
            created_at=created_at,
        )

    def to_params(self, entity: AiCall) -> dict[str, Any]:
        """Build column values from a call.

        The cost is stored as **text**, not as a float: this is money in
        fractions of a cent accumulated over many rows, and binary floating
        point is exactly where that drifts.
        """
        return {
            "id": int(entity.id),
            "account_id": int(entity.account_id),
            "chat_id": int(entity.chat_id) if entity.chat_id is not None else None,
            "vendor": entity.model.vendor.value,
            "model_identifier": entity.model.identifier,
            "data_boundary": entity.model.data_boundary.value,
            "prompt_id": entity.prompt.prompt_id,
            "prompt_version": entity.prompt.version,
            "task_kind": entity.task_kind,
            "input_tokens": entity.usage.input_tokens,
            "output_tokens": entity.usage.output_tokens,
            "estimated_cost": str(entity.cost.amount) if entity.cost is not None else None,
            "cost_currency": entity.cost.currency if entity.cost is not None else None,
            "latency_ms": entity.latency_ms,
            "outcome": entity.outcome.value,
            "finish_reason": (
                entity.finish_reason.value if entity.finish_reason is not None else None
            ),
            "response_digest": entity.response_digest,
            "response_text": entity.response_text,
            "created_at": entity.created_at,
        }


def _as_iso(value: Any) -> str | None:
    """Render a stored timestamp as ISO text, whichever form the driver returned."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class SqlAiCallRepository(Repository[AiCall], AiCallRepository):
    """Stores the model invocations of one account.

    Scoped at construction (ADR-039). Append-only: this class has no ``update``
    and no ``delete``, and the absence of both is the guarantee.
    """

    __slots__ = ("_account_id", "_mapper", "_paginator")

    def __init__(self, uow: SqlAlchemyUnitOfWork, account_id: AccountId) -> None:
        """Bind to a transaction and an account."""
        super().__init__(uow)
        self._account_id = account_id
        self._mapper = AiCallMapper()
        self._paginator = KeysetPaginator(
            sort_column=ai_calls.c.created_at,
            tiebreak_column=ai_calls.c.id,
            sort_field=SORT_FIELD,
        )

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    def _scoped(self) -> Select[Any]:
        """Return the base query, already filtered to this account."""
        return select(ai_calls).where(ai_calls.c.account_id == int(self._account_id))

    async def add(self, call: AiCall) -> None:
        """Persist a record of one call."""
        self._require_own(call, operation="add")
        await self.execute_write(
            insert(ai_calls).values(self._mapper.to_params(call)),
            operation="add_ai_call",
            conflict_message="That AI call has already been recorded.",
        )

    async def get(self, call_id: AiCallId) -> AiCall | None:
        """Return one of this account's calls, or ``None`` if absent."""
        row = await self.fetch_one(
            self._scoped().where(ai_calls.c.id == int(call_id)),
            operation="get_ai_call",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def list_recent(self, request: PageRequest) -> Page[AiCall]:
        """Return one page of this account's calls, newest first."""
        return await self.fetch_page(
            self._scoped(),
            paginator=self._paginator,
            request=request,
            mapper=self._mapper.to_domain,
            operation="list_ai_calls",
        )

    def _require_own(self, call: AiCall, *, operation: str) -> None:
        """Refuse a call belonging to a different account."""
        if call.account_id != self._account_id:
            msg = (
                f"Cannot {operation} an AI call of account {int(call.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That AI call belongs to a different account.",
                context={"scope": int(self._account_id), "call_account": int(call.account_id)},
            )


def ai_call_repository(uow: UnitOfWork, account_id: AccountId) -> SqlAiCallRepository:
    """Build an AI call repository scoped to one account.

    Raises:
        TypeError: If given a unit of work this repository cannot enlist in.
    """
    if not isinstance(uow, SqlAlchemyUnitOfWork):
        msg = f"SqlAiCallRepository requires a SqlAlchemyUnitOfWork, got {type(uow).__name__}"
        raise TypeError(msg)
    return SqlAiCallRepository(uow, account_id)


__all__ = [
    "AiCallMapper",
    "SqlAiCallRepository",
    "ai_call_repository",
]
