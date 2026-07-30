"""Turning Telegram's view of an account into this application's own.

The first code that reads Telegram and writes the database, and therefore the
first place the two models have to be reconciled. Two use cases, because there
are two genuinely different sources:

* :class:`SyncChats` reads the chat list. Every chat this account has, including
  ones with people it never saved.
* :class:`SyncContacts` reads the address book. Every person this account saved,
  including ones it has never messaged.

Neither set contains the other, which is why neither can be derived from the
other and why there are two.

What synchronisation may and may not do
---------------------------------------

Recorded as ADR-053, and worth stating here because every method below obeys it:

1. **It is additive.** Nothing is deleted. A chat that has disappeared from
   Telegram is still the operator's history, and Telegram's opinion of what
   exists is not a mandate to forget.
2. **It never overwrites a decision the operator made.** ``sync_enabled`` and
   ``ai_processing_mode`` are chosen when a chat is first discovered and never
   touched again; a contact the operator deleted stays deleted. A run that
   silently re-enabled AI processing for a chat somebody had disabled it on
   would be a privacy defect, not a bug.
3. **It writes only what Telegram owns**: display names, handles and titles.
4. **A repeat run over unchanged data writes nothing**, so ``updated_at`` means
   "when this last changed" and not "when we last looked".

Transactions
------------

One per chat, and one per contact -- not one per run. The pair
``(Contact, Chat)`` is the atomic unit for a private chat, because ADR-043's
composite key means a private chat cannot exist without the contact it names.
Each unit commits whole or not at all, so an interrupted run leaves complete
records and no partial ones. ADR-034 permits one transaction at a time, which
also makes a run-long transaction a latency budget for everything else.

Failure
-------

A problem with the transport ends the run: if Telegram is unreachable the next
chat will not fare better. A problem with a single item does not -- one chat
Telegram describes in a way this application cannot store must not cost the
operator the other two hundred, which is the same judgement the adapter already
makes about a chat that vanishes mid-listing.

Every problem is reported. Nothing is dropped quietly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from tgassist.application.use_cases.account_scope import require_account, require_gateway_account
from tgassist.domain.errors import (
    ConflictError,
    ConstraintViolationError,
    DomainValidationError,
)
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import Chat, ChatType
from tgassist.domain.model.contact import Contact, validate_username
from tgassist.domain.model.identifiers import AccountId, ChatId, ContactId
from tgassist.domain.model.telegram import TelegramChatInfo, TelegramUser
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.contact_repository import ContactRepository
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.telegram_gateway import (
    DEFAULT_CHAT_LIMIT,
    DEFAULT_CONTACT_LIMIT,
    TelegramGateway,
)
from tgassist.domain.ports.unit_of_work import UnitOfWorkFactory
from tgassist.domain.services.operator_identity import SAVED_MESSAGES_TITLE, require_not_operator

#: Chat kinds a newly discovered chat is synchronised for unless configured
#: otherwise. Private only, matching the MVP scope in ``PROJECT_SPEC.md``
#: section 12: every other kind is still *recorded*, so the operator can see it
#: and switch synchronisation on, but nothing ingests it by default.
DEFAULT_SYNC_CHAT_TYPES: frozenset[ChatType] = frozenset({ChatType.PRIVATE})

CREATED = "created"
UPDATED = "updated"
UNCHANGED = "unchanged"
SKIPPED = "skipped"

#: Outcomes in decreasing order of significance. A unit that writes a contact
#: but leaves its chat alone has changed something, and reporting it as
#: unchanged would make a run that did work look like one that did none.
_PRECEDENCE: tuple[str, ...] = (CREATED, UPDATED, UNCHANGED, SKIPPED)

#: What a problem with a *single item* looks like. Anything else -- a database
#: that has gone away, a gateway that cannot reach Telegram -- ends the run,
#: because the next item would meet the same wall.
_ITEM_FAILURES = (DomainValidationError, ConflictError, ConstraintViolationError)


@dataclass(frozen=True, slots=True)
class SyncProblem:
    """Something a run could not do, and what it was.

    Attributes:
        subject: What the problem is about, as a short identifier -- ``chat
            -100123`` or ``user 4242``. Never a name and never any message
            content: a report is printed and logged, and ``SECURITY.md``
            section 9 makes no exception for diagnostics.
        reason: What went wrong, in a sentence a person can act on.
    """

    subject: str
    reason: str

    def __str__(self) -> str:
        """Render the problem as one readable line."""
        return f"{self.subject}: {self.reason}"


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one synchronisation run did.

    Counts rather than entities: a run over two hundred chats that returned them
    all would make the caller hold the whole result set to learn one number, and
    the records are in the database by the time this is returned.

    ``created + updated + unchanged + skipped`` is every item Telegram offered.
    A :class:`SyncProblem` does not always cost an item -- a handle this
    application cannot store leaves the person recorded without one -- so
    problems are counted separately rather than folded into ``skipped``.

    Attributes:
        created: Records that did not exist before.
        updated: Records whose Telegram-owned fields changed.
        unchanged: Records already matching Telegram. The ordinary result of a
            second run, and the number that shows a run is idempotent.
        skipped: Items deliberately not written -- the operator's own identity,
            somebody they deleted, a chat Telegram described without the one
            thing it needs.
        problems: Everything that went wrong, item by item.
    """

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    problems: tuple[SyncProblem, ...] = ()

    @property
    def considered(self) -> int:
        """How many items Telegram offered."""
        return self.created + self.updated + self.unchanged + self.skipped

    @property
    def changed(self) -> int:
        """How many records this run wrote."""
        return self.created + self.updated

    @property
    def is_clean(self) -> bool:
        """Whether the run finished with nothing to report."""
        return not self.problems


