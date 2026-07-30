"""Where the operator appears in Telegram's own data, and what to do about it.

The operator is a Telegram user like any other, so they turn up inside the data
Telegram returns about their own account. Two appearances matter, and both were
recorded as unenforced until authentication established who the operator is
(``DOMAIN_MODEL.md`` section 5.4, ``TELEGRAM_ARCHITECTURE.md`` section 2.6):

* **A contact.** "A Contact cannot be its own Account's operator identity."
  Contacts are the anchor for memory, goals, relationship profiles and style
  profiles -- all of which describe *the other person*. A contact row holding
  the operator would collect observations about the operator under a model
  built to reason about somebody else, and every feature downstream would treat
  it as a person to prepare replies for.

* **A chat.** Telegram's Saved Messages is a private chat whose counterpart is
  the operator. It is not a conversation with anybody, and the domain already
  has ``ChatType.SAVED`` for it. Recognising it is not an optimisation: every
  Telegram account has one, so synchronisation that did not would try to create
  the forbidden contact on its first run against every real account.

Why here
--------

This is a rule spanning two aggregates -- it needs an Account to state anything
about a Contact -- so neither entity can enforce it alone. A ``Contact`` knows
its ``account_id`` but not the operator's Telegram identifier, and giving it one
would store the same fact on every contact row.

The database cannot enforce it either: SQLite's ``CHECK`` cannot reference
another table, so the alternative would be a trigger, which is a second place
for the rule to live and a second place for it to be wrong.

So it is a domain service: one function, called by every write path that can
create a contact. See ADR-052.
"""

from __future__ import annotations

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.account import Account
from tgassist.domain.model.identifiers import TelegramUserId

#: What to call the operator's own chat when Telegram supplies no title.
#:
#: Telegram normally titles it in the user's own language, and that title is
#: preferred. This exists because a ``saved`` chat still requires a non-empty
#: title, and a blank one would make the chat unrepresentable rather than
#: merely unnamed.
SAVED_MESSAGES_TITLE = "Saved Messages"


def require_not_operator(account: Account, telegram_user_id: TelegramUserId) -> None:
    """Refuse to record the operator as one of their own contacts.

    Args:
        account: The account the contact would belong to.
        telegram_user_id: The Telegram user about to be recorded.

    Raises:
        DomainValidationError: If the user is the account's operator identity.
    """
    if not account.is_operator(telegram_user_id):
        return

    msg = (
        f"Telegram user {int(telegram_user_id)} is account {int(account.id)}'s own "
        f"operator identity and cannot be one of its contacts"
    )
    raise DomainValidationError(
        msg,
        user_message=(
            "That is your own Telegram account. Contacts are the people you talk "
            "to, so you cannot be one of them."
        ),
        context={
            "account_id": int(account.id),
            "telegram_user_id": int(telegram_user_id),
        },
    )


__all__ = [
    "SAVED_MESSAGES_TITLE",
    "require_not_operator",
]
