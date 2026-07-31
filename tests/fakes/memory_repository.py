"""In-memory memory repository.

Written independently of the SQL implementation and sharing one store across
scoped instances, for the reason the other fakes do: a fake holding only its own
account's rows would pass an isolation test by having nothing to leak.

The three unique indexes are reimplemented here rather than approximated. They
are the whole of "acceptance creates exactly one memory" and "a fact is stored
once", and a fake that let a duplicate through would make a contract test pass
against a guarantee the real repository has and this one does not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from tests.fakes.pagination import paginate
from tgassist.domain.errors import ConstraintViolationError, DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    ContactId,
    MemoryId,
    MemoryProposalId,
)
from tgassist.domain.model.memory import Memory
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.memory_repository import MemoryRepository


class InMemoryMemoryStore:
    """Shared storage behind the in-memory repositories."""

    __slots__ = ("_accounts", "_contacts", "memories")

    def __init__(
        self,
        known_accounts: set[int] | None = None,
        contacts: dict[int, int] | None = None,
    ) -> None:
        """Create a store.

        Args:
            known_accounts: Accounts that exist, standing in for the foreign key
                to ``accounts``. ``None`` accepts any.
            contacts: Contact identifier to owning account, standing in for the
                composite foreign key to ``contacts``. ``None`` accepts any.
        """
        self.memories: dict[int, Memory] = {}
        self._accounts = known_accounts
        self._contacts = contacts

    def account_exists(self, account_id: AccountId) -> bool:
        """Report whether the referenced account exists."""
        if self._accounts is None:
            return True
        return int(account_id) in self._accounts

    def contact_belongs_to(self, contact_id: int, account_id: AccountId) -> bool:
        """Report whether a contact exists **and** belongs to this account."""
        if self._contacts is None:
            return True
        return self._contacts.get(contact_id) == int(account_id)

    def delete_contact(self, contact_id: int) -> None:
        """Delete a contact and every memory about them.

        Mirrors ``ON DELETE CASCADE``: purging a contact removes everything
        about them (``PRIVACY.md`` section 7).
        """
        if self._contacts is not None:
            self._contacts.pop(contact_id, None)
        for key, memory in list(self.memories.items()):
            if memory.contact_id is not None and int(memory.contact_id) == contact_id:
                del self.memories[key]

    def forget_provenance(self, *, proposal_id: int | None = None) -> None:
        """Clear a memory's link to a deleted proposal.

        Mirrors ``ON DELETE SET NULL``: a memory is user-approved knowledge and
        does not stop being known because the thing it came from was deleted.
        """
        for key, memory in list(self.memories.items()):
            if proposal_id is not None and memory.proposal_id == proposal_id:
                self.memories[key] = replace(memory, proposal_id=None)


class InMemoryMemoryRepository(MemoryRepository):
    """Stores one account's memories in a shared dictionary."""

    __slots__ = ("_account_id", "_store")

    def __init__(self, store: InMemoryMemoryStore, account_id: AccountId) -> None:
        """Bind to a store and an account scope."""
        self._store = store
        self._account_id = account_id

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def add(self, memory: Memory) -> None:
        """Persist one approved fact."""
        self._require_own(memory, operation="add")
        if not self._store.account_exists(memory.account_id):
            msg = f"No account {int(memory.account_id)} to own this memory"
            raise ConstraintViolationError(msg, user_message="That account does not exist.")
        if memory.contact_id is not None and not self._store.contact_belongs_to(
            int(memory.contact_id), memory.account_id
        ):
            msg = f"No contact {int(memory.contact_id)} in account {int(memory.account_id)}"
            raise ConstraintViolationError(
                msg, user_message="That contact does not exist in this account."
            )
        if int(memory.id) in self._store.memories:
            msg = f"Memory {int(memory.id)} already exists"
            raise ConstraintViolationError(
                msg, user_message="That memory has already been recorded."
            )
        if memory.proposal_id is not None and any(
            stored.proposal_id == memory.proposal_id for stored in self._store.memories.values()
        ):
            msg = f"Proposal {int(memory.proposal_id)} has already produced a memory"
            raise ConstraintViolationError(
                msg, user_message="That proposal has already been accepted."
            )
        if any(
            stored.is_active
            and stored.account_id == memory.account_id
            and stored.contact_id == memory.contact_id
            and stored.category == memory.category
            and stored.key == memory.key
            for stored in self._store.memories.values()
        ):
            msg = f"{memory.category.value} {memory.key} is already remembered"
            raise ConstraintViolationError(msg, user_message="That fact is already remembered.")
        self._store.memories[int(memory.id)] = memory

    async def get(self, memory_id: MemoryId) -> Memory | None:
        """Return one of this account's memories, deleted or not."""
        found = self._store.memories.get(int(memory_id))
        if found is None or found.account_id != self._account_id:
            return None
        # A distinct object, matching the no-identity-map contract.
        return replace(found)

    async def get_by_proposal(self, proposal_id: MemoryProposalId) -> Memory | None:
        """Return the memory an accepted proposal produced, if it still exists."""
        for memory in self._store.memories.values():
            if memory.account_id == self._account_id and memory.proposal_id == proposal_id:
                return replace(memory)
        return None

    async def list_active(self, request: PageRequest) -> Page[Memory]:
        """Return one page of this account's live memories, newest first."""
        return paginate(
            [
                replace(memory)
                for memory in self._store.memories.values()
                if memory.account_id == self._account_id and memory.is_active
            ],
            request,
            sort_key=lambda memory: (memory.created_at, int(memory.id)),
            identity=lambda memory: int(memory.id),
        )

    async def list_for_contact(
        self, contact_id: ContactId | None, *, limit: int
    ) -> tuple[Memory, ...]:
        """Return the live memories about one contact, newest first.

        The contactless case is a genuine equality against ``None`` here, where
        SQL needs ``IS NULL`` -- the fake and the real implementation reach the
        same answer by different means, which is what the contract suite is for.
        """
        found = [
            replace(memory)
            for memory in self._store.memories.values()
            if memory.account_id == self._account_id
            and memory.is_active
            and memory.contact_id == contact_id
        ]
        found.sort(key=lambda memory: (memory.created_at, int(memory.id)), reverse=True)
        return tuple(found[:limit])

    async def mark_retrieved(self, memory_ids: Sequence[MemoryId], now: datetime) -> int:
        """Record that these memories were selected into a context."""
        marked = 0
        for memory_id in memory_ids:
            found = self._store.memories.get(int(memory_id))
            if found is None or found.account_id != self._account_id or not found.is_active:
                continue
            self._store.memories[int(memory_id)] = replace(
                found,
                retrieval_count=found.retrieval_count + 1,
                last_retrieved_at=now,
            )
            marked += 1
        return marked

    async def delete(self, memory_id: MemoryId, now: datetime) -> bool:
        """Forget one memory, softly."""
        found = self._store.memories.get(int(memory_id))
        if found is None or found.account_id != self._account_id or not found.is_active:
            return False
        self._store.memories[int(memory_id)] = replace(found, deleted_at=now)
        return True

    def _require_own(self, memory: Memory, *, operation: str) -> None:
        """Refuse a memory belonging to a different account."""
        if memory.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a memory of account {int(memory.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg, user_message="That memory belongs to a different account."
            )
