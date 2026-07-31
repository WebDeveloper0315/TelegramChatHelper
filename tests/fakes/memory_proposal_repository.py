"""In-memory memory proposal repository.

Written independently of the SQL implementation and sharing one store across
scoped instances, for the reason the other fakes do: a fake holding only its own
account's rows would pass an isolation test by having nothing to leak.

One mutation, and it is a decision. ``decide`` refuses anything but a pending
proposal, exactly as the SQL implementation's ``WHERE`` clause does, and nothing
returns a proposal to pending (ADR-058, ADR-059).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from tests.fakes.pagination import paginate
from tgassist.domain.errors import ConstraintViolationError, DomainValidationError
from tgassist.domain.model.identifiers import AccountId, ConversationId, MemoryProposalId
from tgassist.domain.model.memory import MemoryProposal, ProposalStatus
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.memory_proposal_repository import MemoryProposalRepository


class InMemoryMemoryProposalStore:
    """Shared storage behind the in-memory repositories."""

    __slots__ = ("_accounts", "_calls", "_conversations", "proposals")

    def __init__(
        self,
        known_accounts: set[int] | None = None,
        conversations: dict[int, int] | None = None,
        calls: dict[int, int] | None = None,
    ) -> None:
        """Create a store.

        Args:
            known_accounts: Accounts that exist, standing in for the foreign key
                to ``accounts``. ``None`` accepts any.
            conversations: Conversation identifier to owning account, standing
                in for the composite foreign key. ``None`` accepts any.
            calls: AI call identifier to owning account, for the same reason.
        """
        self.proposals: dict[int, MemoryProposal] = {}
        self._accounts = known_accounts
        self._conversations = conversations
        self._calls = calls

    def account_exists(self, account_id: AccountId) -> bool:
        """Report whether the referenced account exists."""
        if self._accounts is None:
            return True
        return int(account_id) in self._accounts

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

    def register_conversation(self, conversation_id: int, account_id: AccountId) -> None:
        """Record a conversation as existing under an account."""
        if self._conversations is not None:
            self._conversations[conversation_id] = int(account_id)

    def register_call(self, call_id: int, account_id: AccountId) -> None:
        """Record an AI call as existing under an account."""
        if self._calls is not None:
            self._calls[call_id] = int(account_id)

    def delete_conversation(self, conversation_id: int) -> None:
        """Delete a conversation and every proposal extracted from it.

        Mirrors ``ON DELETE CASCADE``: a claim about a conversation that no
        longer exists is residue of it.
        """
        if self._conversations is not None:
            self._conversations.pop(conversation_id, None)
        for key, proposal in list(self.proposals.items()):
            if int(proposal.conversation_id) == conversation_id:
                del self.proposals[key]


class InMemoryMemoryProposalRepository(MemoryProposalRepository):
    """Stores one account's proposals in a shared dictionary."""

    __slots__ = ("_account_id", "_store")

    def __init__(self, store: InMemoryMemoryProposalStore, account_id: AccountId) -> None:
        """Bind to a store and an account scope."""
        self._store = store
        self._account_id = account_id

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def add(self, proposal: MemoryProposal) -> None:
        """Persist one candidate fact."""
        self._require_own(proposal, operation="add")
        if not self._store.account_exists(proposal.account_id):
            msg = f"No account {int(proposal.account_id)} to own this proposal"
            raise ConstraintViolationError(msg, user_message="That account does not exist.")
        if not self._store.conversation_belongs_to(
            int(proposal.conversation_id), proposal.account_id
        ):
            msg = (
                f"No conversation {int(proposal.conversation_id)} in account "
                f"{int(proposal.account_id)}"
            )
            raise ConstraintViolationError(
                msg, user_message="That conversation does not exist in this account."
            )
        if not self._store.call_belongs_to(int(proposal.ai_call_id), proposal.account_id):
            msg = f"No AI call {int(proposal.ai_call_id)} in account {int(proposal.account_id)}"
            raise ConstraintViolationError(
                msg, user_message="That AI call does not exist in this account."
            )
        if int(proposal.id) in self._store.proposals:
            msg = f"Memory proposal {int(proposal.id)} already exists"
            raise ConstraintViolationError(
                msg, user_message="That proposal has already been recorded."
            )
        if any(
            stored.account_id == proposal.account_id
            and stored.conversation_id == proposal.conversation_id
            and stored.category == proposal.category
            and stored.value == proposal.value
            for stored in self._store.proposals.values()
        ):
            msg = (
                f"{proposal.category.value} {proposal.value!r} has already been proposed "
                f"for conversation {int(proposal.conversation_id)}"
            )
            raise ConstraintViolationError(
                msg,
                user_message="That fact has already been proposed for this conversation.",
            )
        self._store.proposals[int(proposal.id)] = proposal

    async def get(self, proposal_id: MemoryProposalId) -> MemoryProposal | None:
        """Return one of this account's proposals, or ``None`` if absent."""
        found = self._store.proposals.get(int(proposal_id))
        if found is None or found.account_id != self._account_id:
            return None
        # A distinct object, matching the no-identity-map contract.
        return replace(found)

    async def list_recent(self, request: PageRequest) -> Page[MemoryProposal]:
        """Return one page of this account's proposals, newest first."""
        return paginate(
            [
                replace(proposal)
                for proposal in self._store.proposals.values()
                if proposal.account_id == self._account_id
            ],
            request,
            sort_key=lambda proposal: (proposal.created_at, int(proposal.id)),
            identity=lambda proposal: int(proposal.id),
        )

    async def decide(
        self, proposal_id: MemoryProposalId, status: ProposalStatus, now: datetime
    ) -> bool:
        """Record a decision about one proposal, once."""
        found = self._store.proposals.get(int(proposal_id))
        if found is None or found.account_id != self._account_id or not found.is_pending:
            return False
        self._store.proposals[int(proposal_id)] = found.decided(status, now)
        return True

    async def list_for_conversation(
        self, conversation_id: ConversationId
    ) -> tuple[MemoryProposal, ...]:
        """Return every proposal already made for one conversation, by identifier."""
        found = [
            replace(proposal)
            for proposal in self._store.proposals.values()
            if proposal.account_id == self._account_id
            and proposal.conversation_id == conversation_id
        ]
        return tuple(sorted(found, key=lambda proposal: int(proposal.id)))

    def _require_own(self, proposal: MemoryProposal, *, operation: str) -> None:
        """Refuse a proposal belonging to a different account."""
        if proposal.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a memory proposal of account "
                f"{int(proposal.account_id)} through a repository scoped to account "
                f"{int(self._account_id)}"
            )
            raise DomainValidationError(
                msg, user_message="That proposal belongs to a different account."
            )
