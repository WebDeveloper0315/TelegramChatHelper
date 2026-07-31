"""The Telegram gateway, run against both implementations.

The fake is the second implementation, not a stand-in for one. Every obligation
here runs against it *and* against ``TdlibGateway`` driven by a scripted TDLib —
which is the only way the fake can be trusted in the tests that use it
everywhere else.

No test here needs a Telegram account, a network or a real native library.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes.tdjson import AuthorizingTdjson
from tests.fakes.telegram_gateway import (
    ACCEPTED_CODE,
    ACCEPTED_PASSWORD,
    FakeTelegramGateway,
    ScriptedHandler,
)
from tgassist.domain.errors import (
    AuthorizationError,
    TdlibNotRunningError,
)
from tgassist.domain.model.chat import ChatType
from tgassist.domain.model.identifiers import (
    AccountId,
    TelegramChatId,
    TelegramMessageId,
    TelegramUserId,
)
from tgassist.domain.model.secret import SecretValue
from tgassist.domain.model.session import AuthorizationState, ConnectionState
from tgassist.domain.model.telegram import (
    NewMessage,
    TelegramChatInfo,
    TelegramMessage,
    TelegramUser,
)
from tgassist.domain.ports.telegram_gateway import TelegramGateway
from tgassist.infrastructure.telegram.client import TdjsonClient
from tgassist.infrastructure.telegram.gateway import GatewaySettings, TdlibGateway

ACCOUNT = AccountId(1)
TIMEOUT = 5.0


def _settings(tmp_path: Path) -> GatewaySettings:
    """Parameters for a gateway that never reaches a network."""
    return GatewaySettings(
        api_id=12345,
        api_hash=SecretValue("0123456789abcdef0123456789abcdef"),
        session_path=tmp_path / "session",
        database_encryption_key=SecretValue("test-session-key"),
    )


@dataclass
class GatewaySubject:
    """One implementation, plus what it took to build it."""

    gateway: TelegramGateway
    label: str


@pytest.fixture
async def fake_subject() -> AsyncIterator[GatewaySubject]:
    """The hand-written fake."""
    gateway = FakeTelegramGateway(ACCOUNT)
    try:
        yield GatewaySubject(gateway=gateway, label="fake")
    finally:
        await gateway.disconnect()


@pytest.fixture
async def tdlib_subject(tmp_path: Path) -> AsyncIterator[GatewaySubject]:
    """The real adapter, driven by a TDLib that runs the login state machine."""
    library = AuthorizingTdjson()
    gateway = TdlibGateway(
        ACCOUNT,
        TdjsonClient(library),
        _settings(tmp_path),
        state_timeout=TIMEOUT,
        startup_timeout=TIMEOUT,
    )
    try:
        yield GatewaySubject(gateway=gateway, label="tdlib")
    finally:
        await gateway.disconnect()


@pytest.fixture(params=["fake", "tdlib"])
def subject(request: pytest.FixtureRequest) -> GatewaySubject:
    """Both implementations.

    Synchronous on purpose: resolving an async-generator fixture through
    ``getfixturevalue`` from an async fixture hands back an unawaited coroutine.
    """
    name = "fake_subject" if request.param == "fake" else "tdlib_subject"
    resolved: GatewaySubject = request.getfixturevalue(name)
    return resolved


class TestGatewayContract:
    """Obligations both implementations must satisfy."""

    def test_satisfies_the_port(self, subject: GatewaySubject) -> None:
        assert isinstance(subject.gateway, TelegramGateway)

    def test_exposes_its_account(self, subject: GatewaySubject) -> None:
        assert subject.gateway.account_id == ACCOUNT

    async def test_starts_disconnected(self, subject: GatewaySubject) -> None:
        assert not await subject.gateway.is_connected()

    async def test_connect_makes_it_connected(self, subject: GatewaySubject) -> None:
        await subject.gateway.connect()

        assert await subject.gateway.is_connected() or (
            await subject.gateway.connection_state() is ConnectionState.OFFLINE
        )

    async def test_connect_is_idempotent(self, subject: GatewaySubject) -> None:
        await subject.gateway.connect()
        await subject.gateway.connect()

        assert await subject.gateway.authorization_state() is not AuthorizationState.LOGGED_OUT

    async def test_disconnect_is_idempotent(self, subject: GatewaySubject) -> None:
        # Every cleanup path ends here, including ones that never connected.
        await subject.gateway.disconnect()
        await subject.gateway.disconnect()

        assert not await subject.gateway.is_connected()

    async def test_connecting_reaches_a_state_that_asks_for_something(
        self, subject: GatewaySubject
    ) -> None:
        await subject.gateway.connect()

        assert await subject.gateway.authorization_state() is AuthorizationState.WAITING_PHONE

    async def test_get_me_without_a_connection_is_refused(self, subject: GatewaySubject) -> None:
        with pytest.raises(TdlibNotRunningError):
            await subject.gateway.get_me()

    async def test_get_me_before_authorizing_is_refused(self, subject: GatewaySubject) -> None:
        # A caller error, and answering it with a Telegram code would hide that.
        await subject.gateway.connect()

        with pytest.raises(AuthorizationError):
            await subject.gateway.get_me()

    async def test_authorization_without_a_connection_is_refused(
        self, subject: GatewaySubject
    ) -> None:
        with pytest.raises(TdlibNotRunningError):
            await subject.gateway.start_authorization(ScriptedHandler())

    def test_there_is_no_typing_indicator_method(self, subject: GatewaySubject) -> None:
        # ADR-023 section 2, expressed structurally: what cannot be called
        # cannot be used to imitate a human being.
        names = dir(subject.gateway)

        assert not [name for name in names if "typing" in name.lower()]
        assert not [name for name in names if "action" in name.lower()]


class TestAuthorizationFlow:
    """Signing in, over both implementations."""

    async def test_a_clean_login_reaches_ready(self, subject: GatewaySubject) -> None:
        await subject.gateway.connect()

        await subject.gateway.start_authorization(ScriptedHandler())

        assert await subject.gateway.authorization_state() is AuthorizationState.READY

    async def test_the_handler_is_asked_for_each_step(self, subject: GatewaySubject) -> None:
        await subject.gateway.connect()
        handler = ScriptedHandler()

        await subject.gateway.start_authorization(handler)

        assert AuthorizationState.WAITING_PHONE in handler.states
        assert AuthorizationState.WAITING_CODE in handler.states

    async def test_get_me_after_login(self, subject: GatewaySubject) -> None:
        await subject.gateway.connect()
        await subject.gateway.start_authorization(ScriptedHandler())

        user = await subject.gateway.get_me()

        assert int(user.id) > 0

    async def test_authorizing_twice_returns_immediately(self, subject: GatewaySubject) -> None:
        # The ordinary case after a restart: a caller need not check first.
        await subject.gateway.connect()
        await subject.gateway.start_authorization(ScriptedHandler())

        exhausted = ScriptedHandler(phones=[], codes=[], passwords=[])
        await subject.gateway.start_authorization(exhausted)

        assert exhausted.states == []

    async def test_a_wrong_code_is_retried_when_the_handler_agrees(
        self, subject: GatewaySubject
    ) -> None:
        await subject.gateway.connect()
        handler = ScriptedHandler(codes=["00000", ACCEPTED_CODE])

        await subject.gateway.start_authorization(handler)

        assert len(handler.errors) == 1
        assert isinstance(handler.errors[0], AuthorizationError)
        assert await subject.gateway.authorization_state() is AuthorizationState.READY

    async def test_a_wrong_code_aborts_when_the_handler_declines(
        self, subject: GatewaySubject
    ) -> None:
        await subject.gateway.connect()
        handler = ScriptedHandler(codes=["00000"], retry=False)

        with pytest.raises(AuthorizationError):
            await subject.gateway.start_authorization(handler)

        assert await subject.gateway.authorization_state() is not AuthorizationState.READY

    async def test_logout_leaves_the_account_signed_out(self, subject: GatewaySubject) -> None:
        await subject.gateway.connect()
        await subject.gateway.start_authorization(ScriptedHandler())

        await subject.gateway.logout()

        assert await subject.gateway.authorization_state() is AuthorizationState.LOGGED_OUT


class TestTwoFactorFlow:
    """Two-step verification, over both implementations."""

    @pytest.fixture(params=["fake", "tdlib"])
    async def protected(
        self, request: pytest.FixtureRequest, tmp_path: Path
    ) -> AsyncIterator[TelegramGateway]:
        """A gateway for an account with two-step verification enabled."""
        gateway: TelegramGateway
        if request.param == "fake":
            gateway = FakeTelegramGateway(ACCOUNT, requires_password=True)
        else:
            gateway = TdlibGateway(
                ACCOUNT,
                TdjsonClient(AuthorizingTdjson(requires_password=True)),
                _settings(tmp_path),
                state_timeout=TIMEOUT,
                startup_timeout=TIMEOUT,
            )
        try:
            yield gateway
        finally:
            await gateway.disconnect()

    async def test_the_password_is_requested_and_accepted(self, protected: TelegramGateway) -> None:
        await protected.connect()
        handler = ScriptedHandler()

        await protected.start_authorization(handler)

        assert AuthorizationState.WAITING_PASSWORD in handler.states
        assert await protected.authorization_state() is AuthorizationState.READY

    async def test_a_wrong_password_is_retried(self, protected: TelegramGateway) -> None:
        await protected.connect()
        handler = ScriptedHandler(passwords=["wrong", ACCEPTED_PASSWORD])

        await protected.start_authorization(handler)

        assert len(handler.errors) == 1
        assert await protected.authorization_state() is AuthorizationState.READY


class TestRestoredSession:
    """A session that is still valid asks for nothing."""

    @pytest.fixture(params=["fake", "tdlib"])
    async def restored(
        self, request: pytest.FixtureRequest, tmp_path: Path
    ) -> AsyncIterator[TelegramGateway]:
        """A gateway whose stored session TDLib still accepts."""
        gateway: TelegramGateway
        if request.param == "fake":
            gateway = FakeTelegramGateway(ACCOUNT, starts_authorized=True)
        else:
            gateway = TdlibGateway(
                ACCOUNT,
                TdjsonClient(AuthorizingTdjson(starts_authorized=True)),
                _settings(tmp_path),
                state_timeout=TIMEOUT,
                startup_timeout=TIMEOUT,
            )
        try:
            yield gateway
        finally:
            await gateway.disconnect()

    async def test_connecting_is_enough(self, restored: TelegramGateway) -> None:
        # This is what "the session survives a restart" means in practice.
        await restored.connect()

        assert await restored.authorization_state() is AuthorizationState.READY

    async def test_no_credential_is_requested(self, restored: TelegramGateway) -> None:
        await restored.connect()
        handler = ScriptedHandler(phones=[], codes=[], passwords=[])

        await restored.start_authorization(handler)

        assert handler.states == []
        assert handler.errors == []

    async def test_get_me_works_straight_away(self, restored: TelegramGateway) -> None:
        await restored.connect()

        assert int((await restored.get_me()).id) > 0


class TestTheFakeMatchesTheAdapter:
    """The two implementations agree where a test would notice."""

    async def test_both_report_the_same_default_user_shape(self, tmp_path: Path) -> None:
        fake = FakeTelegramGateway(ACCOUNT, starts_authorized=True)
        await fake.connect()
        from_fake = await fake.get_me()
        await fake.disconnect()

        real = TdlibGateway(
            ACCOUNT,
            TdjsonClient(AuthorizingTdjson(starts_authorized=True)),
            _settings(tmp_path),
            state_timeout=TIMEOUT,
            startup_timeout=TIMEOUT,
        )
        await real.connect()
        from_adapter = await real.get_me()
        await real.disconnect()

        assert from_fake == from_adapter


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

CHAT_A = TelegramChatInfo(
    id=TelegramChatId(100),
    chat_type=ChatType.PRIVATE,
    title="Ada Lovelace",
    counterpart_id=TelegramUserId(2002),
    unread_count=3,
    last_message_id=TelegramMessageId(50),
)
CHAT_B = TelegramChatInfo(
    id=TelegramChatId(200),
    chat_type=ChatType.SUPERGROUP,
    title="Engineering",
    last_message_id=TelegramMessageId(9),
)
CHAT_EMPTY = TelegramChatInfo(id=TelegramChatId(300), chat_type=ChatType.GROUP, title="Nobody here")

SENT_AT = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def message(number: int, *, text: str | None = None) -> TelegramMessage:
    """Build a message in CHAT_A with an identifier ordering it."""
    return TelegramMessage(
        id=TelegramMessageId(number),
        chat_id=CHAT_A.id,
        sender_id=TelegramUserId(2002),
        sent_at=SENT_AT + timedelta(minutes=number),
        text=text if text is not None else f"message {number}",
    )


HISTORY = tuple(message(number) for number in range(1, 26))

#: The address book. ADA is also CHAT_A's counterpart, so the two populations
#: overlap without either containing the other.
ADA = TelegramUser(
    id=TelegramUserId(2002), first_name="Ada", last_name="Lovelace", username="ada_lovelace"
)
GRACE = TelegramUser(id=TelegramUserId(3003), first_name="Grace", username="ghopper")

#: Resolvable but never saved: somebody who messaged this account. Proves that
#: the address book and "everyone Telegram can resolve" are different sets.
STRANGER = TelegramUser(id=TelegramUserId(4004), first_name="Sam")


@pytest.fixture(params=["fake", "tdlib"])
async def readable(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[TelegramGateway]:
    """An authorized gateway with three chats and a history in one of them."""
    gateway: TelegramGateway
    if request.param == "fake":
        fake = FakeTelegramGateway(ACCOUNT, starts_authorized=True)
        fake.script_chats(CHAT_A, CHAT_B, CHAT_EMPTY)
        fake.script_history(CHAT_A.id, *HISTORY)
        fake.script_contacts(ADA, GRACE)
        fake.script_users(STRANGER)
        gateway = fake
    else:
        library = AuthorizingTdjson(starts_authorized=True)
        library.script_chats(CHAT_A, CHAT_B, CHAT_EMPTY)
        library.script_history(CHAT_A.id, *HISTORY)
        library.script_contacts(ADA, GRACE)
        library.script_users(STRANGER)
        gateway = TdlibGateway(
            ACCOUNT,
            TdjsonClient(library),
            _settings(tmp_path),
            state_timeout=TIMEOUT,
            startup_timeout=TIMEOUT,
        )
    await gateway.connect()
    try:
        yield gateway
    finally:
        await gateway.disconnect()


class TestListingChats:
    """Obligations both implementations must satisfy."""

    async def test_it_returns_every_chat(self, readable: TelegramGateway) -> None:
        chats = await readable.list_chats()

        assert {chat.id for chat in chats} == {CHAT_A.id, CHAT_B.id, CHAT_EMPTY.id}

    async def test_it_preserves_telegram_ordering(self, readable: TelegramGateway) -> None:
        # Telegram's order is its opinion of recency; recomputing it locally
        # would need data this does not fetch.
        chats = await readable.list_chats()

        assert [chat.id for chat in chats] == [CHAT_A.id, CHAT_B.id, CHAT_EMPTY.id]

    async def test_a_private_chat_names_its_counterpart(self, readable: TelegramGateway) -> None:
        chats = {chat.id: chat for chat in await readable.list_chats()}

        assert chats[CHAT_A.id].is_private
        assert chats[CHAT_A.id].counterpart_id == TelegramUserId(2002)

    async def test_a_group_names_none(self, readable: TelegramGateway) -> None:
        chats = {chat.id: chat for chat in await readable.list_chats()}

        assert not chats[CHAT_B.id].is_private
        assert chats[CHAT_B.id].counterpart_id is None

    async def test_it_carries_the_unread_count_and_last_message(
        self, readable: TelegramGateway
    ) -> None:
        chats = {chat.id: chat for chat in await readable.list_chats()}

        assert chats[CHAT_A.id].unread_count == 3
        assert chats[CHAT_A.id].last_message_id == TelegramMessageId(50)

    async def test_an_empty_chat_says_so(self, readable: TelegramGateway) -> None:
        chats = {chat.id: chat for chat in await readable.list_chats()}

        assert chats[CHAT_EMPTY.id].is_empty
        assert chats[CHAT_EMPTY.id].last_message_id is None

    async def test_the_limit_is_a_ceiling(self, readable: TelegramGateway) -> None:
        assert len(await readable.list_chats(limit=2)) == 2

    async def test_asking_for_more_than_exists_is_not_an_error(
        self, readable: TelegramGateway
    ) -> None:
        assert len(await readable.list_chats(limit=1000)) == 3


class TestGettingOneChat:
    async def test_it_returns_the_chat(self, readable: TelegramGateway) -> None:
        found = await readable.get_chat(CHAT_A.id)

        assert found is not None
        assert found.title == "Ada Lovelace"

    async def test_an_invisible_chat_is_none_not_an_error(self, readable: TelegramGateway) -> None:
        # An ordinary answer to "does this exist for me": the account left it,
        # or never had access.
        assert await readable.get_chat(TelegramChatId(999_999)) is None


class TestListingContacts:
    """Obligations both implementations must satisfy."""

    async def test_it_returns_the_address_book(self, readable: TelegramGateway) -> None:
        assert {user.id for user in await readable.list_contacts()} == {ADA.id, GRACE.id}

    async def test_it_preserves_telegram_ordering(self, readable: TelegramGateway) -> None:
        assert [user.id for user in await readable.list_contacts()] == [ADA.id, GRACE.id]

    async def test_it_excludes_somebody_never_saved(self, readable: TelegramGateway) -> None:
        # The address book is not "everyone Telegram can resolve", which is why
        # listing chats and listing contacts are two calls rather than one.
        assert STRANGER.id not in {user.id for user in await readable.list_contacts()}

    async def test_it_carries_names_and_handles(self, readable: TelegramGateway) -> None:
        book = {user.id: user for user in await readable.list_contacts()}

        assert book[ADA.id].first_name == "Ada"
        assert book[ADA.id].last_name == "Lovelace"
        assert book[ADA.id].username == "ada_lovelace"

    async def test_a_missing_family_name_is_none(self, readable: TelegramGateway) -> None:
        book = {user.id: user for user in await readable.list_contacts()}

        assert book[GRACE.id].last_name is None

    async def test_the_limit_is_a_ceiling(self, readable: TelegramGateway) -> None:
        assert len(await readable.list_contacts(limit=1)) == 1

    async def test_asking_for_more_than_exists_is_not_an_error(
        self, readable: TelegramGateway
    ) -> None:
        assert len(await readable.list_contacts(limit=1000)) == 2


class TestGettingOneContact:
    async def test_it_returns_the_user(self, readable: TelegramGateway) -> None:
        found = await readable.get_contact(ADA.id)

        assert found is not None
        assert found.display_name == "Ada Lovelace"

    async def test_it_resolves_somebody_never_saved(self, readable: TelegramGateway) -> None:
        # A private chat's counterpart is reached this way, and they need never
        # have been added to the address book.
        found = await readable.get_contact(STRANGER.id)

        assert found is not None
        assert found.username is None

    async def test_an_invisible_user_is_none_not_an_error(self, readable: TelegramGateway) -> None:
        assert await readable.get_contact(TelegramUserId(999_999)) is None


# ---------------------------------------------------------------------------
# The update stream
# ---------------------------------------------------------------------------


@pytest.fixture(params=["fake", "tdlib"])
async def streaming(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[tuple[TelegramGateway, object]]:
    """An authorized gateway, plus the means to make Telegram report something.

    The second element is a callable taking a :class:`TelegramMessage`. For the
    fake it pushes onto its queue; for the adapter it renders an
    ``updateNewMessage`` frame that the real mapper then reads. Both go through
    the same code a real arriving message would.
    """
    if request.param == "fake":
        fake = FakeTelegramGateway(ACCOUNT, starts_authorized=True)
        await fake.connect()
        try:
            yield fake, fake.push_message
        finally:
            await fake.disconnect()
        return

    library = AuthorizingTdjson(starts_authorized=True)
    real = TdlibGateway(
        ACCOUNT,
        TdjsonClient(library),
        _settings(tmp_path),
        state_timeout=TIMEOUT,
        startup_timeout=TIMEOUT,
    )
    await real.connect()
    try:
        yield real, library.announce_message
    finally:
        await real.disconnect()


def arriving(number: int) -> TelegramMessage:
    """Build a message that could arrive in CHAT_A."""
    return TelegramMessage(
        id=TelegramMessageId(1000 + number),
        chat_id=CHAT_A.id,
        sender_id=TelegramUserId(2002),
        sent_at=SENT_AT + timedelta(minutes=number),
        text=f"arriving {number}",
    )


async def take(gateway: TelegramGateway, count: int) -> list[TelegramMessage]:
    """Take a fixed number of messages off a gateway's update stream."""
    taken: list[TelegramMessage] = []
    stream = gateway.updates()
    try:
        for _ in range(count):
            update = await asyncio.wait_for(anext(stream), TIMEOUT)
            assert isinstance(update, NewMessage)
            taken.append(update.message)
    finally:
        await stream.aclose()
    return taken


