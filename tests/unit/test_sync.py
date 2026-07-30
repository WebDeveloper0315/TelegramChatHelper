"""Chat and contact synchronisation.

Three layers, because the guarantees live at different depths:

* the operator-identity rule, which is a domain rule and is checked as one;
* the use cases against fakes, which is where behaviour lives;
* the use cases against a **real SQLite database**, which is the only place
  "an interrupted unit leaves nothing behind" can be observed at all. The
  in-memory repositories write through immediately, so a rollback is invisible
  to them -- and rollback is exactly what the transaction boundary promises.

No test here needs a Telegram account, a network or a real native library.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import AdvanceableClock, SequentialIdGenerator
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.chat_repository import InMemoryChatRepository, InMemoryChatStore
from tests.fakes.contact_repository import InMemoryContactRepository, InMemoryContactStore
from tests.fakes.telegram_gateway import FakeTelegramGateway
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.container import Container
from tgassist.application.use_cases.account import CreateAccountRequest
from tgassist.application.use_cases.contact import CreateContact
from tgassist.application.use_cases.sync import (
    DEFAULT_SYNC_CHAT_TYPES,
    SyncChats,
    SyncContacts,
    SyncProblem,
    SyncReport,
)
from tgassist.domain.errors import (
    AuthorizationError,
    DatabaseUnavailableError,
    DomainValidationError,
    RecordNotFoundError,
    TelegramError,
)
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import AiProcessingMode, Chat, ChatType
from tgassist.domain.model.contact import Contact
from tgassist.domain.model.identifiers import (
    AccountId,
    ContactId,
    TelegramChatId,
    TelegramUserId,
)
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.telegram import TelegramChatInfo, TelegramUser
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.domain.services.operator_identity import (
    SAVED_MESSAGES_TITLE,
    require_not_operator,
)

NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)

#: Who the operator is. Both accounts are deliberately *different* people, so a
#: test that leaked one account's operator identity into the other would fail.
OPERATOR_A = TelegramUserId(1001)
OPERATOR_B = TelegramUserId(1002)

ADA = TelegramUser(
    id=TelegramUserId(2002), first_name="Ada", last_name="Lovelace", username="ada_lovelace"
)
GRACE = TelegramUser(id=TelegramUserId(3003), first_name="Grace", username="ghopper")
OPERATOR_USER = TelegramUser(id=OPERATOR_A, first_name="Me", username="myself_here")

PRIVATE_WITH_ADA = TelegramChatInfo(
    id=TelegramChatId(2002),
    chat_type=ChatType.PRIVATE,
    title="Ada Lovelace",
    counterpart_id=ADA.id,
)
GROUP = TelegramChatInfo(
    id=TelegramChatId(-100_500), chat_type=ChatType.SUPERGROUP, title="Engineering"
)
SAVED = TelegramChatInfo(
    id=TelegramChatId(1001),
    chat_type=ChatType.PRIVATE,
    title="Saved Messages",
    counterpart_id=OPERATOR_A,
)


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


class _RegisteringContactRepository(InMemoryContactRepository):
    """Registers each new contact with the chat store, as inserting a row does.

    Without this the chat store's stand-in for the composite foreign key would
    never see a contact synchronisation created, and every private chat would be
    rejected for referencing somebody who "does not exist". The schema has no
    such problem: the row is there by the time the chat references it.
    """

    __slots__ = ("_chat_store",)

    def __init__(
        self,
        store: InMemoryContactStore,
        account_id: AccountId,
        chat_store: InMemoryChatStore,
    ) -> None:
        """Bind to both stores."""
        super().__init__(store, account_id)
        self._chat_store = chat_store

    async def add(self, contact: Contact) -> None:
        """Persist a contact and make it referenceable."""
        await super().add(contact)
        self._chat_store.register_contact(contact.id, contact.account_id)


class _Harness:
    """A synchronisation environment built entirely from fakes."""

    def __init__(self) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.contact_store = InMemoryContactStore(known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)})
        self.chat_store = InMemoryChatStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)}, contacts={}
        )
        self.clock = AdvanceableClock(NOW)
        self.ids = SequentialIdGenerator(start=100)
        self.units: list[InMemoryUnitOfWork] = []
        self._factory = InMemoryUnitOfWorkFactory()

    def unit_of_work(self) -> InMemoryUnitOfWork:
        uow = self._factory()
        self.units.append(uow)
        return uow

    def accounts(self, _uow: UnitOfWork) -> InMemoryAccountRepository:
        return self.accounts_repository

    def contacts(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryContactRepository:
        return _RegisteringContactRepository(self.contact_store, account_id, self.chat_store)

    def chats(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryChatRepository:
        return InMemoryChatRepository(self.chat_store, account_id)

    @property
    def commits(self) -> int:
        """How many transactions committed. One per unit that wrote something."""
        return sum(1 for unit in self.units if unit.is_committed)

    async def add_account(
        self,
        account_id: AccountId,
        operator: TelegramUserId,
        *,
        is_active: bool = False,
    ) -> Account:
        account = Account.create(
            account_id=account_id,
            telegram_user_id=operator,
            display_name=f"account-{int(account_id)}",
            now=NOW,
            is_active=is_active,
        )
        await self.accounts_repository.add(account)
        return account

    async def add_contact(
        self,
        account_id: AccountId,
        user: TelegramUser,
        *,
        contact_id: int = 11,
        display_name: str | None = None,
        username: str | None = None,
    ) -> Contact:
        contact = Contact.create(
            contact_id=ContactId(contact_id),
            account_id=account_id,
            telegram_user_id=user.id,
            display_name=display_name if display_name is not None else user.display_name,
            username=username,
            now=NOW,
        )
        await self.contacts(self.unit_of_work(), account_id).add(contact)
        return contact

    def sync_contacts(self) -> SyncContacts:
        return SyncContacts(self.unit_of_work, self.contacts, self.accounts, self.clock, self.ids)

    def sync_chats(self, types: frozenset[ChatType] = DEFAULT_SYNC_CHAT_TYPES) -> SyncChats:
        return SyncChats(
            self.unit_of_work,
            self.chats,
            self.contacts,
            self.accounts,
            self.clock,
            self.ids,
            types,
        )

    async def stored_contacts(self, account_id: AccountId) -> list[Contact]:
        page = await self.contacts(self.unit_of_work(), account_id).list_contacts(
            PageRequest(limit=100), include_archived=True
        )
        return list(page.items)

    async def stored_chats(self, account_id: AccountId) -> list[Chat]:
        page = await self.chats(self.unit_of_work(), account_id).list_chats(PageRequest(limit=100))
        return list(page.items)


@pytest.fixture
def harness() -> _Harness:
    """A fresh environment for one test."""
    return _Harness()


@pytest.fixture
async def gateway() -> AsyncIterator[FakeTelegramGateway]:
    """A connected, authorized gateway for account A."""
    fake = FakeTelegramGateway(ACCOUNT_A, user=OPERATOR_USER, starts_authorized=True)
    await fake.connect()
    try:
        yield fake
    finally:
        await fake.disconnect()


# ---------------------------------------------------------------------------
# The operator-identity rule
# ---------------------------------------------------------------------------


class TestOperatorIdentity:
    """``DOMAIN_MODEL.md`` section 5.4's invariant, finally enforceable."""

    def _account(self) -> Account:
        return Account.create(
            account_id=ACCOUNT_A,
            telegram_user_id=OPERATOR_A,
            display_name="me",
            now=NOW,
        )

    def test_an_account_recognises_itself(self) -> None:
        assert self._account().is_operator(OPERATOR_A)

    def test_an_account_does_not_recognise_somebody_else(self) -> None:
        assert not self._account().is_operator(ADA.id)

    def test_another_account_is_not_this_operator(self) -> None:
        # Two accounts, two people. The identity is the Account's, not global.
        assert not self._account().is_operator(OPERATOR_B)

    def test_recording_somebody_else_is_permitted(self) -> None:
        require_not_operator(self._account(), ADA.id)

    def test_recording_the_operator_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="operator identity") as excinfo:
            require_not_operator(self._account(), OPERATOR_A)

        assert "your own Telegram account" in excinfo.value.user_message

    def test_the_refusal_names_both_sides(self) -> None:
        with pytest.raises(DomainValidationError) as excinfo:
            require_not_operator(self._account(), OPERATOR_A)

        assert excinfo.value.context == {
            "account_id": int(ACCOUNT_A),
            "telegram_user_id": int(OPERATOR_A),
        }


