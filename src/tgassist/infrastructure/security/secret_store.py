"""Secret store adapters.

Resolution order follows ADR-021: an environment variable overrides the
operating system credential store, which is the durable backend. Environment
variables serve continuous integration and scripted use; the credential store
serves interactive use, where asking a person to maintain environment variables
reliably produces a plaintext file instead.

There is no plaintext fallback. If neither backend is available the store
reports itself unavailable and the caller decides -- which, for the Telegram
session key, means refusing to start (``SECURITY.md`` section 7).
"""

from __future__ import annotations

import asyncio
import os
from typing import Final

import keyring
from keyring.errors import KeyringError

from tgassist.domain.errors import ReadOnlySecretStoreError
from tgassist.domain.model.secret import SecretValue
from tgassist.domain.ports.secret_store import SecretStore
from tgassist.infrastructure.logging import get_logger

SERVICE_NAME: Final = "tgassist"
"""Namespace under which secrets are stored in the credential store."""


class EnvironmentSecretStore(SecretStore):
    """Reads secrets from environment variables. Read-only.

    Writing is refused rather than silently accepted: mutating ``os.environ``
    would appear to succeed while persisting nothing, so a secret the caller
    believed was saved would vanish at exit.
    """

    __slots__ = ()

    async def get(self, name: str) -> SecretValue | None:
        """Return the secret from the environment, or ``None`` if unset."""
        raw = os.environ.get(name)
        return SecretValue(raw) if raw else None

    async def set(self, name: str, value: SecretValue) -> None:
        """Refuse the write; environment variables are not a durable store."""
        del value
        msg = f"Cannot store {name!r}: the environment store is read-only"
        raise ReadOnlySecretStoreError(
            msg,
            user_message="Secrets cannot be saved to environment variables.",
            context={"name": name},
        )

    async def delete(self, name: str) -> None:
        """Refuse the deletion; environment variables are not a durable store."""
        msg = f"Cannot delete {name!r}: the environment store is read-only"
        raise ReadOnlySecretStoreError(
            msg,
            user_message="Secrets cannot be removed from environment variables.",
            context={"name": name},
        )

    async def list_names(self) -> list[str]:
        """Return an empty list.

        The environment holds thousands of unrelated variables and offers no way
        to tell which are meant as secrets for this application. Guessing by
        prefix would report names that are not ours and miss names that are.
        """
        return []

    async def is_available(self) -> bool:
        """Report availability. The environment is always readable."""
        return True


class KeyringSecretStore(SecretStore):
    """Stores secrets in the operating system credential store.

    Uses the Windows Credential Manager (DPAPI), the macOS Keychain, or a
    Secret Service provider on Linux. This is the only backend that is both
    durable and encrypted at rest without the application managing a key.

    The ``keyring`` calls are blocking operating system calls, so they run in a
    worker thread rather than on the event loop (ADR-013).
    """

    __slots__ = ("_log", "_service")

    def __init__(self, service_name: str = SERVICE_NAME) -> None:
        """Create a store.

        Args:
            service_name: Namespace within the credential store. Tests use a
                unique name so they never touch the real user's credentials.
        """
        self._service = service_name
        self._log = get_logger(__name__)

    async def get(self, name: str) -> SecretValue | None:
        """Return the stored secret, or ``None`` if absent or unreadable."""
        try:
            raw = await asyncio.to_thread(keyring.get_password, self._service, name)
        except KeyringError:
            self._log.warning("secret_store_read_failed", name=name, service=self._service)
            return None
        return SecretValue(raw) if raw else None

    async def set(self, name: str, value: SecretValue) -> None:
        """Store the secret, replacing any existing value."""
        await asyncio.to_thread(keyring.set_password, self._service, name, value.reveal())

    async def delete(self, name: str) -> None:
        """Remove the secret. Deleting an absent name is not an error."""
        try:
            await asyncio.to_thread(keyring.delete_password, self._service, name)
        except KeyringError:
            # Backends disagree on whether deleting an absent name raises, so
            # the idempotency the contract promises is enforced here.
            return

    async def list_names(self) -> list[str]:
        """Return an empty list.

        The ``keyring`` API offers no portable enumeration: several backends
        cannot list credentials at all. Returning an empty list is the honest
        answer for a store that cannot be enumerated, and the contract permits
        it. Callers that need an inventory consult the configured secret names
        instead.
        """
        return []

    async def is_available(self) -> bool:
        """Report whether a usable credential backend is present."""
        try:
            backend = await asyncio.to_thread(keyring.get_keyring)
        except KeyringError:
            return False
        name = type(backend).__name__
        # The fail backend is what keyring installs when no real provider is
        # found; it raises on every operation, so it is not availability.
        return "fail" not in name.lower()

    def backend_name(self) -> str:
        """Return the active backend's class name, for diagnostics."""
        try:
            return type(keyring.get_keyring()).__name__
        except KeyringError:  # pragma: no cover - defensive
            return "unavailable"


class ChainedSecretStore(SecretStore):
    """Reads from several stores in order and writes to the first writable one.

    This is the composition ADR-021 specifies: an environment variable overrides
    the credential store for automation, while interactive use gets durable,
    encrypted storage.
    """

    __slots__ = ("_stores",)

    def __init__(self, *stores: SecretStore) -> None:
        """Create a chain.

        Args:
            *stores: Stores in priority order, highest first.
        """
        if not stores:
            msg = "A chained secret store requires at least one backing store"
            raise ValueError(msg)
        self._stores = stores

    async def get(self, name: str) -> SecretValue | None:
        """Return the first value found, searching in priority order."""
        for store in self._stores:
            value = await store.get(name)
            if value is not None:
                return value
        return None

    async def set(self, name: str, value: SecretValue) -> None:
        """Store the value in the first store that accepts writes."""
        last_error: ReadOnlySecretStoreError | None = None
        for store in self._stores:
            try:
                await store.set(name, value)
            except ReadOnlySecretStoreError as exc:
                last_error = exc
                continue
            else:
                return
        raise last_error or ReadOnlySecretStoreError(
            f"No writable secret store is configured for {name!r}",
            user_message="Secrets cannot be saved: no writable credential store is available.",
            context={"name": name},
        )

    async def delete(self, name: str) -> None:
        """Delete from every store that accepts writes.

        Deleting from all of them, rather than the first, prevents a stale value
        in a lower-priority store from resurfacing after the higher-priority one
        is cleared.
        """
        for store in self._stores:
            try:
                await store.delete(name)
            except ReadOnlySecretStoreError:
                continue

    async def list_names(self) -> list[str]:
        """Return the union of names across the chain, in priority order."""
        seen: dict[str, None] = {}
        for store in self._stores:
            for name in await store.list_names():
                seen.setdefault(name, None)
        return list(seen)

    async def is_available(self) -> bool:
        """Report whether any store in the chain is available."""
        for store in self._stores:
            if await store.is_available():
                return True
        return False


def build_default_secret_store() -> ChainedSecretStore:
    """Build the store described by ADR-021: environment over credential store."""
    return ChainedSecretStore(EnvironmentSecretStore(), KeyringSecretStore())
