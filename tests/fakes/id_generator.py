"""Deterministic identifier generator fake."""

from __future__ import annotations

import uuid

from tgassist.domain.ports.id_generator import IdGenerator


class SequentialIdGenerator(IdGenerator):
    """Generates predictable, strictly increasing identifiers.

    Deterministic output makes assertions on generated identifiers readable:
    ``assert message.id == 1`` rather than matching a pattern.
    """

    __slots__ = ("_counter", "_start")

    def __init__(self, start: int = 1) -> None:
        if start < 1:
            msg = "Identifiers must be positive"
            raise ValueError(msg)
        self._start = start
        self._counter = start - 1

    def new_id(self) -> int:
        self._counter += 1
        return self._counter

    def new_uuid(self) -> str:
        self._counter += 1
        # Encode the counter in the low bits so the strings sort in creation
        # order, matching the ordering guarantee the real generator provides.
        return str(uuid.UUID(int=self._counter))

    def new_correlation_id(self) -> str:
        self._counter += 1
        return f"corr-{self._counter:08d}"

    def reset(self) -> None:
        """Restore the generator to its initial state."""
        self._counter = self._start - 1