class TestTheUpdateStream:
    """Obligations both implementations must satisfy."""

    async def test_an_arriving_message_reaches_the_consumer(
        self, streaming: tuple[TelegramGateway, object]
    ) -> None:
        gateway, report = streaming
        report(arriving(1))  # type: ignore[operator]

        assert await take(gateway, 1) == [arriving(1)]

    async def test_arrival_order_is_preserved(
        self, streaming: tuple[TelegramGateway, object]
    ) -> None:
        # Telegram's order is the only order there is; nothing re-sorts.
        gateway, report = streaming
        for number in (3, 1, 2):
            report(arriving(number))  # type: ignore[operator]

        taken = await take(gateway, 3)

        assert [int(m.id) for m in taken] == [1003, 1001, 1002]

    async def test_updates_are_held_until_somebody_asks(
        self, streaming: tuple[TelegramGateway, object]
    ) -> None:
        # The whole of why a run cannot lose an update to its own start-up: the
        # stream fills from connect, whether or not anything is draining it.
        gateway, report = streaming
        for number in (1, 2, 3):
            report(arriving(number))  # type: ignore[operator]
        await asyncio.sleep(0.05)

        assert len(await take(gateway, 3)) == 3

    async def test_the_message_survives_the_round_trip(
        self, streaming: tuple[TelegramGateway, object]
    ) -> None:
        gateway, report = streaming
        report(arriving(1))  # type: ignore[operator]

        (received,) = await take(gateway, 1)

        assert received.text == "arriving 1"
        assert received.chat_id == CHAT_A.id
        assert received.sender_id == TelegramUserId(2002)

    async def test_a_second_consumer_is_refused(
        self, streaming: tuple[TelegramGateway, object]
    ) -> None:
        # The queue holds one item per update, so two consumers would take turns
        # and each would silently miss what the other took.
        gateway, report = streaming
        report(arriving(1))  # type: ignore[operator]
        first = gateway.updates()
        await asyncio.wait_for(anext(first), TIMEOUT)

        with pytest.raises(TdlibNotRunningError, match="already has a consumer"):
            async for _ in gateway.updates():
                pass

        await first.aclose()

    async def test_disconnecting_ends_the_stream(
        self, streaming: tuple[TelegramGateway, object]
    ) -> None:
        gateway, _report = streaming
        taken: list[object] = []

        async def drain() -> None:
            async for update in gateway.updates():
                taken.append(update)

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.05)
        await gateway.disconnect()

        await asyncio.wait_for(task, TIMEOUT)
        assert taken == []


