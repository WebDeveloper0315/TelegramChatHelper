"""Composition root.

This is the only module permitted to construct infrastructure adapters. Every
other component receives its collaborators through its constructor, which is
what allows the domain and the use cases to be exercised against fakes.

Wiring is written by hand rather than delegated to a framework. The object graph
is fixed and single-process, so a container library would add a declaration
language and a layer of indirection without removing any real work. If runtime
service registration becomes necessary for plugins, that trade-off is revisited
in Milestone 12.

**This container is not a service locator.** Components never receive the
container and reach into it for what they need; the container constructs them
and passes their dependencies as constructor arguments. The distinction matters
because a locator hides a component's real dependencies from its signature,
which is exactly the information a reader and a test need most.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from tgassist import __version__
from tgassist.application.use_cases.account import (
    CreateAccount,
    GetAccount,
    ListAccounts,
    SetActiveAccount,
)
from tgassist.application.use_cases.authenticate import AuthenticateAccount, LogOutAccount
from tgassist.application.use_cases.chat import (
    GetChat,
    ListChats,
    OpenGroupChat,
    OpenPrivateChat,
    SetChatPolicy,
)
from tgassist.application.use_cases.contact import (
    ChangeContactStatus,
    CreateContact,
    GetContact,
    ListContacts,
)
from tgassist.application.use_cases.message import (
    GetMessage,
    IngestMessages,
    ReadChatHistory,
)
from tgassist.application.use_cases.session import PrepareSession
from tgassist.application.use_cases.user_profile import (
    GetUserProfile,
    UpdateUserProfile,
)
from tgassist.domain.errors import (
    RecordNotFoundError,
    SchemaVersionError,
    SecretStoreUnavailableError,
    TelegramNotConfiguredError,
)
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.tdlib import TdlibRuntime
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.contact_repository import ContactRepository
from tgassist.domain.ports.database import HealthReport
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.message_repository import MessageRepository
from tgassist.domain.ports.migration_runner import SchemaState, SchemaStatus
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.secret_store import SecretStore
from tgassist.domain.ports.session_repository import SessionRepository
from tgassist.domain.ports.telegram_gateway import TelegramGateway
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.domain.ports.user_profile_repository import UserProfileRepository
from tgassist.infrastructure.clock import SystemClock
from tgassist.infrastructure.config import (
    AppConfig,
    LoadedConfig,
    Profile,
    is_owner_only,
    load_config,
)
from tgassist.infrastructure.events import InProcessEventBus
from tgassist.infrastructure.ids import UuidV7IdGenerator
from tgassist.infrastructure.logging import configure_logging, get_logger
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqliteDatabase,
    UnitOfWorkFactory,
    account_repository,
    chat_repository,
    contact_repository,
    message_repository,
    session_repository,
    user_profile_repository,
)
from tgassist.infrastructure.security import build_default_secret_store
from tgassist.infrastructure.telegram import LoaderSettings, TdjsonLoader
from tgassist.infrastructure.telegram.client import TdjsonClient
from tgassist.infrastructure.telegram.gateway import GatewaySettings, TdlibGateway


class Container:
    """Holds the application's infrastructure and hands it to callers.

    Construct with :meth:`create`, which performs the startup sequence in the
    order the specification requires: configuration is resolved and validated
    before anything else, and logging is configured before any component that
    might need to report a problem.

    Every port can be overridden through the constructor, so a test builds a
    container with fakes rather than patching module globals.
    """

    __slots__ = (
        "_clock",
        "_closed",
        "_database",
        "_events",
        "_ids",
        "_loaded",
        "_migrations",
        "_secrets",
        "_uow_factory",
    )

    def __init__(
        self,
        loaded: LoadedConfig,
        *,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        events: EventBus | None = None,
        secrets: SecretStore | None = None,
        database: SqliteDatabase | None = None,
    ) -> None:
        """Store a resolved configuration and build or accept the core ports.

        Prefer :meth:`create` for real use. This constructor exists so that a
        test can inject doubles without touching the filesystem or the operating
        system credential store.

        Args:
            loaded: Resolved configuration with the provenance of each value.
            clock: Time source. Defaults to the system clock.
            ids: Identifier generator. Defaults to UUID version 7, driven by
                whichever clock is in use, so a fixed clock also fixes the
                identifiers.
            events: Event bus. Defaults to synchronous in-process delivery.
            secrets: Secret store. Defaults to environment over credential store.
            database: Database. Defaults to SQLite at the configured path. Tests
                inject an in-memory database this way.
        """
        self._loaded = loaded
        self._clock = clock if clock is not None else SystemClock()
        self._ids = ids if ids is not None else UuidV7IdGenerator(self._clock)
        self._events = events if events is not None else InProcessEventBus()
        self._secrets = secrets if secrets is not None else build_default_secret_store()
        self._database = (
            database
            if database is not None
            else SqliteDatabase(
                loaded.config.database.model_copy(update={"path": loaded.config.database_path})
            )
        )
        self._uow_factory = UnitOfWorkFactory(self._database)
        self._migrations = AlembicMigrationRunner(self._database)
        self._closed = False

    @classmethod
    def create(
        cls,
        *,
        profile: Profile | str | None = None,
        config_dir: Path | None = None,
        overrides: dict[str, Any] | None = None,
        configure_logging_on_start: bool = True,
    ) -> Self:
        """Resolve configuration, configure logging and build the container.

        Args:
            profile: Environment profile. Defaults to ``TGASSIST_PROFILE``.
            config_dir: Configuration directory. Defaults to ``TGASSIST_CONFIG_DIR``.
            overrides: Highest-precedence values, such as command-line flags.
            configure_logging_on_start: Configure global logging. Disabled by
                tests that must not mutate process-wide logging state.

        Raises:
            ConfigurationError: If configuration is missing, malformed or invalid.
        """
        loaded = load_config(profile=profile, config_dir=config_dir, overrides=overrides)
        container = cls(loaded)

        if configure_logging_on_start:
            configure_logging(loaded.config)
            get_logger(__name__).info(
                "application_configured",
                profile=loaded.config.profile.value,
                config_layers=[str(path) for path in loaded.layers],
                data_dir=str(loaded.config.paths.data_dir),
            )
        return container

    # -- Configuration ----------------------------------------------------

    @property
    def config(self) -> AppConfig:
        """Return the immutable application configuration."""
        return self._loaded.config

    @property
    def loaded_config(self) -> LoadedConfig:
        """Return the configuration together with the provenance of each value."""
        return self._loaded

    # -- Core ports -------------------------------------------------------

    @property
    def clock(self) -> Clock:
        """Return the application's time source."""
        return self._clock

    @property
    def ids(self) -> IdGenerator:
        """Return the application's identifier generator."""
        return self._ids

    @property
    def events(self) -> EventBus:
        """Return the application's event bus."""
        return self._events

    @property
    def secrets(self) -> SecretStore:
        """Return the application's secret store."""
        return self._secrets

    @property
    def database(self) -> SqliteDatabase:
        """Return the database."""
        return self._database

    @property
    def unit_of_work(self) -> UnitOfWorkFactory:
        """Return the unit of work factory.

        A factory rather than a unit of work: a use case decides when its
        transaction begins, and an injected open transaction would outlive the
        operation it was meant to bound.
        """
        return self._uow_factory

    @property
    def migrations(self) -> AlembicMigrationRunner:
        """Return the migration runner."""
        return self._migrations

    @property
    def accounts(self) -> RepositoryFactory[AccountRepository]:
        """Return the account repository factory.

        A factory, not a repository: a repository belongs to one transaction,
        and handing out a long-lived one would keep a closed transaction alive.
        """
        return account_repository

    @property
    def user_profiles(self) -> ScopedRepositoryFactory[UserProfileRepository]:
        """Return the user profile repository factory.

        Scoped: the account is supplied at construction, so no method can be
        called without one and no caller can pass the wrong one (ADR-039).
        """
        return user_profile_repository

    @property
    def contacts(self) -> ScopedRepositoryFactory[ContactRepository]:
        """Return the contact repository factory.

        Scoped, like every repository over account-owned data. It matters more
        here than for the profile: a profile is one row and a mistake is
        obvious, whereas a contact list quietly containing somebody else's
        contacts looks exactly like a correct one (ADR-039).
        """
        return contact_repository

    @property
    def chats(self) -> ScopedRepositoryFactory[ChatRepository]:
        """Return the chat repository factory."""
        return chat_repository

    @property
    def messages(self) -> ScopedRepositoryFactory[MessageRepository]:
        """Return the message repository factory.

        Append-only: the port has no update or delete, so nothing built from
        this factory can change a stored message.
        """
        return message_repository

    @property
    def sessions(self) -> ScopedRepositoryFactory[SessionRepository]:
        """Return the session repository factory.

        No delete: a session goes with its account, by cascade. Logging out is a
        transition, not a deletion.
        """
        return session_repository

    # -- Use cases --------------------------------------------------------
    #
    # Built on demand rather than held, because a use case is a small object
    # over injected collaborators and caching one would only add lifetime
    # questions. Each receives exactly the dependencies it needs, which is what
    # keeps the container from becoming a service locator.

    def create_account(self) -> CreateAccount:
        """Build the account creation use case."""
        return CreateAccount(self._uow_factory, self.accounts, self._clock, self._ids)

    def get_account(self) -> GetAccount:
        """Build the account lookup use case."""
        return GetAccount(self._uow_factory, self.accounts)

    def list_accounts(self) -> ListAccounts:
        """Build the account listing use case."""
        return ListAccounts(self._uow_factory, self.accounts)

    def set_active_account(self) -> SetActiveAccount:
        """Build the account activation use case."""
        return SetActiveAccount(self._uow_factory, self.accounts, self._clock)

    def get_user_profile(self) -> GetUserProfile:
        """Build the profile retrieval use case."""
        return GetUserProfile(self._uow_factory, self.user_profiles, self.accounts, self._clock)

    def update_user_profile(self) -> UpdateUserProfile:
        """Build the profile update use case."""
        return UpdateUserProfile(
            self._uow_factory, self.user_profiles, self.get_user_profile(), self._clock
        )

    def create_contact(self) -> CreateContact:
        """Build the contact creation use case."""
        return CreateContact(
            self._uow_factory, self.contacts, self.accounts, self._clock, self._ids
        )

    def get_contact(self) -> GetContact:
        """Build the contact lookup use case."""
        return GetContact(self._uow_factory, self.contacts, self.accounts)

    def list_contacts(self) -> ListContacts:
        """Build the contact listing use case."""
        return ListContacts(self._uow_factory, self.contacts, self.accounts)

    def change_contact_status(self) -> ChangeContactStatus:
        """Build the contact archive, restore and delete use case."""
        return ChangeContactStatus(self._uow_factory, self.contacts, self.accounts, self._clock)

    def open_private_chat(self) -> OpenPrivateChat:
        """Build the private chat creation use case.

        The one use case so far needing two scoped repositories: it resolves a
        contact and creates a chat in a single transaction.
        """
        return OpenPrivateChat(
            self._uow_factory, self.chats, self.contacts, self.accounts, self._clock, self._ids
        )

    def open_group_chat(self) -> OpenGroupChat:
        """Build the group chat creation use case."""
        return OpenGroupChat(self._uow_factory, self.chats, self.accounts, self._clock, self._ids)

    def get_chat(self) -> GetChat:
        """Build the chat lookup use case."""
        return GetChat(self._uow_factory, self.chats, self.accounts)

    def list_chats(self) -> ListChats:
        """Build the chat listing use case."""
        return ListChats(self._uow_factory, self.chats, self.accounts)

    def set_chat_policy(self) -> SetChatPolicy:
        """Build the chat policy use case."""
        return SetChatPolicy(self._uow_factory, self.chats, self.accounts, self._clock)

    def ingest_messages(self) -> IngestMessages:
        """Build the ingestion pipeline.

        Source-agnostic: the CLI feeds it today, synchronisation will feed the
        same object in Milestone 3.
        """
        return IngestMessages(
            self._uow_factory,
            self.messages,
            self.chats,
            self.accounts,
            self._clock,
            self._ids,
        )

    def read_chat_history(self) -> ReadChatHistory:
        """Build the chat history use case."""
        return ReadChatHistory(self._uow_factory, self.messages, self.accounts)

    def get_message(self) -> GetMessage:
        """Build the message lookup use case."""
        return GetMessage(self._uow_factory, self.messages, self.accounts)

    def prepare_session(self) -> PrepareSession:
        """Build the session preparation use case.

        Establishes what a login needs and cannot create for itself: a place to
        put the encrypted store, and a key in the credential store to encrypt it
        with. Authentication itself arrives in the next slice.
        """
        return PrepareSession(
            self._uow_factory,
            self.sessions,
            self.accounts,
            self._secrets,
            self._clock,
            self.config.paths.sessions_dir,
        )

    def authenticate_account(self) -> AuthenticateAccount:
        """Build the login use case.

        It takes the gateway per call rather than here, because a gateway holds
        a live connection and this container hands out objects with no lifetime.
        """
        return AuthenticateAccount(
            self._uow_factory,
            self.sessions,
            self.accounts,
            self.prepare_session(),
            self._clock,
        )

    def log_out_account(self) -> LogOutAccount:
        """Build the logout use case."""
        return LogOutAccount(
            self._uow_factory, self.sessions, self.accounts, self._secrets, self._clock
        )

    def repository[R](self, factory: RepositoryFactory[R], uow: UnitOfWork) -> R:
        """Build a repository bound to an open unit of work.

        This is a convenience for wiring, not a service locator: the caller
        supplies the factory it already depends on, so nothing is looked up by
        name or type. A use case receives its factories as constructor
        parameters and calls them directly; this exists for the composition
        root and for tests.
        """
        return factory(uow)

    # -- Startup ----------------------------------------------------------

    async def start(self, *, migrate: bool | None = None) -> SchemaStatus:
        """Bring the application up: check the credential store, open the database.

        The order is deliberate. ``SECURITY.md`` section 7 point 6 says the
        application refuses to start when the credential store is unavailable
        rather than falling back to an unencrypted session, and a check that ran
        after the database was open would have already done work it promised not
        to do.

        This is the entry point every command that touches user data uses.
        Diagnostic commands deliberately do not: ``doctor`` exists to *report* an
        unavailable credential store, so refusing to run it would remove the one
        tool that explains the refusal.

        Args:
            migrate: Apply pending migrations. Defaults to the configured
                ``database.auto_migrate``.

        Returns:
            The schema position after any migration.

        Raises:
            SecretStoreUnavailableError: If ``security.require_secret_store`` is
                set and no credential backend is available.
            DatabaseUnavailableError: If the database cannot be opened.
            SchemaVersionError: If the database was written by a newer version.
            MigrationFailedError: If a migration fails.
        """
        await self.verify_secret_store()
        return await self.start_database(migrate=migrate)

    async def start_database(self, *, migrate: bool | None = None) -> SchemaStatus:
        """Open the database and bring its schema to the expected revision.

        Args:
            migrate: Apply pending migrations. Defaults to the configured
                ``database.auto_migrate``.

        Returns:
            The schema position after any migration.

        Raises:
            DatabaseUnavailableError: If the database cannot be opened.
            SchemaVersionError: If the database was written by a newer version.
                Refusing to start is the only safe response, because a migration
                that removed a column cannot restore what it discarded.
            MigrationFailedError: If a migration fails, leaving the database at
                its previous revision.
        """
        await self._database.connect()
        status = await self._migrations.status()

        if status.state in (SchemaState.AHEAD, SchemaState.UNKNOWN):
            msg = (
                f"The database is at revision {status.current_revision!r}; this "
                f"application expects {status.head_revision!r}"
            )
            raise SchemaVersionError(
                msg,
                user_message=(
                    "This database was created by a newer version of the application. "
                    "Please update to open it."
                ),
                context={
                    "current": status.current_revision,
                    "expected": status.head_revision,
                    "state": status.state.value,
                },
            )

        should_migrate = self.config.database.auto_migrate if migrate is None else migrate
        if should_migrate and status.state in (SchemaState.EMPTY, SchemaState.BEHIND):
            await self._migrations.upgrade()
            status = await self._migrations.status()

        return status

    async def database_health(self) -> HealthReport:
        """Check the database and report what was found. Never raises."""
        return await self._database.health()

    # -- Logging ----------------------------------------------------------

    def logger(self, name: str | None = None) -> Any:
        """Return a bound logger for the given name."""
        return get_logger(name)

    # -- Filesystem -------------------------------------------------------

    def ensure_directories(self) -> None:
        """Create the application's directory layout."""
        self.config.paths.ensure(restrict_permissions=self.config.security.enforce_file_permissions)

    def tdlib_runtime(self) -> TdlibRuntime:
        """Inspect the native Telegram runtime without raising.

        Presentation reaches the loader through here rather than importing
        infrastructure directly (ADR-011). Returns a report even when the
        runtime is unusable, because a diagnostic that raises tells the user
        less than one that explains.
        """
        return self._tdjson_loader().inspect()

    @asynccontextmanager
    async def telegram_for(self, account_id: AccountId) -> AsyncIterator[TelegramGateway]:
        """Open a Telegram gateway for one account, and close it afterwards.

        Not a property like ``clock``: a gateway owns a native client and a
        network connection, so it is acquired and released explicitly
        (``TELEGRAM_ARCHITECTURE.md`` section 7.3). The context manager exists so
        that no caller has to remember the release.

        The session must already have been prepared — the gateway cannot be
        built without the store path and the encryption key, and inventing
        either here would put a second creator of session material in the
        system.

        Raises:
            TelegramNotConfiguredError: If ``telegram.api_id`` is unset, or the
                application hash is not in the credential store.
            RecordNotFoundError: If the account has no prepared session.
            TdlibNotFoundError: If no verified native library is available.
        """
        settings = await self._gateway_settings(account_id)
        # The runtime report is discarded here: load() has already refused
        # anything unusable, so what is left to say about it belongs to
        # `tdlib doctor` rather than to a login.
        library, _runtime = self._tdjson_loader().load()
        gateway = TdlibGateway(account_id, TdjsonClient(library), settings)
        try:
            yield gateway
        finally:
            await gateway.disconnect()

    async def _gateway_settings(self, account_id: AccountId) -> GatewaySettings:
        """Collect everything TDLib needs before it will accept a request."""
        telegram = self.config.telegram
        if telegram.api_id is None:
            msg = "telegram.api_id is not configured"
            raise TelegramNotConfiguredError(
                msg,
                user_message=(
                    "No Telegram application id is configured. Obtain one from "
                    "https://my.telegram.org and set telegram.api_id."
                ),
            )

        api_hash = await self._secrets.get(telegram.api_hash_ref)
        if api_hash is None:
            msg = f"No secret named {telegram.api_hash_ref} is stored"
            raise TelegramNotConfiguredError(
                msg,
                user_message=(
                    f"No Telegram application hash is stored under "
                    f"{telegram.api_hash_ref}. Obtain one from "
                    f"https://my.telegram.org and store it there."
                ),
                context={"api_hash_ref": telegram.api_hash_ref},
            )

        async with self._uow_factory() as uow:
            session = await session_repository(uow, account_id).get()
        if session is None:
            msg = f"Account {int(account_id)} has no prepared session"
            raise RecordNotFoundError(
                msg,
                user_message="That account has no Telegram session yet.",
                context={"account_id": int(account_id)},
            )

        key = await self._secrets.get(session.encryption_key_ref)
        if key is None:
            msg = f"No session key is stored under {session.encryption_key_ref}"
            raise TelegramNotConfiguredError(
                msg,
                user_message=(
                    "The session encryption key is missing from the credential "
                    "store. Sign out and sign in again to create a new one."
                ),
                context={"account_id": int(account_id)},
            )

        return GatewaySettings(
            api_id=telegram.api_id,
            api_hash=api_hash,
            session_path=session.session_path,
            database_encryption_key=key,
            device_model=telegram.device_model,
            application_version=__version__,
        )

    def _tdjson_loader(self) -> TdjsonLoader:
        """Build a loader from the configured Telegram settings."""
        telegram = self.config.telegram
        return TdjsonLoader(
            LoaderSettings(
                configured_path=telegram.tdjson_path,
                data_dir=self.config.paths.data_dir,
                minimum_version=telegram.minimum_version,
                log_verbosity=telegram.log_verbosity,
                search_system=telegram.search_system_library_path,
            )
        )

    def permission_report(self) -> dict[str, bool | None]:
        """Report whether each existing directory is restricted to its owner.

        ``None`` means "not verified on this platform", never "safe".
        Presentation reaches filesystem detail through here rather than
        importing infrastructure directly.
        """
        return {
            directory.name: is_owner_only(directory)
            for directory in self.config.paths.all_directories()
            if directory.exists()
        }

    # -- Security ---------------------------------------------------------

    async def verify_secret_store(self) -> bool:
        """Check the credential backend, enforcing the configured requirement.

        Returns:
            Whether a usable backend is present.

        Raises:
            SecretStoreUnavailableError: If ``security.require_secret_store`` is
                set and no backend is available. Failing closed is deliberate:
                continuing would mean storing a Telegram session -- the highest
                value asset in the system -- without encryption.
        """
        available = await self._secrets.is_available()
        if not available and self.config.security.require_secret_store:
            msg = "No operating system credential store is available"
            raise SecretStoreUnavailableError(
                msg,
                user_message=(
                    "The system credential store is unavailable, so credentials "
                    "cannot be protected."
                ),
                context={"profile": self.config.profile.value},
            )
        return available

    # -- Lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Release held resources, including the database and its worker thread."""
        if self._closed:
            return
        self._closed = True
        await self._database.close()

    def close(self) -> None:
        """Release resources that can be released without an event loop.

        The database owns a worker thread and is closed properly by
        :meth:`aclose`. This synchronous form exists for callers that never
        opened it, and still shuts the executor down so no thread is leaked.
        """
        if self._closed:
            return
        self._closed = True
        self._database.executor.close(wait=False)

    @property
    def is_closed(self) -> bool:
        """Report whether :meth:`close` has been called."""
        return self._closed

    def __enter__(self) -> Self:
        """Enter the container's lifetime."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Leave the container's lifetime, releasing resources."""
        self.close()

    async def __aenter__(self) -> Self:
        """Enter the container's lifetime."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Leave the container's lifetime, closing the database."""
        await self.aclose()