class TestCreatingAContactByHand:
    """The same rule, on the other write path."""

    async def test_it_refuses_the_operator(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        use_case = CreateContact(
            harness.unit_of_work, harness.contacts, harness.accounts, harness.clock, harness.ids
        )

        with pytest.raises(DomainValidationError, match="operator identity"):
            await use_case.execute(telegram_user_id=int(OPERATOR_A), display_name="me")

    async def test_it_still_accepts_anybody_else(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        use_case = CreateContact(
            harness.unit_of_work, harness.contacts, harness.accounts, harness.clock, harness.ids
        )

        contact = await use_case.execute(telegram_user_id=int(ADA.id), display_name="Ada")

        assert contact.telegram_user_id == ADA.id


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class TestSyncReport:
    def test_it_counts_everything_offered(self) -> None:
        report = SyncReport(created=2, updated=1, unchanged=3, skipped=1)

        assert report.considered == 7
        assert report.changed == 3

    def test_a_run_with_no_problems_is_clean(self) -> None:
        assert SyncReport(created=1).is_clean

    def test_a_run_with_problems_is_not(self) -> None:
        assert not SyncReport(problems=(SyncProblem("chat 1", "no"),)).is_clean

    def test_a_problem_reads_as_one_line(self) -> None:
        assert str(SyncProblem("user 42", "It went wrong.")) == "user 42: It went wrong."


# ---------------------------------------------------------------------------
# Contact synchronisation
# ---------------------------------------------------------------------------


class TestFirstContactSync:
    async def test_it_records_the_address_book(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(ADA, GRACE)

        report = await harness.sync_contacts().execute(gateway)

        assert report.created == 2
        assert {c.telegram_user_id for c in await harness.stored_contacts(ACCOUNT_A)} == {
            ADA.id,
            GRACE.id,
        }

    async def test_it_stores_the_name_and_the_handle(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(ADA)

        await harness.sync_contacts().execute(gateway)

        (contact,) = await harness.stored_contacts(ACCOUNT_A)
        assert contact.display_name == "Ada Lovelace"
        assert contact.username == "ada_lovelace"

    async def test_a_person_with_no_handle_is_stored_without_one(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(TelegramUser(id=TelegramUserId(5005), first_name="Sam"))

        await harness.sync_contacts().execute(gateway)

        (contact,) = await harness.stored_contacts(ACCOUNT_A)
        assert contact.username is None

    async def test_one_transaction_per_person(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Not one per run: an interrupted run leaves complete records, and the
        # application's single transaction is not held for the whole thing.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(ADA, GRACE)

        await harness.sync_contacts().execute(gateway)

        assert harness.commits == 2

    async def test_it_uses_the_active_account_when_none_is_named(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(ADA)

        await harness.sync_contacts().execute(gateway, None)

        assert len(await harness.stored_contacts(ACCOUNT_A)) == 1

    async def test_it_refuses_when_no_account_is_active(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        with pytest.raises(RecordNotFoundError, match="No account is active"):
            await harness.sync_contacts().execute(gateway)


class TestRepeatedContactSync:
    async def test_it_creates_no_duplicates(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(ADA, GRACE)
        use_case = harness.sync_contacts()

        await use_case.execute(gateway)
        report = await use_case.execute(gateway)

        assert report.unchanged == 2
        assert report.created == 0
        assert len(await harness.stored_contacts(ACCOUNT_A)) == 2

    async def test_it_writes_nothing_the_second_time(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(ADA, GRACE)
        use_case = harness.sync_contacts()
        await use_case.execute(gateway)
        before = harness.commits

        await use_case.execute(gateway)

        assert harness.commits == before

    async def test_it_does_not_move_the_update_time(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # ``updated_at`` means "when this last changed", not "when we last
        # looked". A run that touched it would make every retention and
        # staleness question unanswerable.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(ADA)
        use_case = harness.sync_contacts()
        await use_case.execute(gateway)

        harness.clock.advance(timedelta(hours=1))
        await use_case.execute(gateway)

        (contact,) = await harness.stored_contacts(ACCOUNT_A)
        assert contact.updated_at == NOW


class TestChangedContactData:
    async def test_a_changed_handle_is_recorded(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        await harness.add_contact(ACCOUNT_A, ADA, username="ada_lovelace")
        gateway.script_contacts(
            TelegramUser(id=ADA.id, first_name="Ada", last_name="Lovelace", username="countess")
        )

        report = await harness.sync_contacts().execute(gateway)

        (contact,) = await harness.stored_contacts(ACCOUNT_A)
        assert report.updated == 1
        assert contact.username == "countess"

    async def test_a_changed_name_is_recorded(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        await harness.add_contact(ACCOUNT_A, ADA)
        gateway.script_contacts(TelegramUser(id=ADA.id, first_name="Ada", last_name="Byron"))

        await harness.sync_contacts().execute(gateway)

        (contact,) = await harness.stored_contacts(ACCOUNT_A)
        assert contact.display_name == "Ada Byron"

    async def test_a_removed_handle_is_cleared(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Telegram no longer reporting a handle means it is gone, and keeping a
        # stale one would show a handle that no longer reaches anybody.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        await harness.add_contact(ACCOUNT_A, ADA, username="ada_lovelace")
        gateway.script_contacts(TelegramUser(id=ADA.id, first_name="Ada", last_name="Lovelace"))

        await harness.sync_contacts().execute(gateway)

        (contact,) = await harness.stored_contacts(ACCOUNT_A)
        assert contact.username is None

    async def test_a_handle_that_cannot_be_stored_costs_the_handle_not_the_person(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Telegram has granted short handles historically. Losing somebody the
        # operator talks to over a field nothing yet reads is the wrong trade.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(TelegramUser(id=ADA.id, first_name="Ada", username="ada"))

        report = await harness.sync_contacts().execute(gateway)

        (contact,) = await harness.stored_contacts(ACCOUNT_A)
        assert contact.username is None
        assert report.created == 1
        assert "handle" in report.problems[0].reason

    async def test_an_unstorable_handle_leaves_the_previous_one_alone(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        await harness.add_contact(ACCOUNT_A, ADA, username="ada_lovelace")
        gateway.script_contacts(
            TelegramUser(id=ADA.id, first_name="Ada", last_name="Lovelace", username="ada")
        )

        await harness.sync_contacts().execute(gateway)

        (contact,) = await harness.stored_contacts(ACCOUNT_A)
        assert contact.username == "ada_lovelace"


class TestSynchronisationRespectsOperatorDecisions:
    async def test_a_deleted_contact_is_not_resurrected(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        contact = await harness.add_contact(ACCOUNT_A, ADA, display_name="Ada")
        contacts = harness.contacts(harness.unit_of_work(), ACCOUNT_A)
        await contacts.update(contact.deleted(NOW))
        gateway.script_contacts(TelegramUser(id=ADA.id, first_name="Ada", last_name="Lovelace"))

        report = await harness.sync_contacts().execute(gateway)

        stored = await contacts.get(contact.id, include_deleted=True)
        assert stored is not None
        assert stored.is_deleted
        assert stored.display_name == "Ada"
        assert report.skipped == 1

    async def test_an_archived_contact_is_still_updated(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Archived means "out of my way", not "forget them". Their name should
        # still be right when they are restored.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        contact = await harness.add_contact(ACCOUNT_A, ADA, display_name="Ada")
        contacts = harness.contacts(harness.unit_of_work(), ACCOUNT_A)
        await contacts.update(contact.archived(NOW))
        gateway.script_contacts(TelegramUser(id=ADA.id, first_name="Ada", last_name="Lovelace"))

        await harness.sync_contacts().execute(gateway)

        stored = await contacts.get(contact.id)
        assert stored is not None
        assert stored.is_archived
        assert stored.display_name == "Ada Lovelace"

    async def test_nothing_is_ever_deleted(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # A person Telegram no longer lists is still somebody the operator has
        # history with. Telegram's opinion of what exists is not a mandate.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        await harness.add_contact(ACCOUNT_A, GRACE, contact_id=22)
        gateway.script_contacts(ADA)

        await harness.sync_contacts().execute(gateway)

        assert {c.telegram_user_id for c in await harness.stored_contacts(ACCOUNT_A)} == {
            ADA.id,
            GRACE.id,
        }


class TestTheOperatorIsNeverAContact:
    async def test_an_address_book_containing_the_operator_skips_them(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(ADA, OPERATOR_USER)

        report = await harness.sync_contacts().execute(gateway)

        assert report.created == 1
        assert report.skipped == 1
        assert {c.telegram_user_id for c in await harness.stored_contacts(ACCOUNT_A)} == {ADA.id}

    async def test_the_other_account_is_not_this_one_s_operator(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Account B's owner is an ordinary person from account A's point of view.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(TelegramUser(id=OPERATOR_B, first_name="Someone else"))

        report = await harness.sync_contacts().execute(gateway)

        assert report.created == 1


class TestTwoAccountsKnowingTheSamePerson:
    async def test_each_gets_its_own_contact(self, harness: _Harness) -> None:
        # ADR-041: ``telegram_user_id`` is not unique in the table, only
        # ``(account_id, telegram_user_id)`` is. What is remembered about a
        # person differs per account.
        await harness.add_account(ACCOUNT_A, OPERATOR_A)
        await harness.add_account(ACCOUNT_B, OPERATOR_B)

        for account, operator in ((ACCOUNT_A, OPERATOR_A), (ACCOUNT_B, OPERATOR_B)):
            fake = FakeTelegramGateway(
                account,
                user=TelegramUser(id=operator, first_name="Me"),
                starts_authorized=True,
            )
            await fake.connect()
            fake.script_contacts(ADA)
            await harness.sync_contacts().execute(fake, account)
            await fake.disconnect()

        (from_a,) = await harness.stored_contacts(ACCOUNT_A)
        (from_b,) = await harness.stored_contacts(ACCOUNT_B)
        assert from_a.id != from_b.id
        assert from_a.telegram_user_id == from_b.telegram_user_id == ADA.id

    async def test_neither_can_see_the_other_s_contacts(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A)
        await harness.add_account(ACCOUNT_B, OPERATOR_B)
        fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        await fake.connect()
        fake.script_contacts(ADA)

        await harness.sync_contacts().execute(fake, ACCOUNT_A)
        await fake.disconnect()

        assert await harness.stored_contacts(ACCOUNT_B) == []


class TestContactSyncOwnership:
    async def test_a_gateway_for_another_account_is_refused(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A)
        await harness.add_account(ACCOUNT_B, OPERATOR_B)

        with pytest.raises(AuthorizationError, match="bound to account"):
            await harness.sync_contacts().execute(gateway, ACCOUNT_B)

    async def test_nothing_is_read_before_the_check(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # The refusal happens before Telegram is touched, so a mis-wired call
        # cannot even see the other account's data.
        await harness.add_account(ACCOUNT_A, OPERATOR_A)
        await harness.add_account(ACCOUNT_B, OPERATOR_B)
        gateway.script_contacts(ADA)

        with pytest.raises(AuthorizationError):
            await harness.sync_contacts().execute(gateway, ACCOUNT_B)

        assert gateway.contact_calls == 0


# ---------------------------------------------------------------------------
# Chat synchronisation
# ---------------------------------------------------------------------------


class TestFirstChatSync:
    async def test_a_private_chat_creates_the_chat_and_the_person(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(PRIVATE_WITH_ADA)
        gateway.script_users(ADA)

        report = await harness.sync_chats().execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        (contact,) = await harness.stored_contacts(ACCOUNT_A)
        assert report.created == 1
        assert chat.chat_type is ChatType.PRIVATE
        assert chat.contact_id == contact.id
        assert contact.display_name == "Ada Lovelace"

    async def test_the_chat_and_the_person_share_one_transaction(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # ADR-043's composite key makes the pair the atomic unit: a private chat
        # cannot exist without the contact it names.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(PRIVATE_WITH_ADA)
        gateway.script_users(ADA)

        await harness.sync_chats().execute(gateway)

        assert harness.commits == 1

    async def test_a_group_is_recorded_with_its_title(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(GROUP)

        await harness.sync_chats().execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert chat.chat_type is ChatType.SUPERGROUP
        assert chat.title == "Engineering"
        assert chat.contact_id is None

    async def test_a_group_creates_no_contact(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(GROUP)

        await harness.sync_chats().execute(gateway)

        assert await harness.stored_contacts(ACCOUNT_A) == []

    async def test_a_negative_chat_identifier_is_accepted(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Telegram numbers groups and channels below zero.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(GROUP)

        await harness.sync_chats().execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert int(chat.telegram_chat_id) < 0

    async def test_the_counterpart_is_resolved_once_per_chat(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(PRIVATE_WITH_ADA)
        gateway.script_users(ADA)

        await harness.sync_chats().execute(gateway)

        assert gateway.contact_calls == 1

    async def test_the_limit_reaches_telegram(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(PRIVATE_WITH_ADA, GROUP)
        gateway.script_users(ADA)

        report = await harness.sync_chats().execute(gateway, limit=1)

        assert report.considered == 1


class TestSyncScope:
    async def test_a_private_chat_is_synchronised_by_default(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(PRIVATE_WITH_ADA)
        gateway.script_users(ADA)

        await harness.sync_chats().execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert chat.sync_enabled

    async def test_a_group_is_recorded_but_not_synchronised(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Recorded so the operator can see it and switch synchronisation on;
        # not ingested, because the MVP scope is private chats.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(GROUP)

        await harness.sync_chats().execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert not chat.sync_enabled

    async def test_configuration_decides_which_kinds(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(GROUP)

        await harness.sync_chats(frozenset({ChatType.SUPERGROUP})).execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert chat.sync_enabled

    async def test_a_new_chat_starts_with_content_kept_local(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(PRIVATE_WITH_ADA)
        gateway.script_users(ADA)

        await harness.sync_chats().execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert chat.ai_processing_mode is AiProcessingMode.LOCAL_ONLY

    async def test_it_never_revisits_an_operator_s_choice(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # The privacy guarantee: a chat somebody switched off must not switch
        # itself back on, and one they restricted must stay restricted.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(PRIVATE_WITH_ADA)
        gateway.script_users(ADA)
        use_case = harness.sync_chats()
        await use_case.execute(gateway)

        chats = harness.chats(harness.unit_of_work(), ACCOUNT_A)
        (stored,) = await harness.stored_chats(ACCOUNT_A)
        await chats.update(
            stored.with_sync_enabled(False, NOW).with_ai_processing_mode(
                AiProcessingMode.DISABLED, NOW
            )
        )
        await use_case.execute(gateway)

        (after,) = await harness.stored_chats(ACCOUNT_A)
        assert not after.sync_enabled
        assert after.ai_processing_mode is AiProcessingMode.DISABLED


class TestSavedMessages:
    """Telegram's chat with oneself, which every real account has."""

    async def test_it_is_stored_as_a_saved_chat(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(SAVED)

        await harness.sync_chats().execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert chat.chat_type is ChatType.SAVED
        assert chat.title == "Saved Messages"

    async def test_it_creates_no_contact_for_the_operator(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Without this every first run against a real account would try to
        # record its owner as one of their own contacts, which ADR-052 forbids.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(SAVED)

        await harness.sync_chats().execute(gateway)

        assert await harness.stored_contacts(ACCOUNT_A) == []

    async def test_the_counterpart_is_never_looked_up(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(SAVED)

        await harness.sync_chats().execute(gateway)

        assert gateway.contact_calls == 0

    async def test_an_untitled_one_gets_a_name(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(
            TelegramChatInfo(
                id=TelegramChatId(int(OPERATOR_A)),
                chat_type=ChatType.PRIVATE,
                title="   ",
                counterpart_id=OPERATOR_A,
            )
        )

        await harness.sync_chats().execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert chat.title == SAVED_MESSAGES_TITLE

    async def test_it_is_the_same_chat_on_a_second_run(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(SAVED)
        use_case = harness.sync_chats()

        await use_case.execute(gateway)
        report = await use_case.execute(gateway)

        assert report.unchanged == 1
        assert len(await harness.stored_chats(ACCOUNT_A)) == 1

    async def test_another_account_s_owner_is_an_ordinary_person(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # A private chat with account B's owner is a normal chat for account A.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(
            TelegramChatInfo(
                id=TelegramChatId(int(OPERATOR_B)),
                chat_type=ChatType.PRIVATE,
                title="Someone Else",
                counterpart_id=OPERATOR_B,
            )
        )
        gateway.script_users(TelegramUser(id=OPERATOR_B, first_name="Someone", last_name="Else"))

        await harness.sync_chats().execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert chat.chat_type is ChatType.PRIVATE
        assert len(await harness.stored_contacts(ACCOUNT_A)) == 1


class TestRepeatedChatSync:
    async def test_it_creates_no_duplicates(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(PRIVATE_WITH_ADA, GROUP)
        gateway.script_users(ADA)
        use_case = harness.sync_chats()

        await use_case.execute(gateway)
        report = await use_case.execute(gateway)

        assert report.unchanged == 2
        assert len(await harness.stored_chats(ACCOUNT_A)) == 2
        assert len(await harness.stored_contacts(ACCOUNT_A)) == 1

    async def test_it_writes_nothing_the_second_time(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(PRIVATE_WITH_ADA, GROUP)
        gateway.script_users(ADA)
        use_case = harness.sync_chats()
        await use_case.execute(gateway)
        before = harness.commits

        await use_case.execute(gateway)

        assert harness.commits == before

    async def test_it_does_not_move_the_update_time(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(GROUP)
        use_case = harness.sync_chats()
        await use_case.execute(gateway)

        harness.clock.advance(timedelta(hours=1))
        await use_case.execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert chat.updated_at == NOW


class TestChangedChatData:
    async def test_a_renamed_group_is_retitled(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(GROUP)
        use_case = harness.sync_chats()
        await use_case.execute(gateway)

        gateway.script_chats(
            TelegramChatInfo(id=GROUP.id, chat_type=ChatType.SUPERGROUP, title="Platform")
        )
        report = await use_case.execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert report.updated == 1
        assert chat.title == "Platform"

    async def test_a_renamed_counterpart_updates_the_contact_not_the_chat(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # A private chat has no title of its own: its name is the contact's,
        # stored once, on the contact.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(PRIVATE_WITH_ADA)
        gateway.script_users(ADA)
        use_case = harness.sync_chats()
        await use_case.execute(gateway)

        gateway.script_users(TelegramUser(id=ADA.id, first_name="Ada", last_name="Byron"))
        report = await use_case.execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        (contact,) = await harness.stored_contacts(ACCOUNT_A)
        assert report.updated == 1
        assert contact.display_name == "Ada Byron"
        assert chat.title is None

    async def test_a_kind_that_changed_underneath_us_is_left_alone(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # This model has no transition for it, and guessing one would rewrite
        # history the operator can see.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(GROUP)
        use_case = harness.sync_chats()
        await use_case.execute(gateway)

        gateway.script_chats(
            TelegramChatInfo(id=GROUP.id, chat_type=ChatType.CHANNEL, title="Engineering")
        )
        report = await use_case.execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert chat.chat_type is ChatType.SUPERGROUP
        assert report.skipped == 1
        assert "left alone" in report.problems[0].reason


class TestChatSyncRespectsOperatorDecisions:
    async def test_a_chat_with_a_deleted_contact_is_not_recreated(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        contact = await harness.add_contact(ACCOUNT_A, ADA)
        contacts = harness.contacts(harness.unit_of_work(), ACCOUNT_A)
        await contacts.update(contact.deleted(NOW))
        gateway.script_chats(PRIVATE_WITH_ADA)
        gateway.script_users(ADA)

        report = await harness.sync_chats().execute(gateway)

        assert report.skipped == 1
        assert await harness.stored_chats(ACCOUNT_A) == []

    async def test_a_chat_gone_from_telegram_is_kept(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(GROUP)
        use_case = harness.sync_chats()
        await use_case.execute(gateway)

        gateway.script_chats()
        await use_case.execute(gateway)

        assert len(await harness.stored_chats(ACCOUNT_A)) == 1


class TestChatsTelegramDescribesBadly:
    async def test_a_private_chat_with_nobody_in_it_is_reported(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(
            TelegramChatInfo(id=TelegramChatId(77), chat_type=ChatType.PRIVATE, title="?")
        )

        report = await harness.sync_chats().execute(gateway)

        assert report.skipped == 1
        assert "no counterpart" in report.problems[0].reason
        assert await harness.stored_chats(ACCOUNT_A) == []

    async def test_an_unreadable_counterpart_is_reported(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # A deleted Telegram account: the chat is still listed, the person is
        # no longer resolvable.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(PRIVATE_WITH_ADA)

        report = await harness.sync_chats().execute(gateway)

        assert report.skipped == 1
        assert "could not be read" in report.problems[0].reason

    async def test_one_bad_chat_does_not_cost_the_others(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # The same judgement the adapter makes about a chat that vanishes
        # mid-listing: one problem must not lose the rest.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(
            TelegramChatInfo(id=TelegramChatId(77), chat_type=ChatType.PRIVATE, title="?"),
            PRIVATE_WITH_ADA,
            GROUP,
        )
        gateway.script_users(ADA)

        report = await harness.sync_chats().execute(gateway)

        assert report.created == 2
        assert report.skipped == 1
        assert len(await harness.stored_chats(ACCOUNT_A)) == 2

    async def test_an_untitled_group_still_gets_stored(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(
            TelegramChatInfo(id=TelegramChatId(-42), chat_type=ChatType.GROUP, title="")
        )

        await harness.sync_chats().execute(gateway)

        (chat,) = await harness.stored_chats(ACCOUNT_A)
        assert chat.title == "Chat -42"

    async def test_a_title_too_long_to_store_is_reported(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_chats(
            TelegramChatInfo(id=GROUP.id, chat_type=ChatType.GROUP, title="x" * 300)
        )

        report = await harness.sync_chats().execute(gateway)

        assert report.skipped == 1
        assert not report.is_clean
        assert await harness.stored_chats(ACCOUNT_A) == []


class TestChatSyncOwnership:
    async def test_a_gateway_for_another_account_is_refused(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A)
        await harness.add_account(ACCOUNT_B, OPERATOR_B)

        with pytest.raises(AuthorizationError, match="bound to account"):
            await harness.sync_chats().execute(gateway, ACCOUNT_B)

    async def test_a_chat_and_its_contact_always_share_an_account(self, harness: _Harness) -> None:
        # ADR-043's rule, and the one this slice most had to preserve. It holds
        # structurally: both repositories are scoped to the account this run
        # resolved, so no other identifier can arrive.
        await harness.add_account(ACCOUNT_A, OPERATOR_A)
        await harness.add_account(ACCOUNT_B, OPERATOR_B)

        for account, operator in ((ACCOUNT_A, OPERATOR_A), (ACCOUNT_B, OPERATOR_B)):
            fake = FakeTelegramGateway(
                account,
                user=TelegramUser(id=operator, first_name="Me"),
                starts_authorized=True,
            )
            await fake.connect()
            fake.script_chats(PRIVATE_WITH_ADA)
            fake.script_users(ADA)
            await harness.sync_chats().execute(fake, account)
            await fake.disconnect()

        for account in (ACCOUNT_A, ACCOUNT_B):
            (chat,) = await harness.stored_chats(account)
            contacts = {c.id: c for c in await harness.stored_contacts(account)}
            assert chat.account_id == account
            assert chat.contact_id in contacts
            assert contacts[chat.contact_id].account_id == account

    async def test_two_accounts_record_the_same_telegram_chat_separately(
        self, harness: _Harness
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A)
        await harness.add_account(ACCOUNT_B, OPERATOR_B)

        for account, operator in ((ACCOUNT_A, OPERATOR_A), (ACCOUNT_B, OPERATOR_B)):
            fake = FakeTelegramGateway(
                account,
                user=TelegramUser(id=operator, first_name="Me"),
                starts_authorized=True,
            )
            await fake.connect()
            fake.script_chats(GROUP)
            await harness.sync_chats().execute(fake, account)
            await fake.disconnect()

        (from_a,) = await harness.stored_chats(ACCOUNT_A)
        (from_b,) = await harness.stored_chats(ACCOUNT_B)
        assert from_a.id != from_b.id
        assert from_a.telegram_chat_id == from_b.telegram_chat_id


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


class _UnreachableGateway(FakeTelegramGateway):
    """A gateway that reaches Telegram and finds it gone."""

    __slots__ = ()

    async def list_chats(self, *, limit: int = 200) -> tuple[TelegramChatInfo, ...]:
        """Fail the way an unreachable Telegram does."""
        del limit
        msg = "Telegram is unreachable"
        raise TelegramError(msg, user_message="Telegram could not be reached.")

    async def list_contacts(self, *, limit: int = 1000) -> tuple[TelegramUser, ...]:
        """Fail the way an unreachable Telegram does."""
        del limit
        msg = "Telegram is unreachable"
        raise TelegramError(msg, user_message="Telegram could not be reached.")


class _FailingContactRepository(_RegisteringContactRepository):
    """Refuses to write one particular person, as a constraint would."""

    __slots__ = ("_refuse",)

    def __init__(
        self,
        store: InMemoryContactStore,
        account_id: AccountId,
        chat_store: InMemoryChatStore,
        refuse: TelegramUserId,
    ) -> None:
        """Bind to both stores and remember whom to refuse."""
        super().__init__(store, account_id, chat_store)
        self._refuse = refuse

    async def add(self, contact: Contact) -> None:
        """Persist a contact, unless it is the one this repository refuses."""
        if contact.telegram_user_id == self._refuse:
            msg = f"contacts.telegram_user_id {int(self._refuse)}"
            raise DomainValidationError(msg, user_message="That row was refused.")
        await super().add(contact)


class _BrokenDatabaseRepository(_RegisteringContactRepository):
    """A repository whose database has gone away."""

    __slots__ = ()

    async def get_by_telegram_id(
        self, telegram_user_id: TelegramUserId, *, include_deleted: bool = False
    ) -> Contact | None:
        """Fail the way an unreachable database does."""
        del telegram_user_id, include_deleted
        msg = "The database file is gone"
        raise DatabaseUnavailableError(msg, user_message="The database could not be opened.")


class TestTelegramUnavailable:
    async def test_a_chat_run_ends_rather_than_returning_a_partial_report(
        self, harness: _Harness
    ) -> None:
        # If Telegram is unreachable, the next chat will not fare better. A
        # report claiming zero chats would look exactly like an empty account.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        fake = _UnreachableGateway(ACCOUNT_A, user=OPERATOR_USER, starts_authorized=True)
        await fake.connect()

        with pytest.raises(TelegramError, match="unreachable"):
            await harness.sync_chats().execute(fake)

        await fake.disconnect()

    async def test_a_contact_run_ends_too(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        fake = _UnreachableGateway(ACCOUNT_A, user=OPERATOR_USER, starts_authorized=True)
        await fake.connect()

        with pytest.raises(TelegramError):
            await harness.sync_contacts().execute(fake)

        await fake.disconnect()

    async def test_nothing_is_written(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        fake = _UnreachableGateway(ACCOUNT_A, user=OPERATOR_USER, starts_authorized=True)
        await fake.connect()

        with pytest.raises(TelegramError):
            await harness.sync_chats().execute(fake)
        await fake.disconnect()

        assert harness.commits == 0

    async def test_a_gateway_that_is_not_signed_in_is_refused(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        fake = FakeTelegramGateway(ACCOUNT_A, user=OPERATOR_USER)
        await fake.connect()

        with pytest.raises(AuthorizationError):
            await harness.sync_chats().execute(fake)

        await fake.disconnect()


class TestPartialBatchFailure:
    async def test_the_run_continues_past_one_refused_row(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(ADA, GRACE)
        harness.contacts = lambda _uow, account_id: _FailingContactRepository(  # type: ignore[method-assign]
            harness.contact_store, account_id, harness.chat_store, ADA.id
        )

        report = await harness.sync_contacts().execute(gateway)

        assert report.created == 1
        assert report.skipped == 1
        assert {c.telegram_user_id for c in await harness.stored_contacts(ACCOUNT_A)} == {GRACE.id}

    async def test_the_refusal_is_reported_with_a_safe_message(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # The developer message names a column and a value; the report carries
        # the user-facing sentence, because a report is printed and logged.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(ADA)
        harness.contacts = lambda _uow, account_id: _FailingContactRepository(  # type: ignore[method-assign]
            harness.contact_store, account_id, harness.chat_store, ADA.id
        )

        report = await harness.sync_contacts().execute(gateway)

        assert report.problems == (SyncProblem(f"user {int(ADA.id)}", "That row was refused."),)

    async def test_a_broken_database_ends_the_run(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Not a per-item problem: every remaining item would meet the same wall,
        # and a report listing two hundred identical failures helps nobody.
        await harness.add_account(ACCOUNT_A, OPERATOR_A, is_active=True)
        gateway.script_contacts(ADA, GRACE)
        harness.contacts = lambda _uow, account_id: _BrokenDatabaseRepository(  # type: ignore[method-assign]
            harness.contact_store, account_id, harness.chat_store
        )

        with pytest.raises(DatabaseUnavailableError):
            await harness.sync_contacts().execute(gateway)


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


@pytest.fixture
async def stored(container: Container) -> AsyncIterator[Container]:
    """A container over a real SQLite file.

    Closed with ``aclose``: the database owns a worker thread and a connection,
    and the synchronous close cannot shut either down.
    """
    try:
        yield container
    finally:
        await container.aclose()


async def _prepare(container: Container) -> Account:
    """Create the schema and one active account."""
    await container.start_database()
    return await container.create_account().execute(
        CreateAccountRequest(telegram_user_id=int(OPERATOR_A), display_name="me")
    )


class TestAgainstARealDatabase:
    """The transaction boundary, which no in-memory fake can demonstrate.

    The in-memory repositories write through immediately, so a rollback is
    invisible to them -- and rollback is the whole of what "no half-written
    results" means.
    """

    async def test_a_first_run_stores_the_chat_and_the_person(self, stored: Container) -> None:
        account = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, user=OPERATOR_USER, starts_authorized=True)
        await gateway.connect()
        gateway.script_chats(PRIVATE_WITH_ADA, SAVED, GROUP)
        gateway.script_users(ADA)

        report = await stored.sync_chats().execute(gateway)
        await gateway.disconnect()

        chats = await stored.list_chats().execute(PageRequest(limit=10))
        contacts = await stored.list_contacts().execute(PageRequest(limit=10))
        assert report.created == 3
        assert {chat.chat_type for chat in chats.items} == {
            ChatType.PRIVATE,
            ChatType.SAVED,
            ChatType.SUPERGROUP,
        }
        assert len(contacts.items) == 1

    async def test_a_second_run_changes_nothing(self, stored: Container) -> None:
        account = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, user=OPERATOR_USER, starts_authorized=True)
        await gateway.connect()
        gateway.script_chats(PRIVATE_WITH_ADA, SAVED, GROUP)
        gateway.script_users(ADA)
        await stored.sync_chats().execute(gateway)

        report = await stored.sync_chats().execute(gateway)
        await gateway.disconnect()

        assert report.unchanged == 3
        assert report.changed == 0

    async def test_a_failed_unit_leaves_nothing_behind(self, stored: Container) -> None:
        # A contact who already has a private chat, and a *second* chat with
        # them: the partial unique index refuses it. The contact rename in the
        # same transaction must go with it, or the run has left a record
        # nothing asked for.
        account = await _prepare(stored)
        contact = await stored.create_contact().execute(
            telegram_user_id=int(ADA.id), display_name="Ada"
        )
        await stored.open_private_chat().execute(
            contact_id=int(contact.id), telegram_chat_id=int(PRIVATE_WITH_ADA.id)
        )

        gateway = FakeTelegramGateway(account.id, user=OPERATOR_USER, starts_authorized=True)
        await gateway.connect()
        gateway.script_chats(
            TelegramChatInfo(
                id=TelegramChatId(999),
                chat_type=ChatType.PRIVATE,
                title="Ada Lovelace",
                counterpart_id=ADA.id,
            )
        )
        gateway.script_users(ADA)
        report = await stored.sync_chats().execute(gateway)
        await gateway.disconnect()

        after = await stored.get_contact().execute(int(contact.id))
        assert report.skipped == 1
        assert not report.is_clean
        assert after is not None
        assert after.display_name == "Ada"
        assert len((await stored.list_chats().execute(PageRequest(limit=10))).items) == 1

    async def test_the_composite_foreign_key_holds(self, stored: Container) -> None:
        # The database enforces ADR-043 independently of the application check.
        account = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, user=OPERATOR_USER, starts_authorized=True)
        await gateway.connect()
        gateway.script_chats(PRIVATE_WITH_ADA)
        gateway.script_users(ADA)
        await stored.sync_chats().execute(gateway)
        await gateway.disconnect()

        (chat,) = (await stored.list_chats().execute(PageRequest(limit=10))).items
        (contact,) = (await stored.list_contacts().execute(PageRequest(limit=10))).items
        assert chat.account_id == contact.account_id == account.id
        assert chat.contact_id == contact.id
