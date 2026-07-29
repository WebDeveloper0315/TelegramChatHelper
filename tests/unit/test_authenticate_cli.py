"""The login and logout commands, and the container wiring behind them.

End to end against a real SQLite file and a real container, with the gateway
replaced. The point of this layer is the wiring, and wiring is exactly what a
fake container would hide — but nothing here reaches Telegram.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fakes import InMemorySecretStore
from tests.fakes.telegram_gateway import FakeTelegramGateway
from tgassist.application.container import Container
from tgassist.domain.errors import RecordNotFoundError, TelegramNotConfiguredError
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.secret import SecretValue
from tgassist.domain.model.session import AuthorizationState, Session
from tgassist.infrastructure.config import AppConfig, LoadedConfig
from tgassist.presentation.cli.app import app

runner = CliRunner()
TELEGRAM_USER = 1001


@pytest.fixture
def cli_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_logging: None,  # noqa: ARG001 - a command configures logging process-wide
) -> Path:
    """Point the CLI at an isolated data directory with logging silenced.

    The credential store is substituted too. A CLI-built container makes its own
    (there is nothing to inject into), and ``tgassist login`` writes a session
    key -- so without this the suite would leave one secret per test in the
    developer's own operating-system credential manager.
    """
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TGASSIST_APP__DATA_DIR", str(data_dir))
    monkeypatch.setenv("TGASSIST_LOGGING__CONSOLE_ENABLED", "false")
    monkeypatch.setenv("TGASSIST_LOGGING__FILE_ENABLED", "false")

    store = InMemorySecretStore()
    monkeypatch.setattr("tgassist.application.container.build_default_secret_store", lambda: store)
    return data_dir


@pytest.fixture
def _account() -> None:
    """Create an active account whose Telegram identity the fake gateway matches."""
    result = runner.invoke(app, ["account", "create", str(TELEGRAM_USER), "Primary"])
    assert result.exit_code == 0, result.output


@pytest.fixture
def _gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the container's gateway with a scripted one.

    Only the gateway. The database, the credential store, the container and the
    commands are all real, so what is exercised is the wiring rather than a
    rehearsal of it.
    """

    @asynccontextmanager
    async def fake_gateway(
        self: Container, account_id: AccountId
    ) -> AsyncIterator[FakeTelegramGateway]:
        del self
        gateway = FakeTelegramGateway(account_id)
        try:
            yield gateway
        finally:
            await gateway.disconnect()

    monkeypatch.setattr(Container, "telegram_for", fake_gateway)