class TestBothImplementationsStreamIdentically:
    async def test_the_same_updates_arrive_identically(self, tmp_path: Path) -> None:
        sent = [arriving(number) for number in (1, 2, 3)]

        fake = FakeTelegramGateway(ACCOUNT, starts_authorized=True)
        await fake.connect()
        for item in sent:
            fake.push_message(item)
        from_fake = await take(fake, 3)
        await fake.disconnect()

        library = AuthorizingTdjson(starts_authorized=True)
        real = TdlibGateway(
            ACCOUNT,
            TdjsonClient(library),
            _settings(tmp_path),
            state_timeout=TIMEOUT,
            startup_timeout=TIMEOUT,
        )
        await real.connect()
        for item in sent:
            library.announce_message(item)
        from_adapter = await take(real, 3)
        await real.disconnect()

        assert from_fake == from_adapter == sent


class TestFetchingHistory:
    async def test_the_newest_page_comes_first(self, readable: TelegramGateway) -> None:
        page = await readable.fetch_history(CHAT_A.id, limit=10)

        assert [int(m.id) for m in page.messages] == list(range(25, 15, -1))

    async def test_the_page_carries_its_own_boundary(self, readable: TelegramGateway) -> None:
        # A caller that derived it would have to know Telegram returns short
        # pages for reasons of its own.
        page = await readable.fetch_history(CHAT_A.id, limit=10)

        assert page.oldest_message_id == TelegramMessageId(16)
        assert not page.reached_beginning

    async def test_pages_do_not_overlap(self, readable: TelegramGateway) -> None:
        first = await readable.fetch_history(CHAT_A.id, limit=10)
        second = await readable.fetch_history(
            CHAT_A.id, before_message_id=first.oldest_message_id, limit=10
        )

        assert {m.id for m in first.messages} & {m.id for m in second.messages} == set()

    async def test_paging_reaches_every_message_exactly_once(
        self, readable: TelegramGateway
    ) -> None:
        seen: list[int] = []
        cursor: TelegramMessageId | None = None
        for _ in range(10):
            page = await readable.fetch_history(CHAT_A.id, before_message_id=cursor, limit=10)
            seen.extend(int(m.id) for m in page.messages)
            if page.reached_beginning:
                break
            cursor = page.oldest_message_id

        assert sorted(seen) == list(range(1, 26))

    async def test_the_beginning_is_reported_not_inferred(self, readable: TelegramGateway) -> None:
        # The whole reason the boundary is returned: an empty page is how a
        # backfill knows to stop, and inferring it is the classic infinite loop.
        page = await readable.fetch_history(CHAT_A.id, before_message_id=TelegramMessageId(1))

        assert page.is_empty
        assert page.reached_beginning

    async def test_a_chat_with_no_history_is_empty_not_an_error(
        self, readable: TelegramGateway
    ) -> None:
        page = await readable.fetch_history(CHAT_EMPTY.id)

        assert page.is_empty
        assert page.reached_beginning

    async def test_message_content_survives_the_round_trip(self, readable: TelegramGateway) -> None:
        page = await readable.fetch_history(CHAT_A.id, limit=1)

        assert page.messages[0].text == "message 25"
        assert page.messages[0].chat_id == CHAT_A.id
        assert page.messages[0].sender_id == TelegramUserId(2002)


