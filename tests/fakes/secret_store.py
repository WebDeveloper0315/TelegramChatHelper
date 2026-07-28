"""Secret store fakes."""

from __future__ import annotations

from tgassist.domain.model.secret import SecretValue
from tgassist.domain.ports.secret_store import SecretStore


class InMemorySecretStore(SecretStore):
    """Holds secrets in a dictionary for the lifetime of the test.

    Behaviourally complete: it honours the same idempotency, overwrite and
    absent-name semantics as the real store, so the contract suite is a genuine
    check rather than a formality.
    """

    __slots__ = ("_available", "_values")

    def __init__(self, *, available: bool = True) -> None:
        self._values: dict[str, str] = {}
        self._available = available

    async def get(self, name: str) -> SecretValue | None:
        raw = self._values.get(name)
        return SecretValue(raw) if raw else None

    async def set(self, name: str, value: SecretValue) -> None:
        self._values[name] = value.reveal()

    async def delete(self, name: str) -> None:
        self._values.pop(name, None)

    async def list_names(self) -> list[str]:
        return sorted(self._values)

    async def is_available(self) -> bool:
        return self._available


class UnavailableSecretStore(SecretStore):
    """A store that reports itself unavailable and holds nothing.

    Models a machine with no credential backend, so the fail-closed behaviour of
    ``Container.verify_secret_store`` can be exercised.
    """

    __slots__ = ()

    async def get(self, name: str) -> SecretValue | None:
        return None

    async def set(self, name: str, value: SecretValue) -> None:
        return None

    async def delete(self, name: str) -> None:
        return None

    async def list_names(self) -> list[str]:
        return []

    async def is_available(self) -> bool:
        return False
