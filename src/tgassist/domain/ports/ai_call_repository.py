"""AI call repository port.

Scoped to one Account at construction (ADR-039).

**Append-only, expressed in the interface.** There is no ``update`` and no
``delete``. An ``AiCall`` records what happened at an instant -- a request went
out, tokens were spent, money was owed -- and none of that becomes untrue later.
The same arrangement ``MessageRepository`` has (ADR-046), and for a reason that
is arguably stronger here: this is the record a user consults to find out what
their AI spending was, and a record that can be edited is not one.

Retention is the exception, and it is not a delete. ``ai_calls`` is subject to
*log* retention rather than conversation retention (``DOMAIN_MODEL.md`` section
5.25) -- a bulk purge by age, which Milestone 10 owns and which will be a
migration-shaped operation rather than a method here.

Three operations, each traceable to a caller that exists:

* :meth:`add` -- ``ExecuteAiTask``, on every outcome including the failures.
* :meth:`get` -- ``tgassist ai show``.
* :meth:`list_recent` -- ``tgassist ai list``.

There is no ``aggregate_cost``, though a spending report will obviously want
one. It has no caller until that report exists, and the query that serves it
should be measured against the index it needs rather than guessed a milestone
early (``DATABASE.md`` section 20).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tgassist.domain.model.ai import AiCall
from tgassist.domain.model.identifiers import AccountId, AiCallId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest


@runtime_checkable
class AiCallRepository(Protocol):
    """Stores the model invocations of one account.

    Satisfies the repository contract in ``domain/ports/repository.py`` and is
    verified against it by the shared contract suite: absence returns ``None``
    rather than raising, the repository never commits, and results are domain
    objects rather than rows.
    """

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        ...

    async def add(self, call: AiCall) -> None:
        """Persist a record of one call.

        Raises:
            DomainValidationError: If the call belongs to another account.
            ConstraintViolationError: If the identifier is taken, or the chat
                does not belong to this account.
        """
        ...

    async def get(self, call_id: AiCallId) -> AiCall | None:
        """Return one of this account's calls, or ``None`` if absent."""
        ...

    async def list_recent(self, request: PageRequest) -> Page[AiCall]:
        """Return one page of this account's calls, newest first.

        Ordered by when the call was made. Unlike messages, that is also the
        order they were written in -- nothing back-fills an AI call -- but it is
        stated as the sort key rather than left to insertion order, because a
        listing that depended on insertion order would be right by luck.
        """
        ...
