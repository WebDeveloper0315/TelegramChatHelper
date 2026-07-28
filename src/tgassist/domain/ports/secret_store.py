"""Secret storage port.

The only component permitted to hold credential material. Everything else refers
to a secret by *name* -- configuration records ``api_key_ref: ANTHROPIC_API_KEY``,
never the key itself (ADR-021).

Names are not sensitive and may appear in configuration, logs and the database.
Values are, and appear in none of them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tgassist.domain.model.secret import SecretValue


@runtime_checkable
class SecretStore(Protocol):
    """Stores and retrieves secrets.

    Contract, guaranteed by every implementation and verified by the shared
    contract test suite:

    1. :meth:`get` returns ``None`` for an unknown name rather than raising.
       "Not configured" is an ordinary state, not an error.
    2. :meth:`get` returns a :class:`SecretValue`, never a bare string, so the
       value stays masked on every incidental rendering path.
    3. :meth:`set` overwrites an existing value for the same name.
    4. :meth:`delete` on an unknown name succeeds silently; deletion is
       idempotent so that cleanup paths need no existence check.
    5. :meth:`list_names` returns names only. An implementation that cannot
       enumerate returns an empty list rather than raising.
    6. :meth:`is_available` reports whether the backend can be used at all. It
       never raises, because it is the check callers make *before* deciding how
       to handle an unavailable backend.
    7. A read-only implementation raises :class:`ReadOnlySecretStoreError` from
       :meth:`set` and :meth:`delete`. It does not silently discard the write.
    8. No implementation writes a secret value to a log, a database, a backup or
       any file it does not itself encrypt.
    """

    async def get(self, name: str) -> SecretValue | None:
        """Return the secret stored under ``name``, or ``None`` if absent."""
        ...

    async def set(self, name: str, value: SecretValue) -> None:
        """Store ``value`` under ``name``, replacing any existing value."""
        ...

    async def delete(self, name: str) -> None:
        """Remove the secret stored under ``name``. Idempotent."""
        ...

    async def list_names(self) -> list[str]:
        """Return the names of stored secrets, never their values."""
        ...

    async def is_available(self) -> bool:
        """Report whether this backend can be used in the current environment."""
        ...