class TestReadsRequireAuthorization:
    """Reading before signing in is a caller error, and says so."""

    @pytest.fixture(params=["fake", "tdlib"])
    async def unauthorized(
        self, request: pytest.FixtureRequest, tmp_path: Path
    ) -> AsyncIterator[TelegramGateway]:
        """A connected gateway that has not signed in."""
        gateway: TelegramGateway
        if request.param == "fake":
            gateway = FakeTelegramGateway(ACCOUNT)
        else:
            gateway = TdlibGateway(
                ACCOUNT,
                TdjsonClient(AuthorizingTdjson()),
                _settings(tmp_path),
                state_timeout=TIMEOUT,
                startup_timeout=TIMEOUT,
            )
        await gateway.connect()
        try:
            yield gateway
        finally:
            await gateway.disconnect()

    async def test_listing_chats(self, unauthorized: TelegramGateway) -> None:
        with pytest.raises(AuthorizationError):
            await unauthorized.list_chats()

    async def test_getting_a_chat(self, unauthorized: TelegramGateway) -> None:
        with pytest.raises(AuthorizationError):
            await unauthorized.get_chat(CHAT_A.id)

    async def test_fetching_history(self, unauthorized: TelegramGateway) -> None:
        with pytest.raises(AuthorizationError):
            await unauthorized.fetch_history(CHAT_A.id)

    async def test_listing_contacts(self, unauthorized: TelegramGateway) -> None:
        with pytest.raises(AuthorizationError):
            await unauthorized.list_contacts()

    async def test_getting_a_contact(self, unauthorized: TelegramGateway) -> None:
        with pytest.raises(AuthorizationError):
            await unauthorized.get_contact(ADA.id)

    async def test_the_update_stream_does_not(self, unauthorized: TelegramGateway) -> None:
        # Deliberately different from the reads: updates are what *tell* a
        # gateway it has become authorized, so a stream that refused before
        # authorization could never deliver the news.
        stream = unauthorized.updates()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(anext(stream), 0.05)
        await stream.aclose()