@dataclass(slots=True)
class _Tally:
    """A report under construction.

    Mutable, unlike everything else here, because it is written once per item
    inside a loop and rebuilding a frozen report each time would say nothing the
    counters do not.
    """

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    problems: list[SyncProblem] = field(default_factory=list)

    def record(self, outcome: str) -> None:
        """Count one item's outcome."""
        setattr(self, outcome, getattr(self, outcome) + 1)

    def fault(self, subject: str, reason: str) -> None:
        """Note a problem, without deciding what it cost."""
        self.problems.append(SyncProblem(subject=subject, reason=reason))

    def finish(self) -> SyncReport:
        """Freeze into the report a caller receives."""
        return SyncReport(
            created=self.created,
            updated=self.updated,
            unchanged=self.unchanged,
            skipped=self.skipped,
            problems=tuple(self.problems),
        )


class SyncContacts:
    """Records the people in this account's Telegram address book.

    The operator's own entry is skipped rather than refused. Telegram does not
    put it in the address book today, but a contact that *is* the operator is
    forbidden (ADR-052), and a run that failed on one would be a run Telegram
    could break by changing its mind.
    """

    __slots__ = ("_accounts", "_clock", "_contacts", "_ids", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        contacts: ScopedRepositoryFactory[ContactRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._contacts = contacts
        self._accounts = accounts
        self._clock = clock
        self._ids = ids

    async def execute(
        self,
        gateway: TelegramGateway,
        account_id: AccountId | None = None,
        *,
        limit: int = DEFAULT_CONTACT_LIMIT,
    ) -> SyncReport:
        """Read the address book and record what it holds.

        The gateway is a parameter rather than a constructor dependency because
        it holds a live connection, and a use case built once per call has no
        lifetime to hang that on (``TELEGRAM_ARCHITECTURE.md`` section 7.3).

        Args:
            gateway: A connected gateway bound to this account.
            account_id: Account to synchronise. ``None`` selects the active one.
            limit: How many address-book entries to resolve at most.

        Returns:
            What the run did.

        Raises:
            RecordNotFoundError: If no account matches, or none is active.
            AuthorizationError: If the gateway is bound to a different account,
                or the account is not signed in.
            TelegramError: If Telegram could not be read. The run ends there: if
                the address book is unreachable, the next entry will not fare
                better.
        """
        account = await _load_account(self._unit_of_work, self._accounts, account_id)
        require_gateway_account(gateway, account.id)

        book = await gateway.list_contacts(limit=limit)

        tally = _Tally()
        for user in book:
            await self._one(account, user, tally)
        return tally.finish()

    async def _one(self, account: Account, user: TelegramUser, tally: _Tally) -> None:
        """Record one person, in a transaction of their own."""
        subject = f"user {int(user.id)}"
        if account.is_operator(user.id):
            tally.record(SKIPPED)
            return

        try:
            async with self._unit_of_work() as uow:
                contacts = self._contacts(uow, account.id)
                outcome, _ = await _upsert_contact(
                    contacts, account, user, self._clock.now(), self._ids, tally, subject
                )
                if outcome in (CREATED, UPDATED):
                    await uow.commit()
            tally.record(outcome)
        except _ITEM_FAILURES as exc:
            tally.record(SKIPPED)
            tally.fault(subject, _explain(exc))


class SyncChats:
    """Records the chats Telegram has for this account, and the people in them.

    A chat's contact is written before the chat, in the same transaction,
    because ADR-043's composite key means a private chat cannot exist without
    the contact it names. The order is a correctness requirement rather than a
    preference (``TELEGRAM_ARCHITECTURE.md`` section 8.6).

    Cross-account ownership is structurally impossible here rather than checked:
    both repositories are scoped to the single account this run resolved, and
    the contact a chat names is one this run read back from that scoped
    repository. There is no argument through which another account's identifier
    could arrive.
    """

    __slots__ = (
        "_accounts",
        "_chats",
        "_clock",
        "_contacts",
        "_ids",
        "_sync_types",
        "_unit_of_work",
    )

    def __init__(  # noqa: PLR0913, PLR0917 - one collaborator per dependency
        self,
        unit_of_work: UnitOfWorkFactory,
        chats: ScopedRepositoryFactory[ChatRepository],
        contacts: ScopedRepositoryFactory[ContactRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
        ids: IdGenerator,
        sync_chat_types: frozenset[ChatType] = DEFAULT_SYNC_CHAT_TYPES,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Args:
            unit_of_work: Transaction factory.
            chats: Chat repository factory, scoped per account.
            contacts: Contact repository factory, scoped per account.
            accounts: Account repository factory.
            clock: Time source.
            ids: Local identifier generator.
            sync_chat_types: Which kinds of chat are synchronised when first
                discovered. Every kind is recorded either way; this decides only
                the initial ``sync_enabled``, and nothing revisits it.
        """
        self._unit_of_work = unit_of_work
        self._chats = chats
        self._contacts = contacts
        self._accounts = accounts
        self._clock = clock
        self._ids = ids
        self._sync_types = sync_chat_types

    async def execute(
        self,
        gateway: TelegramGateway,
        account_id: AccountId | None = None,
        *,
        limit: int = DEFAULT_CHAT_LIMIT,
    ) -> SyncReport:
        """Read the chat list and record what it holds.

        Args:
            gateway: A connected gateway bound to this account.
            account_id: Account to synchronise. ``None`` selects the active one.
            limit: How many chats to read at most.

        Returns:
            What the run did.

        Raises:
            RecordNotFoundError: If no account matches, or none is active.
            AuthorizationError: If the gateway is bound to a different account,
                or the account is not signed in.
            TelegramError: If Telegram could not be read.
        """
        account = await _load_account(self._unit_of_work, self._accounts, account_id)
        require_gateway_account(gateway, account.id)

        listing = await gateway.list_chats(limit=limit)

        tally = _Tally()
        for info in listing:
            await self._one(gateway, account, info, tally)
        return tally.finish()

    async def _one(
        self,
        gateway: TelegramGateway,
        account: Account,
        info: TelegramChatInfo,
        tally: _Tally,
    ) -> None:
        """Record one chat and, for a private one, the person in it.

        The counterpart is resolved *before* the transaction opens. It is a
        network call, and holding the application's only transaction open across
        one would make every other operation wait on Telegram (ADR-034).
        """
        subject = f"chat {int(info.id)}"
        counterpart: TelegramUser | None = None

        if info.is_private and not _is_saved_messages(account, info):
            if info.counterpart_id is None:
                # Telegram called it private and named nobody. A private chat
                # with no contact is not representable, and inventing one would
                # be worse than saying so.
                tally.record(SKIPPED)
                tally.fault(subject, "Telegram described a private chat with no counterpart.")
                return
            counterpart = await gateway.get_contact(info.counterpart_id)
            if counterpart is None:
                tally.record(SKIPPED)
                tally.fault(subject, "The other person's Telegram account could not be read.")
                return

        try:
            tally.record(await self._write(account, info, counterpart, tally, subject))
        except _ITEM_FAILURES as exc:
            tally.record(SKIPPED)
            tally.fault(subject, _explain(exc))

    async def _write(
        self,
        account: Account,
        info: TelegramChatInfo,
        counterpart: TelegramUser | None,
        tally: _Tally,
        subject: str,
    ) -> str:
        """Write one chat, and its contact when it has one, in one transaction.

        The chat is the item this run counts, so a skipped chat is a skipped
        unit whatever happened to the person in it. Otherwise the more
        significant of the two outcomes is reported: a run that renamed somebody
        but left their chat alone did change something, and calling that
        unchanged would make work look like idleness.
        """
        now = self._clock.now()
        async with self._unit_of_work() as uow:
            contact: Contact | None = None
            person = UNCHANGED

            if counterpart is not None:
                contacts = self._contacts(uow, account.id)
                person, contact = await _upsert_contact(
                    contacts, account, counterpart, now, self._ids, tally, subject
                )
                if contact is None:
                    # The operator deleted this person, and a private chat
                    # cannot be stored without the contact it names.
                    return SKIPPED

            chats = self._chats(uow, account.id)
            outcome = await self._upsert_chat(chats, account, info, contact, now, tally, subject)

            # A contact rename is a real change even when the chat beside it was
            # left alone, so it is committed on its own account.
            if _strongest(person, outcome) in (CREATED, UPDATED):
                await uow.commit()
            return outcome if outcome == SKIPPED else _strongest(person, outcome)

    async def _upsert_chat(  # noqa: PLR0913, PLR0917 - one argument per thing the write needs
        self,
        chats: ChatRepository,
        account: Account,
        info: TelegramChatInfo,
        contact: Contact | None,
        now: datetime,
        tally: _Tally,
        subject: str,
    ) -> str:
        """Create or update one chat, returning what happened to it."""
        stored = await chats.get_by_telegram_id(info.id)
        resolved = _local_chat_type(account, info)

        if stored is None:
            await chats.add(self._build(account, info, resolved, contact, now))
            return CREATED

        if stored.chat_type is not resolved:
            # Telegram can promote a basic group to a supergroup, but that
            # changes the chat identifier too, so it arrives as a new chat. A
            # kind changing underneath a stored identifier is something this
            # model has no transition for, and guessing one would rewrite
            # history the operator can see.
            tally.fault(
                subject,
                f"Telegram now calls this a {resolved.value} chat, but it is stored "
                f"as {stored.chat_type.value}. It was left alone.",
            )
            return SKIPPED

        if stored.is_private:
            # A private chat's name belongs to its contact, and was written
            # above. Telegram owns nothing else on this row.
            return UNCHANGED

        changed = stored.retitled(_titled(account, info), now)
        if changed is stored:
            return UNCHANGED
        await chats.update(changed)
        return UPDATED

    def _build(
        self,
        account: Account,
        info: TelegramChatInfo,
        resolved: ChatType,
        contact: Contact | None,
        now: datetime,
    ) -> Chat:
        """Build a new Chat of the right kind.

        ``sync_enabled`` is decided here and only here. A later run finds a row
        and never revisits the question, which is what keeps a chat the operator
        switched off from switching itself back on.
        """
        enabled = resolved in self._sync_types
        if resolved is not ChatType.PRIVATE:
            return Chat.group_titled(
                chat_id=ChatId(self._ids.new_id()),
                account_id=account.id,
                telegram_chat_id=info.id,
                chat_type=resolved,
                title=_titled(account, info),
                now=now,
                sync_enabled=enabled,
            )

        if contact is None:  # pragma: no cover - the caller resolved one
            msg = f"Chat {int(info.id)} is private and has no contact to name"
            raise DomainValidationError(
                msg, user_message="A private chat must name the contact it is with."
            )
        return Chat.private_with(
            chat_id=ChatId(self._ids.new_id()),
            account_id=account.id,
            telegram_chat_id=info.id,
            contact_id=contact.id,
            now=now,
            sync_enabled=enabled,
        )


async def _upsert_contact(  # noqa: PLR0913, PLR0917 - one argument per thing the write needs
    contacts: ContactRepository,
    account: Account,
    user: TelegramUser,
    now: datetime,
    ids: IdGenerator,
    tally: _Tally,
    subject: str,
) -> tuple[str, Contact | None]:
    """Create or update one contact, returning what happened and the result.

    Shared by both use cases, because a person reached through a chat and the
    same person reached through the address book must produce one row -- and two
    copies of "what does Telegram own about a contact" would eventually disagree
    about it.

    Returns ``(outcome, None)`` for somebody the operator deleted. Nothing
    Telegram says about them is a reason to bring them back, and a caller that
    needs the contact learns it has none rather than being handed a resurrected
    one.

    Raises:
        DomainValidationError: If the user is the account's own operator
            identity (ADR-052). The callers divert that case before reaching
            here; this is the backstop that makes it unreachable rather than
            merely unreached.
    """
    require_not_operator(account, user.id)

    stored = await contacts.get_by_telegram_id(user.id, include_deleted=True)
    username = _storable_username(user, tally, subject)

    if stored is None:
        contact = Contact.create(
            contact_id=ContactId(ids.new_id()),
            account_id=account.id,
            telegram_user_id=user.id,
            display_name=user.display_name,
            username=username,
            now=now,
        )
        await contacts.add(contact)
        return CREATED, contact

    if stored.is_deleted:
        return SKIPPED, None

    changed = stored.renamed(user.display_name, now)
    if user.username is None or username is not None:
        # A handle Telegram reports that could not be stored leaves the existing
        # one alone: it is the last handle that *was* storable, which is closer
        # to the truth than discarding it. A handle Telegram no longer reports
        # is genuinely gone, and is cleared.
        changed = changed.with_username(username, now)

    if changed is stored:
        return UNCHANGED, stored
    await contacts.update(changed)
    return UPDATED, changed


async def _load_account(
    unit_of_work: UnitOfWorkFactory,
    accounts: RepositoryFactory[AccountRepository],
    account_id: AccountId | None,
) -> Account:
    """Load the account a run belongs to, in a transaction of its own.

    Read before the run rather than inside each unit: the operator's own
    Telegram identity is what every unit is measured against, and re-reading it
    per item would let it change halfway through a run.
    """
    async with unit_of_work() as uow:
        return await require_account(accounts(uow), account_id)


def _storable_username(user: TelegramUser, tally: _Tally, subject: str) -> str | None:
    """Return the handle if this application can hold it, reporting it if not.

    Telegram's own rule and this application's agree today, so this is a
    boundary check rather than a routine one -- but Telegram has granted short
    handles historically, and losing a person over a field nothing yet reads
    would be the wrong trade.
    """
    if user.username is None:
        return None
    try:
        return validate_username(user.username)
    except DomainValidationError as exc:
        tally.fault(subject, f"{_explain(exc)} The person was recorded without a handle.")
        return None


def _is_saved_messages(account: Account, info: TelegramChatInfo) -> bool:
    """Whether a chat is Telegram's Saved Messages, which is a chat with oneself."""
    return (
        info.is_private
        and info.counterpart_id is not None
        and account.is_operator(info.counterpart_id)
    )


def _local_chat_type(account: Account, info: TelegramChatInfo) -> ChatType:
    """Return the kind this chat is stored as.

    The one place Telegram's answer is not taken at face value. Saved Messages
    arrives as a private chat, because that is what it is on the wire -- but a
    private chat names a contact, and that contact would be the operator, which
    ADR-052 forbids. The domain already has ``SAVED`` for exactly this, and
    recognising it is what stops the first run against any real account from
    trying to record its owner as one of their own contacts.
    """
    return ChatType.SAVED if _is_saved_messages(account, info) else info.chat_type


def _titled(account: Account, info: TelegramChatInfo) -> str:
    """Return a non-empty title for a chat that must have one.

    Telegram normally names Saved Messages in the operator's own language, and
    that name is preferred. The fallbacks exist because a titled chat with a
    blank title is unrepresentable, and an unnamed chat should still be stored.
    """
    title = info.title.strip()
    if title:
        return title
    return SAVED_MESSAGES_TITLE if _is_saved_messages(account, info) else f"Chat {int(info.id)}"


def _strongest(*outcomes: str) -> str:
    """Return the most significant of several outcomes."""
    return min(outcomes, key=_PRECEDENCE.index)


def _explain(error: Exception) -> str:
    """Return the safe, user-facing sentence an error carries.

    Deliberately not ``str(error)``: a developer message names values, and a
    report is both printed and logged.
    """
    message = getattr(error, "user_message", None)
    return message if isinstance(message, str) and message else "It could not be recorded."


__all__ = [
    "CREATED",
    "DEFAULT_SYNC_CHAT_TYPES",
    "SKIPPED",
    "UNCHANGED",
    "UPDATED",
    "SyncChats",
    "SyncContacts",
    "SyncProblem",
    "SyncReport",
]
