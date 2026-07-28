"""The repository contract.

This module defines what every repository must do, and deliberately does **not**
define a generic ``Repository[T, ID]`` interface that they all inherit. The
reasoning is set out below, because omitting a near-universal enterprise
abstraction deserves an argument rather than a shrug.

Why no generic CRUD base
------------------------

The aggregates in this system do not share a lifecycle:

* ``Message`` is append-only and arrives in bulk. It is never updated except to
  record a remote edit, and never deleted except by retention.
* ``AuditEvent`` is append-only with **no** update or delete path at all. An
  architectural test asserts that ``AuditRepository`` exposes no mutation
  method, because an audit trail that can be rewritten is not an audit trail.
* ``RelationshipProfile`` is a computed singleton per contact, upserted whole.
  It is never partially updated, and "create" and "update" are the same
  operation.
* ``Memory`` cannot be updated at all in the ordinary sense: a value change
  must create a ``MemoryRevision`` in the same transaction, so the write takes
  two arguments where a generic ``update(entity)`` takes one.

A shared ``Repository`` with ``create``, ``update``, ``delete``, ``find_by_id``
would therefore be either a lie -- ``AuditRepository.delete`` existing and
raising -- or the intersection of those lifecycles, which is nearly empty. Both
outcomes are worse than no base at all, and the first would break a guarantee
the project currently enforces mechanically.

The second reason is query shape. A generic base invites ``find(**criteria)``,
and a repository whose interface is "any query you like" is a database
connection with extra steps. Named methods (``list_recent_by_chat``) can each be
matched to an index and measured; an open query surface cannot.

What *is* shared is the **mechanics**: transaction-aware execution, keyset
pagination, row mapping and error normalisation. Those live in
``infrastructure.persistence.repository`` as a base class, which is inheritance
for code reuse rather than for polymorphism -- no caller ever holds a
``Repository`` and asks it to do something generic.

What every repository guarantees
--------------------------------

These are obligations, verified by the shared contract suite in
``tests/support/repository_contract.py`` rather than merely described here.

1. **Account scoping.** Every method takes an account scope or an entity that
   carries one. There is no unscoped query path, so cross-account leakage is
   impossible rather than merely unlikely.
2. **Domain objects only.** Parameters and returns are domain objects. A row, a
   SQLAlchemy construct or a driver type never crosses the boundary.
3. **Absence is not an error.** A lookup returns ``None`` when nothing matches.
   Only a method named to promise a result raises ``RecordNotFoundError``.
4. **Soft-deleted rows are excluded** unless explicitly requested.
5. **Typed errors.** Driver failures arrive as ``PersistenceError`` subclasses.
6. **No transaction control.** A repository never begins, commits or rolls
   back. It enlists in the unit of work that owns it, which is what allows a use
   case to compose several repositories atomically.
7. **Keyset pagination.** Collection queries take a ``PageRequest`` and return a
   ``Page``. No numeric offsets.
8. **No business logic.** No derived values, no validation beyond what the
   schema enforces, no clock reads. A repository that decides something is a
   domain service in disguise.

Identity
--------

There is no identity map. Reading the same row twice produces two equal objects,
not the same object. Entities are therefore compared **by identifier**, never by
reference, and a caller holding a stale copy is holding a snapshot rather than a
live view. This is a consequence of using SQLAlchemy Core rather than the ORM
(ADR-015) and is a feature: an entity that cannot silently change underneath its
holder is far easier to reason about.

Loading
-------

Everything is eager. There are no lazy proxies, no session-attached state and no
relationship traversal, so an accidental N+1 query is not expressible. A use
case needing related data asks for it explicitly, which makes the second query
visible at the call site where its cost can be seen.

Lifetime
--------

A repository is scoped to one unit of work and must not outlive it. Repositories
are created per transaction and discarded with it; storing one on a long-lived
object would keep a closed transaction alive and is a defect.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from tgassist.domain.ports.unit_of_work import UnitOfWork

R_co = TypeVar("R_co", covariant=True)

RepositoryFactory = Callable[[UnitOfWork], R_co]
"""Builds a repository bound to a unit of work.

A use case declares the repositories it needs as constructor parameters of this
shape, and creates them inside its transaction::

    class IngestMessage:
        def __init__(
            self,
            unit_of_work: UnitOfWorkFactory,
            messages: RepositoryFactory[MessageRepository],
            chats: RepositoryFactory[ChatRepository],
        ) -> None: ...

        async def execute(self, ...) -> None:
            async with self._unit_of_work() as uow:
                messages = self._messages(uow)
                chats = self._chats(uow)
                ...
                await uow.commit()

This is a deliberate alternative to two more common arrangements.

Hanging repositories off the unit of work (``uow.messages``) reads well but
requires the unit of work to know every repository in the system, which makes an
interface that should describe a transaction into a catalogue of storage. It
also tells a reader nothing about what a given use case touches.

Passing the container and asking it for repositories is a service locator: the
use case's real dependencies disappear from its signature, which is exactly the
information a reader and a test need most.

Declaring factories keeps the dependency list honest -- a use case that needs
four repositories says so -- while still allowing the repository to be created
inside the transaction it belongs to.
"""
