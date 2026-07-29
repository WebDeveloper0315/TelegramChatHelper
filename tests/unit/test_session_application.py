"""Session use cases and the credential-store startup rule.

The use case runs against fakes, so it exercises application logic with no
database and no credential backend. The startup rule is checked against a real
``Container``, because what it guarantees is wiring and wiring is exactly what a
fake would hide.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes import AdvanceableClock, InMemorySecretStore, UnavailableSecretStore
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.session_repository import (
    InMemorySessionRepository,
    InMemorySessionStore,
)
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.container import Container
from tgassist.application.use_cases.session import (
    KEY_BYTES,
    PrepareSession,
    generate_session_key,
    session_key_name,
)
from tgassist.domain.errors import RecordNotFoundError, SecretStoreUnavailableError
from tgassist.domain.model.account import Account
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.secret import MASK
from tgassist.domain.model.session import AuthorizationState, ConnectionState
from tgassist.domain.ports.migration_runner import SchemaState
from tgassist.domain.ports.secret_store import SecretStore
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.config import AppConfig, LoadedConfig, Profile, load_config

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
SESSIONS_DIR = Path("/data/sessions")


class _Harness:
    """A use-case environment built entirely from fakes."""

    def __init__(self, secret_store: SecretStore | None = None) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.store = InMemorySessionStore(known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)})
        self.secret_store = secret_store if secret_store is not None else InMemorySecretStore()
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
        """Build a scoped repository, exactly as the SQL factory does."""
        return InMemorySessionRepository(self.store, account_id)

    async def add_account(self, account_id: AccountId, *, is_active: bool = False) -> Account:
        account = Account.create(
            account_id=account_id,
            telegram_user_id=TelegramUserId(1000 + int(account_id)),
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


@pytest.fixture
def harness() -> _Harness:
    """A use-case environment built from fakes."""
    return _Harness()


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


class TestSessionKeyGeneration:
    def test_the_name_is_derived_from_the_account(self) -> None:
        # Not random: a key must still be findable after a crash that lost the
        # row, and a stale key must be overwritten rather than orphaned.
        assert session_key_name(ACCOUNT_A) == "telegram-session-key-1"
        assert session_key_name(ACCOUNT_B) == "telegram-session-key-2"

    def test_two_keys_differ(self) -> None:
        assert generate_session_key() != generate_session_key()

    def test_a_key_carries_the_intended_entropy(self) -> None:
        # token_urlsafe encodes KEY_BYTES bytes, so the text is longer than the
        # byte count and never shorter.
        assert len(generate_session_key()) >= KEY_BYTES

    def test_a_key_is_masked_on_every_incidental_path(self) -> None:
        key = generate_session_key()

        assert str(key) == MASK
        assert MASK in repr(key)
        assert key.reveal() not in f"{key}"


# ---------------------------------------------------------------------------
# PrepareSession
# ---------------------------------------------------------------------------


class TestPrepareSession:
    async def test_creates_a_session_for_the_active_account(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        session = await harness.prepare().execute()

        assert session.account_id == ACCOUNT_A
        assert session.authorization_state is AuthorizationState.UNAUTHORIZED
        assert session.connection_state is ConnectionState.OFFLINE

    async def test_creates_a_session_for_a_named_account(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        await harness.add_account(ACCOUNT_B)

        session = await harness.prepare().execute(ACCOUNT_B)

        assert session.account_id == ACCOUNT_B

    async def test_the_store_goes_under_the_configured_directory(self, harness: _Harness) -> None:
        # One directory per account, so two accounts cannot share a store.
        await harness.add_account(ACCOUNT_A, is_active=True)

        session = await harness.prepare().execute()

        assert session.session_path == SESSIONS_DIR / "1"

    async def test_the_key_is_written_to_the_credential_store(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        session = await harness.prepare().execute()
        stored = await harness.secret_store.get(session.encryption_key_ref)

        assert stored is not None
        assert len(stored) >= KEY_BYTES

    async def test_the_row_records_the_name_not_the_key(self, harness: _Harness) -> None:
        # The whole point of ADR-021 for this table: a key value in this column
        # is a security defect.
        await harness.add_account(ACCOUNT_A, is_active=True)

        session = await harness.prepare().execute()
        stored = await harness.secret_store.get(session.encryption_key_ref)

        assert session.encryption_key_ref == session_key_name(ACCOUNT_A)
        assert stored is not None
        assert session.encryption_key_ref != stored.reveal()

    async def test_the_creation_time_comes_from_the_clock(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        harness.clock.advance(timedelta(hours=3))

        session = await harness.prepare().execute()

        assert session.created_at == NOW + timedelta(hours=3)

    async def test_it_commits(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        await harness.prepare().execute()

        assert harness.units[-1].is_committed

    async def test_each_account_gets_its_own_key_and_directory(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        await harness.add_account(ACCOUNT_B)

        first = await harness.prepare().execute(ACCOUNT_A)
        second = await harness.prepare().execute(ACCOUNT_B)
        first_key = await harness.secret_store.get(first.encryption_key_ref)
        second_key = await harness.secret_store.get(second.encryption_key_ref)

        assert first.session_path != second.session_path
        assert first_key != second_key


class TestPrepareSessionIsIdempotent:
    async def test_a_second_call_returns_the_existing_session(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        first = await harness.prepare().execute()

        second = await harness.prepare().execute()

        assert second == first

    async def test_a_second_call_does_not_replace_the_key(self, harness: _Harness) -> None:
        # Replacing it would make the store the first key encrypted permanently
        # unreadable -- worse than useless.
        await harness.add_account(ACCOUNT_A, is_active=True)
        first = await harness.prepare().execute()
        original = await harness.secret_store.get(first.encryption_key_ref)

        await harness.prepare().execute()

        assert await harness.secret_store.get(first.encryption_key_ref) == original

    async def test_a_second_call_does_not_move_the_timestamps(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        await harness.prepare().execute()
        harness.clock.advance(timedelta(days=2))

        again = await harness.prepare().execute()

        assert again.created_at == NOW
        assert again.updated_at == NOW


class TestPrepareSessionRefuses:
    async def test_without_an_active_account(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError, match="No account is active"):
            await harness.prepare().execute()

    async def test_for_an_account_that_does_not_exist(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        with pytest.raises(RecordNotFoundError):
            await harness.prepare().execute(AccountId(999))

    async def test_when_no_credential_store_is_available(self) -> None:
        # There is nowhere else a session key may go: an unencrypted fallback
        # does not exist. This is fatal whatever require_secret_store says,
        # because that flag governs startup rather than this operation.
        harness = _Harness(secret_store=UnavailableSecretStore())
        await harness.add_account(ACCOUNT_A, is_active=True)

        with pytest.raises(SecretStoreUnavailableError) as excinfo:
            await harness.prepare().execute()

        assert "credential store is unavailable" in excinfo.value.user_message

    async def test_and_writes_nothing_when_the_credential_store_is_missing(self) -> None:
        harness = _Harness(secret_store=UnavailableSecretStore())
        await harness.add_account(ACCOUNT_A, is_active=True)

        with pytest.raises(SecretStoreUnavailableError):
            await harness.prepare().execute()

        assert harness.store.sessions == {}
        assert not harness.units[-1].is_committed

    async def test_no_error_carries_key_material(self) -> None:
        harness = _Harness(secret_store=UnavailableSecretStore())
        await harness.add_account(ACCOUNT_A, is_active=True)

        with pytest.raises(SecretStoreUnavailableError) as excinfo:
            await harness.prepare().execute()

        assert excinfo.value.context == {"account_id": 1}


# ---------------------------------------------------------------------------
# The credential-store startup rule (SECURITY.md section 7 point 6)
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, *, require_secret_store: bool) -> AppConfig:
    """A configuration whose only interesting value is the security rule."""
    return AppConfig.model_validate(
        {
            "profile": "testing",
            "app": {"data_dir": str(tmp_path)},
            "logging": {"console_enabled": False, "file_enabled": False},
            "security": {"require_secret_store": require_secret_store},
        }
    )


class TestStartupEnforcesTheCredentialStore:
    async def test_startup_refuses_without_a_credential_store(self, tmp_path: Path) -> None:
        # Carried unenforced since Milestone 0. It stops being theoretical the
        # moment a session key exists.
        container = Container(
            LoadedConfig(config=_config(tmp_path, require_secret_store=True)),
            secrets=UnavailableSecretStore(),
        )
        async with container:
            with pytest.raises(SecretStoreUnavailableError):
                await container.start()

    async def test_startup_does_not_open_the_database_when_it_refuses(self, tmp_path: Path) -> None:
        # The check runs first on purpose: a refusal that had already migrated
        # the database would have done work it promised not to do.
        container = Container(
            LoadedConfig(config=_config(tmp_path, require_secret_store=True)),
            secrets=UnavailableSecretStore(),
        )
        async with container:
            with pytest.raises(SecretStoreUnavailableError):
                await container.start()
            health = await container.database_health()

        assert not health.reachable

    async def test_startup_proceeds_when_the_store_is_available(self, tmp_path: Path) -> None:
        container = Container(
            LoadedConfig(config=_config(tmp_path, require_secret_store=True)),
            secrets=InMemorySecretStore(),
        )
        async with container:
            status = await container.start()

        assert status.state is SchemaState.CURRENT

    async def test_a_configuration_that_opts_out_still_starts(self, tmp_path: Path) -> None:
        # Development and testing set require_secret_store false, so a developer
        # without a credential backend is not locked out of the application.
        container = Container(
            LoadedConfig(config=_config(tmp_path, require_secret_store=False)),
            secrets=UnavailableSecretStore(),
        )
        async with container:
            status = await container.start()

        assert status.state is SchemaState.CURRENT

    def test_the_shipped_profiles_agree_with_that_split(self) -> None:
        # The rule is only safe to enforce literally because the profiles a
        # developer runs opt out of it.
        assert not load_config(profile=Profile.DEVELOPMENT).config.security.require_secret_store
        assert not load_config(profile=Profile.TESTING).config.security.require_secret_store
        assert load_config(profile=Profile.PRODUCTION).config.security.require_secret_store

    async def test_the_container_builds_the_use_case(self, tmp_path: Path) -> None:
        container = Container(LoadedConfig(config=_config(tmp_path, require_secret_store=False)))
        async with container:
            assert isinstance(container.prepare_session(), PrepareSession)
