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
"""Identifier assigned by Telegram. Never used as a local primary key.

The same Telegram user can be known to more than one Account, so this value is
not unique in any table that holds it -- only ``(account_id, telegram_user_id)``
is. Using it as a key would therefore force every child table into a composite
foreign key, and would hand a foreign system control of our identifiers
(ADR-041).
"""

ContactId = NewType("ContactId", int)
"""Local identifier for a :class:`~tgassist.domain.model.contact.Contact`."""

ChatId = NewType("ChatId", int)
"""Local identifier for a :class:`~tgassist.domain.model.chat.Chat`."""

MessageId = NewType("MessageId", int)
"""Local identifier for a :class:`~tgassist.domain.model.message.Message`."""

TelegramMessageId = NewType("TelegramMessageId", int)
"""Identifier assigned by Telegram to a message, unique only within its chat.

Optional on a Message: ingestion is source-agnostic, and a message written at
the keyboard or restored from an export never had one (ADR-045). Its presence is
what makes a message re-ingestable without duplication; its absence means there
is nothing to deduplicate against, which is correct rather than a gap.
"""

TelegramChatId = NewType("TelegramChatId", int)
"""Identifier assigned by Telegram to a chat.

Distinct from :data:`TelegramUserId` because the two are not interchangeable
even though both are integers: for a private chat they happen to coincide, and
for everything else they do not.

**Negative values are legitimate.** Telegram numbers groups and channels below
zero, so the range check that suits a user identifier -- must be positive -- is
wrong here. The only structural rule is that it is not zero.
"""

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


def require_nonzero_chat_identifier(value: TelegramChatId) -> None:
    """Raise if a Telegram chat identifier is zero.

    Separate from :func:`require_positive_identifier` because the rule genuinely
    differs, as the note on :data:`TelegramChatId` says: Telegram numbers groups
    and channels below zero, so requiring a positive value would refuse every
    group and channel a real account has. One function, so the two places that
    hold a chat identifier -- the entity and the gateway's view of one -- cannot
    disagree about it.

    Raises:
        DomainValidationError: If the identifier is zero.
    """
    if value == 0:
        msg = "A Telegram chat identifier cannot be zero"
        raise DomainValidationError(msg, user_message="That is not a valid chat identifier.")
