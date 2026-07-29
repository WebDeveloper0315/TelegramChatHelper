"""Login and logout use cases, and the console handler.

The use cases run against fakes, so they exercise application logic with no
database, no credential backend and no Telegram. The console handler is checked
directly, because what it guarantees — that nothing is retained — is a property
of its shape rather than of any single call.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fakes import AdvanceableClock, InMemorySecretStore
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.session_repository import (
    InMemorySessionRepository,
    InMemorySessionStore,
)
from tests.fakes.telegram_gateway import (
    ACCEPTED_CODE,
    DEFAULT_USER,
    FakeTelegramGateway,
    ScriptedHandler,
)
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.use_cases.authenticate import AuthenticateAccount, LogOutAccount
from tgassist.application.use_cases.session import PrepareSession, session_key_name
from tgassist.domain.errors import AuthorizationError, RecordNotFoundError
from tgassist.domain.model.account import Account
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.session import AuthorizationState, ConnectionState
from tgassist.domain.model.telegram import CodeHint, PasswordHint, TelegramUser
from tgassist.domain.ports.telegram_gateway import RetryDecision
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.presentation.cli.authorization import ConsoleAuthorizationHandler

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
SESSIONS_DIR = Path("/data/sessions")


class _Harness:
    """A use-case environment built entirely from fakes."""

    def __init__(self) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.store = InMemorySessionStore(known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)})
        self.secret_store = InMemorySecretStore()
        self.clock = AdvanceableClock(NOW)
        self.units: list[InMemoryUnitOfWork] = []
        self._factory = InMemoryUnitOfWorkFactory()

    def unit_of_work(self) -> InMemoryUnitOfWork:
        uow = self._factory()
        self.units.append(uow)
        return uow

    def accounts(self, _uow: UnitOfWork) -> InMemoryAccountRepository:
        return self.accounts_repository

    def sessions(self, _uow: UnitOfWork, account_id: AccountId) -> InMemorySessionRepository:
        return InMemorySessionRepository(self.store, account_id)

    async def add_account(
        self,
        account_id: AccountId,
        *,
        is_active: bool = False,
        telegram_user_id: int | None = None,
    ) -> Account:
        account = Account.create(
            account_id=account_id,
            telegram_user_id=TelegramUserId(telegram_user_id or int(DEFAULT_USER.id)),
            display_name=f"account-{int(account_id)}",
            now=NOW,
            is_active=is_active,
        )
        await self.accounts_repository.add(account)
        return account

    def prepare(self) -> PrepareSession:
        return PrepareSession(
            self.unit_of_work,
            self.sessions,
            self.accounts,
            self.secret_store,
            self.clock,
            SESSIONS_DIR,
        )

    def login(self) -> AuthenticateAccount:
        return AuthenticateAccount(
            self.unit_of_work, self.sessions, self.accounts, self.prepare(), self.clock
        )

    def logout(self) -> LogOutAccount:
        return LogOutAccount(
            self.unit_of_work, self.sessions, self.accounts, self.secret_store, self.clock
        )


@pytest.fixture
def harness() -> _Harness:
    """A use-case environment built from fakes."""
    return _Harness()


# ---------------------------------------------------------------------------
# AuthenticateAccount
# ---------------------------------------------------------------------------


class TestAuthenticateAccount:
    async def test_a_clean_login_records_both_axes(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        gateway = FakeTelegramGateway(ACCOUNT_A)

        result = await harness.login().execute(gateway, ScriptedHandler())

        assert result.session.authorization_state is AuthorizationState.READY
        assert result.session.connection_state is ConnectionState.READY
        assert result.session.can_send

    async def test_it_prepares_the_session_first(self, harness: _Harness) -> None:
        # The gateway cannot connect without the store path and the key, and
        # nothing else creates them.
        await harness.add_account(ACCOUNT_A, is_active=True)

        await harness.login().execute(FakeTelegramGateway(ACCOUNT_A), ScriptedHandler())

        assert await harness.secret_store.get(session_key_name(ACCOUNT_A)) is not None

    async def test_it_returns_who_telegram_says_we_are(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        result = await harness.login().execute(FakeTelegramGateway(ACCOUNT_A), ScriptedHandler())

        assert result.user == DEFAULT_USER

    async def test_it_records_the_login_time(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        result = await harness.login().execute(FakeTelegramGateway(ACCOUNT_A), ScriptedHandler())

        assert result.session.last_activity_at == NOW

    async def test_it_commits(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        await harness.login().execute(FakeTelegramGateway(ACCOUNT_A), ScriptedHandler())

        assert harness.units[-1].is_committed

    async def test_a_wrong_code_is_retried(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        handler = ScriptedHandler(codes=["00000", ACCEPTED_CODE])

        result = await harness.login().execute(FakeTelegramGateway(ACCOUNT_A), handler)

        assert len(handler.errors) == 1
        assert result.session.is_authorized

    async def test_two_factor_is_walked_through(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        gateway = FakeTelegramGateway(ACCOUNT_A, requires_password=True)

        result = await harness.login().execute(gateway, ScriptedHandler())

        assert gateway.submitted == ["phone", "code", "password"]
        assert result.session.is_authorized


class TestASessionThatSurvivedARestart:
    async def test_no_credential_is_requested(self, harness: _Harness) -> None:
        # The whole point of storing a session: running login again connects and
        # returns instead of asking for a code that was never sent.
        await harness.add_account(ACCOUNT_A, is_active=True)
        gateway = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        handler = ScriptedHandler(phones=[], codes=[], passwords=[])

        result = await harness.login().execute(gateway, handler)

        assert gateway.submitted == []
        assert result.was_already_authorized

    async def test_a_fresh_login_says_so(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        result = await harness.login().execute(FakeTelegramGateway(ACCOUNT_A), ScriptedHandler())

        assert not result.was_already_authorized

    async def test_the_stored_key_is_not_replaced(self, harness: _Harness) -> None:
        # Replacing it would make the store the first key encrypted unreadable.
        await harness.add_account(ACCOUNT_A, is_active=True)
        await harness.login().execute(FakeTelegramGateway(ACCOUNT_A), ScriptedHandler())
        original = await harness.secret_store.get(session_key_name(ACCOUNT_A))

        await harness.login().execute(
            FakeTelegramGateway(ACCOUNT_A, starts_authorized=True),
            ScriptedHandler(phones=[], codes=[], passwords=[]),
        )

        assert await harness.secret_store.get(session_key_name(ACCOUNT_A)) == original


class TestAuthenticateRefuses:
    async def test_without_an_active_account(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError, match="No account is active"):
            await harness.login().execute(FakeTelegramGateway(ACCOUNT_A), ScriptedHandler())

    async def test_a_gateway_bound_to_a_different_account(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        await harness.add_account(ACCOUNT_B, telegram_user_id=2002)

        with pytest.raises(AuthorizationError, match="bound to account") as excinfo:
            await harness.login().execute(FakeTelegramGateway(ACCOUNT_B), ScriptedHandler())

        assert "different account" in excinfo.value.user_message

    async def test_a_login_as_a_different_telegram_user(self, harness: _Harness) -> None:
        # The account's history belongs to the first person, and there is no way
        # to unmix two.
        await harness.add_account(ACCOUNT_A, is_active=True, telegram_user_id=999)
        gateway = FakeTelegramGateway(ACCOUNT_A)

        with pytest.raises(AuthorizationError, match="authenticated as") as excinfo:
            await harness.login().execute(gateway, ScriptedHandler())

        assert excinfo.value.context["expected"] == 999
        assert excinfo.value.context["actual"] == int(DEFAULT_USER.id)

    async def test_a_refused_identity_is_not_recorded(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True, telegram_user_id=999)

        with pytest.raises(AuthorizationError):
            await harness.login().execute(FakeTelegramGateway(ACCOUNT_A), ScriptedHandler())

        stored = harness.store.sessions[int(ACCOUNT_A)]
        assert stored.authorization_state is AuthorizationState.UNAUTHORIZED


# ---------------------------------------------------------------------------
# LogOutAccount
# ---------------------------------------------------------------------------


class TestLogOutAccount:
    async def test_it_tells_telegram(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        gateway = FakeTelegramGateway(ACCOUNT_A)
        await harness.login().execute(gateway, ScriptedHandler())

        await harness.logout().execute(gateway)

        assert gateway.logout_calls == 1

    async def test_it_records_the_logout(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        gateway = FakeTelegramGateway(ACCOUNT_A)
        await harness.login().execute(gateway, ScriptedHandler())

        session = await harness.logout().execute(gateway)

        assert session is not None
        assert session.authorization_state is AuthorizationState.LOGGED_OUT
        assert session.connection_state is ConnectionState.OFFLINE
        assert session.connected_at is None

    async def test_it_destroys_the_key(self, harness: _Harness) -> None:
        # The store is worthless without the key, which is what makes the
        # remaining bytes unreadable even if a file survives.
        await harness.add_account(ACCOUNT_A, is_active=True)
        gateway = FakeTelegramGateway(ACCOUNT_A)
        await harness.login().execute(gateway, ScriptedHandler())

        await harness.logout().execute(gateway)

        assert await harness.secret_store.get(session_key_name(ACCOUNT_A)) is None

    async def test_it_destroys_the_local_store(self, harness: _Harness, tmp_path: Path) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        prepare = PrepareSession(
            harness.unit_of_work,
            harness.sessions,
            harness.accounts,
            harness.secret_store,
            harness.clock,
            tmp_path,
        )
        session = await prepare.execute(ACCOUNT_A)
        session.session_path.mkdir(parents=True)
        (session.session_path / "db.sqlite").write_text("encrypted", encoding="utf-8")

        await harness.logout().execute(FakeTelegramGateway(ACCOUNT_A))

        assert not session.session_path.exists()

    async def test_the_record_survives_the_logout(self, harness: _Harness) -> None:
        # "This account was signed out" is a fact a deleted row cannot express.
        await harness.add_account(ACCOUNT_A, is_active=True)
        gateway = FakeTelegramGateway(ACCOUNT_A)
        await harness.login().execute(gateway, ScriptedHandler())

        await harness.logout().execute(gateway)

        assert int(ACCOUNT_A) in harness.store.sessions

    async def test_signing_out_of_nothing_is_not_an_error(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        assert await harness.logout().execute(FakeTelegramGateway(ACCOUNT_A)) is None

    async def test_without_an_active_account(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError):
            await harness.logout().execute(FakeTelegramGateway(ACCOUNT_A))


# ---------------------------------------------------------------------------
# The console handler
# ---------------------------------------------------------------------------


class TestConsoleAuthorizationHandler:
    def test_it_has_nowhere_to_keep_a_credential(self) -> None:
        # The structural version of "never retained": there is no attribute a
        # code or a password could survive in.
        assert set(ConsoleAuthorizationHandler.__slots__) == {"_max_attempts", "attempts"}

    async def test_it_retries_until_the_limit(self) -> None:
        handler = ConsoleAuthorizationHandler(max_attempts=3)
        error = AuthorizationError("no", user_message="That code is not correct.")

        first = await handler.on_error(error)
        second = await handler.on_error(error)
        third = await handler.on_error(error)

        assert first is RetryDecision.RETRY
        assert second is RetryDecision.RETRY
        assert third is RetryDecision.ABORT

    async def test_it_reports_the_reason_not_the_value(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        handler = ConsoleAuthorizationHandler()

        await handler.on_error(AuthorizationError("no", user_message="That code is not correct."))

        captured = capsys.readouterr()
        assert "That code is not correct." in captured.err

    async def test_state_changes_are_announced(self, capsys: pytest.CaptureFixture[str]) -> None:
        handler = ConsoleAuthorizationHandler()

        await handler.on_state_change(AuthorizationState.WAITING_CODE)

        assert "login code" in capsys.readouterr().out.lower()

    async def test_an_unannounced_state_prints_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        handler = ConsoleAuthorizationHandler()

        await handler.on_state_change(AuthorizationState.READY)

        assert capsys.readouterr().out == ""

    async def test_the_password_hint_is_shown(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The hint is text the user wrote themselves, so it is theirs to see.
        monkeypatch.setattr("getpass.getpass", lambda _prompt: "secret")
        handler = ConsoleAuthorizationHandler()

        await handler.request_password(
            PasswordHint(
                hint="the usual", has_recovery_email=True, recovery_email_pattern="a**@e*****.com"
            )
        )

        out = capsys.readouterr().out
        assert "the usual" in out
        assert "a**@e*****.com" in out

    async def test_the_password_is_read_without_echoing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prompts: list[str] = []

        def fake_getpass(prompt: str) -> str:
            prompts.append(prompt)
            return "secret"

        monkeypatch.setattr("getpass.getpass", fake_getpass)

        assert await ConsoleAuthorizationHandler().request_password(PasswordHint()) == "secret"
        assert prompts == ["Password: "]

    async def test_the_code_prompt_says_where_it_was_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prompts: list[str] = []

        def fake_prompt(text: str) -> str:
            prompts.append(text)
            return "12345"

        monkeypatch.setattr("typer.prompt", fake_prompt)

        await ConsoleAuthorizationHandler().request_code(CodeHint(delivery="sms", length=5))

        assert "SMS" in prompts[0]
        assert "5 digits" in prompts[0]

    @pytest.mark.parametrize(
        ("delivery", "expected"),
        [
            ("sms", "SMS"),
            ("call", "phone call"),
            ("telegram message", "another Telegram client"),
            ("fireworks", "fireworks"),
        ],
    )
    async def test_each_delivery_reads_sensibly(
        self, monkeypatch: pytest.MonkeyPatch, delivery: str, expected: str
    ) -> None:
        prompts: list[str] = []
        monkeypatch.setattr("typer.prompt", _recording(prompts))

        await ConsoleAuthorizationHandler().request_code(CodeHint(delivery=delivery))

        assert expected in prompts[0]

    async def test_an_unknown_delivery_adds_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        prompts: list[str] = []
        monkeypatch.setattr("typer.prompt", _recording(prompts))

        await ConsoleAuthorizationHandler().request_code(CodeHint(delivery="other"))

        assert prompts[0] == "Login code"


def _recording(prompts: list[str]) -> Callable[[str], str]:
    """Return a prompt that records what it was asked and answers trivially."""

    def prompt(text: str) -> str:
        prompts.append(text)
        return "1"

    return prompt


def test_the_default_user_is_a_real_domain_object() -> None:
    """The fake's user goes through the same validation production does."""
    assert isinstance(DEFAULT_USER, TelegramUser)
    assert int(DEFAULT_USER.id) > 0