class TestReadsRequireAConnection:
    """Nothing connects implicitly."""

    @pytest.mark.parametrize(
        ("operation", "argument"),
        [
            ("list_chats", None),
            ("get_chat", CHAT_A.id),
            ("fetch_history", CHAT_A.id),
            ("list_contacts", None),
            ("get_contact", ADA.id),
        ],
    )
    async def test_each_refuses_without_one(
        self, subject: GatewaySubject, operation: str, argument: object
    ) -> None:
        method = getattr(subject.gateway, operation)
        arguments = () if argument is None else (argument,)

        with pytest.raises(TdlibNotRunningError):
            await method(*arguments)


class TestReadsAgreeAcrossImplementations:
    async def test_the_same_chats_round_trip_identically(self, tmp_path: Path) -> None:
        fake = FakeTelegramGateway(ACCOUNT, starts_authorized=True)
        fake.script_chats(CHAT_A, CHAT_B, CHAT_EMPTY)
        await fake.connect()
        from_fake = await fake.list_chats()
        await fake.disconnect()

        library = AuthorizingTdjson(starts_authorized=True)
        library.script_chats(CHAT_A, CHAT_B, CHAT_EMPTY)
        real = TdlibGateway(
            ACCOUNT,
            TdjsonClient(library),
            _settings(tmp_path),
            state_timeout=TIMEOUT,
            startup_timeout=TIMEOUT,
        )
        await real.connect()
        from_adapter = await real.list_chats()
        await real.disconnect()

        assert from_fake == from_adapter

    async def test_the_same_history_pages_identically(self, tmp_path: Path) -> None:
        fake = FakeTelegramGateway(ACCOUNT, starts_authorized=True)
        fake.script_chats(CHAT_A)
        fake.script_history(CHAT_A.id, *HISTORY)
        await fake.connect()
        from_fake = await fake.fetch_history(CHAT_A.id, limit=7)
        await fake.disconnect()

        library = AuthorizingTdjson(starts_authorized=True)
        library.script_chats(CHAT_A)
        library.script_history(CHAT_A.id, *HISTORY)
        real = TdlibGateway(
            ACCOUNT,
            TdjsonClient(library),
            _settings(tmp_path),
            state_timeout=TIMEOUT,
            startup_timeout=TIMEOUT,
        )
        await real.connect()
        from_adapter = await real.fetch_history(CHAT_A.id, limit=7)
        await real.disconnect()

        assert from_fake == from_adapter

    async def test_the_same_address_book_round_trips_identically(self, tmp_path: Path) -> None:
        fake = FakeTelegramGateway(ACCOUNT, starts_authorized=True)
        fake.script_contacts(ADA, GRACE)
        await fake.connect()
        from_fake = await fake.list_contacts()
        await fake.disconnect()

        library = AuthorizingTdjson(starts_authorized=True)
        library.script_contacts(ADA, GRACE)
        real = TdlibGateway(
            ACCOUNT,
            TdjsonClient(library),
            _settings(tmp_path),
            state_timeout=TIMEOUT,
            startup_timeout=TIMEOUT,
        )
        await real.connect()
        from_adapter = await real.list_contacts()
        await real.disconnect()

        assert from_fake == from_adapter
