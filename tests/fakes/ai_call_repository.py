"""In-memory AI call repository.

Written independently of the SQL implementation and sharing one store across
scoped instances, for the reason the other fakes do: a fake holding only its own
account's rows would pass an isolation test by having nothing to leak.

Append-only, like the thing it stands in for: no update, no delete, and the
absence of both is the guarantee (ADR-057).
"""

from __future__ import annotations

from dataclasses import replace

from tests.fakes.pagination import paginate
from tgassist.domain.errors import ConstraintViolationError, DomainValidationError
from tgassist.domain.model.ai import AiCall
from tgassist.domain.model.identifiers import AccountId, AiCallId, ChatId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.ai_call_repository import AiCallRepository


class InMemoryAiCallStore:
    """Shared storage behind the in-memory repositories."""

    __slots__ = ("_accounts", "_chats", "calls")

    def __init__(
        self,
        known_accounts: set[int] | None = None,
        chats: dict[int, int] | None = None,
    ) -> None:
        """Create a store.

        Args:
            known_accounts: Accounts that exist, standing in for the foreign key
                to ``accounts``. ``None`` accepts any.
            chats: Chat identifier to owning account, standing in for the
                composite foreign key to ``chats``. ``None`` accepts any.
        """
        self.calls: dict[int, AiCall] = {}
        self._accounts = known_accounts
        self._chats = chats

    def account_exists(self, account_id: AccountId) -> bool:
        """Report whether the referenced account exists."""
        if self._accounts is None:
            return True
        return int(account_id) in self._accounts

    def chat_belongs_to(self, chat_id: ChatId, account_id: AccountId) -> bool:
        """Report whether a chat exists **and** belongs to this account."""
        if self._chats is None:
            return True
        return self._chats.get(int(chat_id)) == int(account_id)

    def register_chat(self, chat_id: ChatId, account_id: AccountId) -> None:
        """Record a chat as existing under an account."""
        if self._chats is not None:
            self._chats[int(chat_id)] = int(account_id)

    def delete_chat(self, chat_id: ChatId) -> None:
        """Delete a chat and every call that was about it.

        Mirrors ``ON DELETE CASCADE``: a record derived from a deleted chat is
        residue of that chat.
        """
        if self._chats is not None:
            self._chats.pop(int(chat_id), None)
        for call_id, call in list(self.calls.items()):
            if call.chat_id == chat_id:
                del self.calls[call_id]


def _as_stored(call: AiCall) -> AiCall:
    """Return the call as a repository can give it back.

    The price list a cost was computed from is not persisted -- the cost itself
    is, so the rates have no reader -- and a fake that kept them would let the
    application depend on something no real repository returns.
    """
    return replace(
        call,
        model=replace(call.model, input_cost_per_million=None, output_cost_per_million=None),
    )


class InMemoryAiCallRepository(AiCallRepository):
    """Stores one account's AI calls in a shared dictionary."""

    __slots__ = ("_account_id", "_store")

    def __init__(self, store: InMemoryAiCallStore, account_id: AccountId) -> None:
        """Bind to a store and an account scope."""
        self._store = store
        self._account_id = account_id

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def add(self, call: AiCall) -> None:
        """Persist a record of one call."""
        self._require_own(call, operation="add")
        if not self._store.account_exists(call.account_id):
            msg = f"No account {int(call.account_id)} to own this call"
            raise ConstraintViolationError(msg, user_message="That account does not exist.")
        if call.chat_id is not None and not self._store.chat_belongs_to(
            call.chat_id, call.account_id
        ):
            msg = f"No chat {int(call.chat_id)} in account {int(call.account_id)}"
            raise ConstraintViolationError(
                msg, user_message="That chat does not exist in this account."
            )
        if int(call.id) in self._store.calls:
            msg = f"AI call {int(call.id)} has already been recorded"
            raise ConstraintViolationError(
                msg, user_message="That AI call has already been recorded."
            )
        self._store.calls[int(call.id)] = _as_stored(call)

    async def get(self, call_id: AiCallId) -> AiCall | None:
        """Return one of this account's calls, or ``None`` if absent."""
        found = self._store.calls.get(int(call_id))
        if found is None or found.account_id != self._account_id:
            return None
        # A distinct object, matching the no-identity-map contract.
        return replace(found)

    async def list_recent(self, request: PageRequest) -> Page[AiCall]:
        """Return one page of this account's calls, newest first."""
        return paginate(
            [
                replace(call)
                for call in self._store.calls.values()
                if call.account_id == self._account_id
            ],
            request,
            sort_key=lambda call: (call.created_at, int(call.id)),
            identity=lambda call: int(call.id),
        )

    def _require_own(self, call: AiCall, *, operation: str) -> None:
        """Refuse a call belonging to a different account."""
        if call.account_id != self._account_id:
            msg = (
                f"Cannot {operation} an AI call of account {int(call.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg, user_message="That AI call belongs to a different account."
            )
