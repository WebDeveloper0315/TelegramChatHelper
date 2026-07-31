"""In-memory suggestion repository.

Written independently of the SQL implementation and sharing one store across
scoped instances, for the reason the other fakes do: a fake holding only its own
account's rows would pass an isolation test by having nothing to leak.

One mutation, and it is a decision. ``decide`` refuses anything but a pending
suggestion, exactly as the SQL implementation's ``WHERE`` clause does, and
nothing returns a suggestion to pending (ADR-062).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from tests.fakes.pagination import paginate
from tgassist.domain.errors import ConstraintViolationError, DomainValidationError
from tgassist.domain.model.identifiers import AccountId, ChatId, SuggestionId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.suggestion import Suggestion, SuggestionStatus
from tgassist.domain.ports.suggestion_repository import SuggestionRepository


class InMemorySuggestionStore:
    """Shared storage behind the in-memory repositories."""

    __slots__ = ("_accounts", "_calls", "_chats", "_conversations", "suggestions")

    def __init__(
        self,
        known_accounts: set[int] | None = None,
        chats: dict[int, int] | None = None,
        conversations: dict[int, int] | None = None,
        calls: dict[int, int] | None = None,
    ) -> None:
        """Create a store.

        Args:
            known_accounts: Accounts that exist, standing in for the foreign key
                to ``accounts``. ``None`` accepts any.
            chats: Chat identifier to owning account, standing in for the
                composite foreign key. ``None`` accepts any.
            conversations: Conversation identifier to owning account, likewise.
            calls: AI call identifier to owning account, likewise.
        """
        self.suggestions: dict[int, Suggestion] = {}
        self._accounts = known_accounts
        self._chats = chats
        self._conversations = conversations
        self._calls = calls

    def account_exists(self, account_id: AccountId) -> bool:
        """Report whether the referenced account exists."""
        if self._accounts is None:
            return True
        return int(account_id) in self._accounts

    def chat_belongs_to(self, chat_id: int, account_id: AccountId) -> bool:
        """Report whether a chat exists **and** belongs to this account."""
        if self._chats is None:
            return True
        return self._chats.get(chat_id) == int(account_id)

    def conversation_belongs_to(self, conversation_id: int, account_id: AccountId) -> bool:
        """Report whether a conversation exists **and** belongs to this account."""
        if self._conversations is None:
            return True
        return self._conversations.get(conversation_id) == int(account_id)

    def call_belongs_to(self, call_id: int, account_id: AccountId) -> bool:
        """Report whether an AI call exists **and** belongs to this account."""
        if self._calls is None:
            return True
        return self._calls.get(call_id) == int(account_id)

    def delete_chat(self, chat_id: int) -> None:
        """Delete a chat and every suggestion about it.

        Mirrors ``ON DELETE CASCADE``: a draft about a conversation that no
        longer exists means nothing.
        """
        if self._chats is not None:
            self._chats.pop(chat_id, None)
        for key, suggestion in list(self.suggestions.items()):
            if int(suggestion.chat_id) == chat_id:
                del self.suggestions[key]


class InMemorySuggestionRepository(SuggestionRepository):
    """Stores one account's suggestions in a shared dictionary."""

    __slots__ = ("_account_id", "_store")

    def __init__(self, store: InMemorySuggestionStore, account_id: AccountId) -> None:
        """Bind to a store and an account scope."""
        self._store = store
        self._account_id = account_id

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def add(self, suggestion: Suggestion) -> None:
        """Persist one draft."""
        self._require_own(suggestion, operation="add")
        if not self._store.account_exists(suggestion.account_id):
            msg = f"No account {int(suggestion.account_id)} to own this suggestion"
            raise ConstraintViolationError(msg, user_message="That account does not exist.")
        if not self._store.chat_belongs_to(int(suggestion.chat_id), suggestion.account_id):
            msg = f"No chat {int(suggestion.chat_id)} in account {int(suggestion.account_id)}"
            raise ConstraintViolationError(
                msg, user_message="That chat does not exist in this account."
            )
        if suggestion.conversation_id is not None and not self._store.conversation_belongs_to(
            int(suggestion.conversation_id), suggestion.account_id
        ):
            msg = (
                f"No conversation {int(suggestion.conversation_id)} in account "
                f"{int(suggestion.account_id)}"
            )
            raise ConstraintViolationError(
                msg, user_message="That conversation does not exist in this account."
            )
        if not self._store.call_belongs_to(int(suggestion.ai_call_id), suggestion.account_id):
            msg = f"No AI call {int(suggestion.ai_call_id)} in account {int(suggestion.account_id)}"
            raise ConstraintViolationError(
                msg, user_message="That AI call does not exist in this account."
            )
        if int(suggestion.id) in self._store.suggestions:
            msg = f"Suggestion {int(suggestion.id)} already exists"
            raise ConstraintViolationError(
                msg, user_message="That suggestion has already been recorded."
            )
        self._store.suggestions[int(suggestion.id)] = suggestion

    async def get(self, suggestion_id: SuggestionId) -> Suggestion | None:
        """Return one of this account's suggestions, decided or not."""
        found = self._store.suggestions.get(int(suggestion_id))
        if found is None or found.account_id != self._account_id:
            return None
        # A distinct object, matching the no-identity-map contract.
        return replace(found)

    async def list_pending(self, request: PageRequest) -> Page[Suggestion]:
        """Return one page of this account's undecided suggestions, newest first."""
        return self._page(
            [
                suggestion
                for suggestion in self._store.suggestions.values()
                if suggestion.account_id == self._account_id and suggestion.is_pending
            ],
            request,
        )

    async def list_by_chat(self, chat_id: ChatId, request: PageRequest) -> Page[Suggestion]:
        """Return one page of one chat's suggestions, decided or not, newest first."""
        return self._page(
            [
                suggestion
                for suggestion in self._store.suggestions.values()
                if suggestion.account_id == self._account_id and suggestion.chat_id == chat_id
            ],
            request,
        )

    async def decide(
        self, suggestion_id: SuggestionId, status: SuggestionStatus, now: datetime
    ) -> bool:
        """Record a decision about one suggestion, once."""
        found = self._store.suggestions.get(int(suggestion_id))
        if found is None or found.account_id != self._account_id or not found.is_pending:
            return False
        self._store.suggestions[int(suggestion_id)] = found.decided(status, now)
        return True

    def _page(self, found: list[Suggestion], request: PageRequest) -> Page[Suggestion]:
        """Paginate newest first, with the identifier as the tiebreaker."""
        return paginate(
            [replace(suggestion) for suggestion in found],
            request,
            sort_key=lambda suggestion: (suggestion.created_at, int(suggestion.id)),
            identity=lambda suggestion: int(suggestion.id),
        )

    def _require_own(self, suggestion: Suggestion, *, operation: str) -> None:
        """Refuse a suggestion belonging to a different account."""
        if suggestion.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a suggestion of account "
                f"{int(suggestion.account_id)} through a repository scoped to account "
                f"{int(self._account_id)}"
            )
            raise DomainValidationError(
                msg, user_message="That suggestion belongs to a different account."
            )
