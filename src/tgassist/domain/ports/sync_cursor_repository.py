"""Sync cursor repository port.

Scoped to one Account at construction (ADR-039), like every repository over
account-owned data.

Four operations, each traceable to a caller that exists:

* :meth:`get` -- the read at the start of a backfill, and before every batch.
* :meth:`add` -- the first run against a chat, and ``--reset``.
* :meth:`update` -- **the write that makes resumability work**. Called inside
  the transaction that stored the batch it accounts for (ADR-050).
* :meth:`save` -- add-or-update in one call, because every caller genuinely does
  not care which it is: a cursor exists for a chat or it does not, and the
  distinction is the repository's to know.

There is no ``delete``. A cursor goes with its chat, by cascade. Resetting is
:meth:`save` with a cursor built by ``SyncCursor.start`` -- which is the same
write, and expressing it as a deletion would leave a window in which a chat had
messages and no bookmark.

There is no ``list_pending`` either, though a scheduler will obviously want one.
It has no caller until the scheduler exists, and the index that serves it should
be chosen by the query that needs it (``DATABASE.md`` section 20) rather than
guessed a milestone early.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tgassist.domain.model.identifiers import AccountId, ChatId
from tgassist.domain.model.sync_cursor import SyncCursor


@runtime_checkable
class SyncCursorRepository(Protocol):
    """Stores the synchronisation bookmarks of one account's chats.

    Satisfies the repository contract in ``domain/ports/repository.py`` and is
    verified against it by the shared contract suite: absence returns ``None``
    rather than raising, the repository never commits, and results are domain
    objects rather than rows.

    **This repository never commits.** That matters more here than anywhere
    else: the guarantee the cursor provides is that it moves in the same
    transaction as the messages it accounts for, and a repository that committed
    on its own would break exactly that.
    """

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        ...

    async def get(self, chat_id: ChatId) -> SyncCursor | None:
        """Return one chat's cursor, or ``None`` if it has never been synced."""
        ...

    async def add(self, cursor: SyncCursor) -> None:
        """Persist a new cursor.

        Raises:
            DomainValidationError: If the cursor belongs to another account.
            ConstraintViolationError: If the chat already has a cursor, or if
                the chat does not belong to this account.
        """
        ...

    async def update(self, cursor: SyncCursor) -> None:
        """Persist an advanced cursor.

        Takes the whole entity rather than a set of fields, so the invariants
        checked when it was constructed are the invariants written.

        Raises:
            DomainValidationError: If the cursor belongs to another account.
            RecordNotFoundError: If the chat has no cursor. An update that
                matched nothing would mean a batch had been accounted for
                against a bookmark that does not exist.
        """
        ...

    async def save(self, cursor: SyncCursor) -> None:
        """Persist a cursor, whether or not the chat already had one.

        The operation every caller actually performs. A backfill does not know
        or care whether this chat has been synchronised before -- it knows where
        it has got to, and that is what it writes.
        """
        ...