@pytest.fixture
def _answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every prompt the login asks."""
    monkeypatch.setattr("typer.prompt", lambda text: "12345" if "code" in text.lower() else "+44")
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "not-a-real-password")


@pytest.mark.usefixtures("cli_env", "_account", "_gateway", "_answers")
class TestLoginCommand:
    def test_it_signs_in(self) -> None:
        result = runner.invoke(app, ["login"])

        assert result.exit_code == 0, result.output
        assert "Signed in as" in result.stdout

    def test_it_reports_both_axes(self) -> None:
        result = runner.invoke(app, ["login"])

        assert "authorization: ready" in result.stdout
        assert "connection: ready" in result.stdout

    def test_the_session_survives_the_command(self) -> None:
        runner.invoke(app, ["login"])

        result = runner.invoke(app, ["login"])

        assert result.exit_code == 0, result.output
        assert "Signed in as" in result.stdout

    def test_a_named_account_can_be_chosen(self) -> None:
        listed = runner.invoke(app, ["account", "list"])
        account_id = next(word for word in listed.stdout.split() if word.isdigit())

        result = runner.invoke(app, ["login", "--account", account_id])

        assert result.exit_code == 0, result.output


@pytest.mark.usefixtures("cli_env", "_gateway", "_answers")
class TestLoginWithoutAnAccount:
    def test_it_says_so(self) -> None:
        result = runner.invoke(app, ["login"])

        assert result.exit_code != 0
        assert "No account is active" in result.output


@pytest.mark.usefixtures("cli_env", "_account", "_gateway", "_answers")
class TestLogoutCommand:
    def test_it_asks_before_destroying_anything(self) -> None:
        runner.invoke(app, ["login"])

        result = runner.invoke(app, ["logout"], input="n\n")

        assert result.exit_code == 0, result.output
        assert "Cancelled" in result.stdout

    def test_it_signs_out_when_confirmed(self) -> None:
        runner.invoke(app, ["login"])

        result = runner.invoke(app, ["logout", "--yes"])

        assert result.exit_code == 0, result.output
        assert "Signed out" in result.stdout

    def test_signing_out_of_nothing_says_so(self) -> None:
        result = runner.invoke(app, ["logout", "--yes"])

        assert result.exit_code == 0, result.output
        assert "no Telegram session" in result.stdout

    def test_the_next_login_starts_fresh(self) -> None:
        runner.invoke(app, ["login"])
        runner.invoke(app, ["logout", "--yes"])

        result = runner.invoke(app, ["login"])

        assert result.exit_code == 0, result.output
        assert "Signed in as" in result.stdout


# ---------------------------------------------------------------------------
# The container's own wiring
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, **telegram: object) -> AppConfig:
    """A configuration whose only interesting values are the Telegram ones."""
    return AppConfig.model_validate(
        {
            "profile": "testing",
            "app": {"data_dir": str(tmp_path)},
            "logging": {"console_enabled": False, "file_enabled": False},
            "telegram": telegram,
        }
    )


def _container(tmp_path: Path, **telegram: object) -> Container:
    """A real container with an in-memory credential store.

    The store is substituted deliberately. The default reaches the operating
    system's credential manager, and a test that wrote to it would leave a
    secret on the developer's own machine -- which one earlier version of this
    file did, and which the next test then read back and passed on.
    """
    return Container(
        LoadedConfig(config=_config(tmp_path, **telegram)), secrets=InMemorySecretStore()
    )


class TestGatewayWiring:
    async def test_it_refuses_without_an_application_id(self, tmp_path: Path) -> None:
        # A setup step, not a failure: there is no default and no way to derive
        # one, so it is reported separately from every connection problem.
        container = _container(tmp_path)
        async with container:
            await container.start()
            with pytest.raises(TelegramNotConfiguredError) as excinfo:
                async with container.telegram_for(AccountId(1)):
                    pass

        assert "my.telegram.org" in excinfo.value.user_message

    async def test_it_refuses_without_an_application_hash(self, tmp_path: Path) -> None:
        container = _container(tmp_path, api_id=12345)
        async with container:
            await container.start()
            with pytest.raises(TelegramNotConfiguredError) as excinfo:
                async with container.telegram_for(AccountId(1)):
                    pass

        assert "TELEGRAM_API_HASH" in excinfo.value.user_message

    async def test_it_refuses_without_a_prepared_session(self, tmp_path: Path) -> None:
        # The gateway cannot be built without the store path and the key, and
        # inventing either here would put a second creator of session material
        # in the system.
        container = _container(tmp_path, api_id=12345)
        async with container:
            await container.start()
            await container.secrets.set("TELEGRAM_API_HASH", SecretValue("0" * 32))

            with pytest.raises(RecordNotFoundError):
                async with container.telegram_for(AccountId(999)):
                    pass

    async def test_the_api_hash_is_read_by_name_from_the_credential_store(
        self, tmp_path: Path
    ) -> None:
        container = _container(tmp_path, api_id=12345, api_hash_ref="MY_HASH")
        async with container:
            await container.start()

            with pytest.raises(TelegramNotConfiguredError) as excinfo:
                async with container.telegram_for(AccountId(1)):
                    pass

        assert excinfo.value.context["api_hash_ref"] == "MY_HASH"


@pytest.mark.usefixtures("cli_env", "_account", "_gateway", "_answers")
class TestWhatTheCommandsDoNotPrint:
    def test_no_credential_appears_in_the_output(self) -> None:
        # The one place a code or a password is typed is the one place it must
        # not be echoed back.
        result = runner.invoke(app, ["login"])

        assert "12345" not in result.output
        assert "not-a-real-password" not in result.output
        assert "+44" not in result.output

    def test_the_session_key_never_appears(self) -> None:
        result = runner.invoke(app, ["login"])

        assert "telegram-session-key" not in result.output


@pytest.mark.usefixtures("cli_env", "_account", "_gateway", "_answers")
class TestSessionStateAfterTheCommands:
    def test_login_then_logout_leaves_a_record(self, cli_env: Path) -> None:
        # Synchronous on purpose: the CLI runner calls asyncio.run internally,
        # which cannot be done from inside a running loop.
        assert runner.invoke(app, ["login"]).exit_code == 0
        assert runner.invoke(app, ["logout", "--yes"]).exit_code == 0

        session = asyncio.run(_stored_session(cli_env))

        assert session is not None
        assert session.authorization_state is AuthorizationState.LOGGED_OUT


async def _stored_session(data_dir: Path) -> Session | None:
    """Read the stored session back through a second, real container."""
    container = Container(
        LoadedConfig(
            config=AppConfig.model_validate(
                {
                    "profile": "testing",
                    "app": {"data_dir": str(data_dir)},
                    "logging": {"console_enabled": False, "file_enabled": False},
                }
            )
        )
    )
    async with container:
        await container.start()
        account = await container.get_account().execute(None)
        if account is None:
            return None
        async with container.unit_of_work() as uow:
            return await container.sessions(uow, account.id).get()
