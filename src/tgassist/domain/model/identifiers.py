"""Typed identifiers.

Every entity has an integer identifier, and every table carries several of them.
Passing a ``ContactId`` where an ``AccountId`` belongs is trivially easy and
produces a query that runs, returns nothing, and reports no error -- so the
distinction is worth enforcing.

These are ``NewType`` aliases rather than wrapper classes. Both approaches make
the identifiers non-interchangeable; the difference is where and how.

A wrapper class gives runtime separation and can validate itself, at the cost of
construction and unwrapping at every call site -- ``account.id.value`` in every
query, ``AccountId(row.id)`` in every mapper. A ``NewType`` gives the same
separation statically, at zero runtime cost and with no wrapping noise, and
``mypy --strict`` runs over the whole domain and application layer so the
guarantee is actually enforced rather than nominal.

The one thing a ``NewType`` cannot do is validate. Range checks therefore live
in the entities that hold the identifiers, where an invalid value has a meaning
worth reporting ("an account identifier must be positive") rather than being a
bare type error far from its cause.
"""

from __future__ import annotations

from typing import Final, NewType

from tgassist.domain.errors import DomainValidationError

AccountId = NewType("AccountId", int)
"""Local identifier for an :class:`~tgassist.domain.model.account.Account`."""

TelegramUserId = NewType("TelegramUserId", int)
"""Identifier assigned by Telegram. Never used as a local primary key."""

MIN_IDENTIFIER: Final = 1
"""Identifiers are positive. Zero and negatives indicate an unset value."""


def require_positive_identifier(value: int, *, name: str) -> None:
    """Raise if an identifier is not positive.

    Args:
        value: The identifier to check.
        name: Field name, used in the message so the failure names its cause.

    Raises:
        DomainValidationError: If the identifier is below :data:`MIN_IDENTIFIER`.
    """
    if value < MIN_IDENTIFIER:
        msg = f"{name} must be a positive integer, got {value}"
        raise DomainValidationError(msg, user_message="An internal identifier was invalid.")
