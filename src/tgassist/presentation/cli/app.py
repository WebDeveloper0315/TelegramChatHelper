"""Command line adapter.

A first-class, permanently supported interface, not a temporary harness
(ADR-030). It gives every milestone a demonstrable result, provides the
end-to-end suite a driver that needs no GUI automation, and -- by being a second
presentation adapter -- keeps the application layer honest about not depending
on any particular front end.

This module imports the application layer and the domain, never infrastructure.
Filesystem and configuration detail is reached through the container, which is
the only component permitted to construct adapters.

Commands grow with each milestone. Milestone 0 provides configuration
inspection and environment diagnosis.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from tgassist import __version__
from tgassist.application.container import Container
from tgassist.application.use_cases.account import CreateAccountRequest
from tgassist.application.use_cases.backfill import BackfillReport, BackfillStop
from tgassist.application.use_cases.contact import ContactTransition
from tgassist.application.use_cases.live import LiveOutcome
from tgassist.application.use_cases.message import IncomingMessage
from tgassist.application.use_cases.suggestion import GeneratedSuggestion
from tgassist.application.use_cases.sync import SyncReport
from tgassist.application.use_cases.user_profile import ProfileChanges
from tgassist.domain.errors import (
    AppError,
    ConfigurationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.ai import AiCall, PromptVersion
from tgassist.domain.model.chat import AiProcessingMode, Chat, ChatType
from tgassist.domain.model.identifiers import AccountId, TelegramChatId
from tgassist.domain.model.memory import IMPORTANCE_LEVELS, Importance
from tgassist.domain.model.message import MessageType, SenderKind
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.tdlib import (
    Architecture,
    DependencyVerdict,
    TdlibRuntime,
    VerificationOutcome,
)
from tgassist.domain.model.user_profile import (
    EmojiUsage,
    MessageLength,
    TimeRange,
    TonePreference,
)
from tgassist.domain.ports.telegram_gateway import DEFAULT_CHAT_LIMIT, DEFAULT_CONTACT_LIMIT
from tgassist.domain.services.sensitivity import is_sensitive_key
from tgassist.presentation.cli.authorization import ConsoleAuthorizationHandler

MASKED = "********"

EXIT_ERROR = 1
EXIT_CONFIG_ERROR = 2

MIN_PYTHON = (3, 12)

# Subsystems that do not exist yet. Reported explicitly so that `doctor` output
# is an honest picture of what has and has not been checked, rather than a
# green result that quietly omits everything unimplemented.
PENDING_SUBSYSTEMS = (
    "Telegram library",
    "AI providers",
    "Prompt registry",
)

app = typer.Typer(
    name="tgassist",
    help="Privacy-first AI conversation assistant for Telegram.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")
db_app = typer.Typer(help="Inspect and migrate the database.", no_args_is_help=True)
app.add_typer(db_app, name="db")
account_app = typer.Typer(help="Manage Telegram accounts.", no_args_is_help=True)
app.add_typer(account_app, name="account")
profile_app = typer.Typer(help="Manage operator preferences.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")
contact_app = typer.Typer(help="Manage the people an account knows.", no_args_is_help=True)
app.add_typer(contact_app, name="contact")
chat_app = typer.Typer(help="Manage conversations and what may be done with them.")
app.add_typer(chat_app, name="chat")
message_app = typer.Typer(help="Ingest and read conversation history.", no_args_is_help=True)
app.add_typer(message_app, name="message")
tdlib_app = typer.Typer(help="Inspect the native Telegram library.", no_args_is_help=True)
app.add_typer(tdlib_app, name="tdlib")
# Distinct from `chat`, which manages what this application has stored. These
# commands read Telegram and store nothing, and one word is what keeps the two
# from being confused at the moment a user types them.
telegram_app = typer.Typer(help="Read directly from Telegram.", no_args_is_help=True)
app.add_typer(telegram_app, name="telegram")
# Separate from `telegram`, which reads and stores nothing. These commands
# write, and the distinction is the one a user most needs to see before running
# something against their own account.
sync_app = typer.Typer(help="Copy Telegram state into the local database.", no_args_is_help=True)
app.add_typer(sync_app, name="sync")
# Conversations are derived from stored messages, so these commands reach
# Telegram not at all -- `rebuild` recomputes, it does not fetch.
conversation_app = typer.Typer(help="Inspect and recompute conversations.", no_args_is_help=True)
app.add_typer(conversation_app, name="conversation")
# The AI boundary. `run` is the only command in this application that can send
# content to a third party, and only when a chat has allowed it.
ai_app = typer.Typer(help="Run and inspect AI calls.", no_args_is_help=True)
app.add_typer(ai_app, name="ai")

memory_app = typer.Typer(help="Extract and review candidate memories.", no_args_is_help=True)
app.add_typer(memory_app, name="memory")

# The review queue. Accepting a suggestion records agreement and does
# nothing else: there is no executor anywhere in this application, and
# these commands are given nothing that could send a message (ADR-062).
suggestion_app = typer.Typer(help="Review what the assistant has suggested.", no_args_is_help=True)
app.add_typer(suggestion_app, name="suggestion")

ProfileOption = Annotated[
    str | None,
    typer.Option("--profile", "-p", help="Environment profile to load."),
]
ConfigDirOption = Annotated[
    Path | None,
    typer.Option("--config-dir", help="Directory holding configuration files."),
]
AccountOption = Annotated[
    int | None,
    typer.Option("--account", help="Account to operate on. Defaults to the active one."),
]


@app.command()
def version() -> None:
    """Print the application version."""
    typer.echo(f"tgassist {__version__}")


@config_app.command("show")
def config_show(
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
    origins: Annotated[
        bool, typer.Option("--origins/--no-origins", help="Show where each value came from.")
    ] = True,
) -> None:
    """Print the resolved configuration with every sensitive value masked."""
    container = _open(profile, config_dir)
    loaded = container.loaded_config

    typer.echo(f"profile: {loaded.config.profile.value}")
    typer.echo("")

    if loaded.layers:
        typer.echo("layers applied (in order):")
        for path in loaded.layers:
            typer.echo(f"  {path}")
    else:
        typer.echo("layers applied: none (built-in defaults only)")

    if loaded.missing_layers:
        typer.echo("")
        typer.echo("layers not present (optional):")
        for path in loaded.missing_layers:
            typer.echo(f"  {path}")

    typer.echo("")
    typer.echo("resolved values:")
    for line in _render(loaded.config.model_dump(mode="json"), loaded.origins if origins else {}):
        typer.echo(line)

    typer.echo("")
    typer.echo("resolved paths:")
    paths = loaded.config.paths
    typer.echo(f"  data_dir:     {paths.data_dir}")
    typer.echo(f"  logs_dir:     {loaded.config.log_dir}")
    typer.echo(f"  sessions_dir: {paths.sessions_dir}")
    typer.echo(f"  backups_dir:  {paths.backups_dir}")


@config_app.command("validate")
def config_validate(
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Validate configuration without starting the application."""
    container = _open(profile, config_dir)
    typer.echo(f"Configuration is valid (profile: {container.config.profile.value}).")


@config_app.command("path")
def config_path(
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Print the configuration files that would be read."""
    loaded = _open(profile, config_dir).loaded_config
    for path in loaded.layers:
        typer.echo(f"present  {path}")
    for path in loaded.missing_layers:
        typer.echo(f"absent   {path}")


@app.command()
def doctor(
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Check the environment and report anything that needs attention."""
    container = _open(profile, config_dir)
    checks = _collect_checks(container)

    failures = 0
    for label, status, detail in checks:
        if status is True:
            marker = "ok  "
        elif status is False:
            marker = "FAIL"
            failures += 1
        else:
            marker = "?   "
        typer.echo(f"{marker} {label}: {detail}")

    for label in PENDING_SUBSYSTEMS:
        typer.echo(f"--   {label}: not implemented yet")

    typer.echo("")
    if failures:
        typer.echo(f"{failures} check(s) failed.")
        raise typer.Exit(code=EXIT_ERROR)
    typer.echo("All implemented checks passed.")


def _collect_checks(container: Container) -> list[tuple[str, bool | None, str]]:
    """Run every implemented environment check.

    A ``None`` status means "not verified on this platform", which is reported
    distinctly from a pass: an unverifiable check must not read as a green one.
    """
    config = container.config
    checks: list[tuple[str, bool | None, str]] = [
        (
            "Python version",
            sys.version_info >= MIN_PYTHON,
            f"{sys.version_info.major}.{sys.version_info.minor}",
        ),
        ("Configuration", True, f"profile {config.profile.value}"),
    ]

    try:
        container.ensure_directories()
    except OSError as exc:
        checks.append(("Data directories", False, str(exc)))
    else:
        checks.append(("Data directories", True, str(config.paths.data_dir)))

    checks.extend(
        (f"Permissions: {name}", owner_only, _permission_detail(owner_only))
        for name, owner_only in container.permission_report().items()
    )
    checks.append(("Log directory", config.log_dir.is_dir(), str(config.log_dir)))
    checks.append(_secret_store_check(container))
    checks.append(_schema_check(container))
    return checks


def _schema_check(container: Container) -> tuple[str, bool | None, str]:
    """Report the database schema position without migrating anything.

    ``doctor`` diagnoses; it does not repair. Silently migrating here would make
    a read-only diagnostic command modify the user's data.
    """

    async def probe() -> tuple[str, bool | None, str]:
        try:
            await container.database.connect()
            status = await container.migrations.status()
        except AppError as exc:
            return ("Database", False, exc.message)
        finally:
            await container.database.close()
        detail = f"{status.state.value}, at {status.current_revision or '(empty)'}"
        if status.pending:
            detail += f", {len(status.pending)} migration(s) pending"
        return ("Database", status.can_start, detail)

    return asyncio.run(probe())


def _permission_detail(owner_only: bool | None) -> str:
    if owner_only is None:
        return "not verified on this platform"
    return "owner only" if owner_only else "readable by others"


def _secret_store_check(container: Container) -> tuple[str, bool | None, str]:
    """Report the credential backend, respecting whether it is required."""
    available = asyncio.run(container.secrets.is_available())
    required = container.config.security.require_secret_store
    if available:
        detail = "available"
    elif required:
        detail = "unavailable, and required by configuration"
    else:
        detail = "unavailable, not required by this profile"
    return ("Secret store", available or not required, detail)


@db_app.command("status")
def db_status(
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Report the database schema position without changing anything."""
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.database.connect()
        status = await container.migrations.status()
        typer.echo(f"database : {container.config.database_path}")
        typer.echo(f"state    : {status.state.value}")
        typer.echo(f"current  : {status.current_revision or '(none)'}")
        typer.echo(f"expected : {status.head_revision}")
        if status.pending:
            typer.echo("")
            typer.echo("pending migrations:")
            for info in status.pending:
                typer.echo(f"  {info.revision}  {info.description}")
        if not status.can_start:
            typer.echo("")
            typer.echo("This database cannot be opened by this version.")

    _run_async(container, run())


@db_app.command("migrate")
def db_migrate(
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
    target: Annotated[str, typer.Option("--target", help="Revision to migrate to.")] = "head",
) -> None:
    """Apply pending migrations."""
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.database.connect()
        report = await container.migrations.upgrade(target)
        if not report.changed:
            typer.echo(f"Already at {report.to_revision}; nothing to do.")
            return
        typer.echo(
            f"Migrated {report.from_revision or '(empty)'} -> {report.to_revision} "
            f"in {report.duration_seconds}s."
        )
        if not report.backup_taken:
            typer.echo("No backup was taken: the backup subsystem is not implemented yet.")

    _run_async(container, run())


@db_app.command("downgrade")
def db_downgrade(
    target: Annotated[str, typer.Argument(help="Revision to revert to, or 'base'.")],
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Revert migrations. Intended for development and for backing out a release."""
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.database.connect()
        report = await container.migrations.downgrade(target)
        typer.echo(f"Reverted {report.from_revision} -> {report.to_revision or '(empty)'}.")

    _run_async(container, run())


@db_app.command("check")
def db_check(
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Run a database health check."""
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.database.connect()
        health = await container.database_health()
        typer.echo(f"reachable      : {health.reachable}")
        typer.echo(f"integrity      : {'ok' if health.integrity_ok else 'FAILED'}")
        typer.echo(f"foreign keys   : {'ok' if health.foreign_keys_ok else 'FAILED'}")
        if health.pragmas:
            typer.echo(f"journal mode   : {health.pragmas.journal_mode}")
            typer.echo(f"fk enforcement : {health.pragmas.foreign_keys}")
            typer.echo(f"busy timeout   : {health.pragmas.busy_timeout_ms} ms")
            typer.echo(f"synchronous    : {health.pragmas.synchronous}")
        typer.echo(f"schema revision: {health.schema_revision or '(none)'}")
        typer.echo(f"size           : {health.size_bytes} bytes")
        typer.echo(f"free pages     : {health.freelist_pages}")
        for problem in health.problems:
            typer.echo(f"problem        : {problem}")
        typer.echo("")
        typer.echo("Healthy." if health.healthy else "Problems were found.")
        if not health.healthy:
            raise typer.Exit(code=EXIT_ERROR)

    _run_async(container, run())


def _run_async(container: Container, coro: Coroutine[Any, Any, None]) -> None:
    """Run a coroutine, translating domain errors and always closing the database."""

    async def wrapper() -> None:
        try:
            await coro
        finally:
            await container.aclose()

    try:
        asyncio.run(wrapper())
    except AppError as exc:
        typer.echo(f"Error: {exc.user_message}", err=True)
        typer.echo(f"  {exc.code}: {exc.message}", err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc


@account_app.command("create")
def account_create(
    telegram_user_id: Annotated[
        int, typer.Argument(help="The Telegram user id this account belongs to.")
    ],
    display_name: Annotated[str, typer.Argument(help="A label for this account.")],
    timezone: Annotated[
        str, typer.Option("--timezone", "-t", help="IANA timezone identifier.")
    ] = "UTC",
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Add an account. The first account created becomes the active one.

    Telegram authentication does not exist yet, so the identifier is supplied
    rather than discovered. From Milestone 2 it comes from the logged-in
    session, and this command becomes a development affordance.
    """
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        account = await container.create_account().execute(
            CreateAccountRequest(
                telegram_user_id=telegram_user_id,
                display_name=display_name,
                timezone=timezone,
            )
        )
        typer.echo(f"Created account {account.id} ({account.display_name}).")
        if account.is_active:
            typer.echo("It is now the active account.")

    _run_async(container, run())


@account_app.command("show")
def account_show(
    account_id: Annotated[
        int | None,
        typer.Argument(help="Account to show. Omit for the active account."),
    ] = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show one account."""
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        account = await container.get_account().execute(account_id)
        if account is None:
            typer.echo("No account found." if account_id else "No account is active.")
            raise typer.Exit(code=EXIT_ERROR)
        typer.echo(f"id               : {account.id}")
        typer.echo(f"telegram user id : {account.telegram_user_id}")
        typer.echo(f"display name     : {account.display_name}")
        typer.echo(f"timezone         : {account.timezone}")
        typer.echo(f"active           : {account.is_active}")
        typer.echo(f"created          : {account.created_at.isoformat()}")
        typer.echo(f"updated          : {account.updated_at.isoformat()}")

    _run_async(container, run())


@account_app.command("list")
def account_list(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Rows per page.")] = 20,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """List accounts, newest first."""
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        page = await container.list_accounts().execute(PageRequest(limit=limit))
        if not page:
            typer.echo("No accounts.")
            return
        for account in page:
            marker = "*" if account.is_active else " "
            typer.echo(
                f"{marker} {account.id:>6}  {account.display_name:<24} "
                f"{account.timezone:<20} telegram:{account.telegram_user_id}"
            )
        typer.echo("")
        typer.echo("* = active account")
        if page.has_more:
            typer.echo("More accounts available; raise --limit to see them.")

    _run_async(container, run())


@account_app.command("activate")
def account_activate(
    account_id: Annotated[int, typer.Argument(help="Account to make active.")],
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Switch which account the application operates."""
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        account = await container.set_active_account().execute(account_id)
        typer.echo(f"Account {account.id} ({account.display_name}) is now active.")

    _run_async(container, run())


@profile_app.command("show")
def profile_show(
    account_id: Annotated[
        int | None, typer.Option("--account", help="Account. Defaults to the active one.")
    ] = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show the operator preferences for an account.

    Creates a profile with defaults on first access, so this always succeeds for
    an account that exists -- adding an account should not require deciding
    preferences before anything works.
    """
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        found = await container.get_user_profile().execute(
            AccountId(account_id) if account_id is not None else None
        )
        typer.echo(f"account          : {found.account_id}")
        typer.echo(f"language         : {found.primary_language}")
        typer.echo(f"tone             : {found.tone_preference.value}")
        typer.echo(f"message length   : {found.preferred_message_length.value}")
        typer.echo(f"emoji            : {found.emoji_usage.value}")
        typer.echo(f"quiet hours      : {found.quiet_hours}")
        typer.echo(f"created          : {found.created_at.isoformat()}")
        typer.echo(f"updated          : {found.updated_at.isoformat()}")

    _run_async(container, run())


@profile_app.command("set")
def profile_set(  # noqa: PLR0913 - one option per settable preference
    *,
    language: Annotated[
        str | None, typer.Option("--language", "-l", help="Primary language, e.g. en or en-GB.")
    ] = None,
    tone: Annotated[
        TonePreference | None, typer.Option("--tone", help="Register for suggested replies.")
    ] = None,
    length: Annotated[
        MessageLength | None, typer.Option("--length", help="Preferred reply length.")
    ] = None,
    emoji: Annotated[
        EmojiUsage | None, typer.Option("--emoji", help="How freely to use emoji.")
    ] = None,
    quiet_hours: Annotated[
        str | None,
        typer.Option("--quiet-hours", help="When not to prompt, as HH:MM-HH:MM."),
    ] = None,
    account_id: Annotated[
        int | None, typer.Option("--account", help="Account. Defaults to the active one.")
    ] = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Change one or more preferences. Options left out are left alone."""
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        changes = ProfileChanges(
            primary_language=language,
            tone_preference=tone,
            preferred_message_length=length,
            emoji_usage=emoji,
            quiet_hours=_parse_quiet_hours(quiet_hours),
        )
        if changes.is_empty:
            typer.echo("Nothing to change. Pass at least one option; see --help.")
            return

        updated = await container.update_user_profile().execute(
            changes, AccountId(account_id) if account_id is not None else None
        )
        typer.echo(f"Updated preferences for account {updated.account_id}.")
        typer.echo(f"  language       : {updated.primary_language}")
        typer.echo(f"  tone           : {updated.tone_preference.value}")
        typer.echo(f"  message length : {updated.preferred_message_length.value}")
        typer.echo(f"  emoji          : {updated.emoji_usage.value}")
        typer.echo(f"  quiet hours    : {updated.quiet_hours}")

    _run_async(container, run())


def _parse_quiet_hours(value: str | None) -> TimeRange | None:
    """Parse ``HH:MM-HH:MM`` into a time range.

    Raises:
        DomainValidationError: If the value is not two times separated by a
            hyphen. Parsing here rather than in the domain keeps the entity
            free of input formats, while still reporting the failure through
            the same error taxonomy as any other invalid value.
    """
    if value is None:
        return None
    parts = value.split("-")
    if len(parts) != 2:  # noqa: PLR2004 - a range has exactly two ends
        msg = f"{value!r} is not a time range"
        raise DomainValidationError(
            msg,
            user_message=(
                f"{value!r} is not a time range. Use HH:MM-HH:MM, for example 22:00-08:00."
            ),
        )
    return TimeRange.from_clock(parts[0], parts[1])


@contact_app.command("add")
def contact_add(  # noqa: PLR0913, PLR0917 - Typer reads the options from the signature
    telegram_user_id: Annotated[int, typer.Argument(help="Telegram user identifier.")],
    display_name: Annotated[str, typer.Argument(help="Name to show for this person.")],
    username: Annotated[
        str | None, typer.Option("--username", "-u", help="Telegram handle, with or without @.")
    ] = None,
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Record a person this account knows.

    Telegram synchronisation does not exist yet, so the identifier is supplied
    rather than discovered. From Milestone 3 contacts arrive from the account's
    chat list, and this command becomes a development affordance.
    """
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        contact = await container.create_contact().execute(
            telegram_user_id=telegram_user_id,
            display_name=display_name,
            username=username,
            account_id=AccountId(account_id) if account_id is not None else None,
        )
        handle = f" (@{contact.username})" if contact.username else ""
        typer.echo(f"Added contact {contact.id}: {contact.display_name}{handle}.")

    _run_async(container, run())


@contact_app.command("show")
def contact_show(
    contact_id: Annotated[int, typer.Argument(help="Contact to show.")],
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show one contact, including a deleted one."""
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        # Deleted contacts are shown here deliberately: somebody asking for a
        # specific identifier wants to know it was deleted, not to be told it
        # does not exist.
        contact = await container.get_contact().execute(
            contact_id,
            account_id=AccountId(account_id) if account_id is not None else None,
            include_deleted=True,
        )
        if contact is None:
            typer.echo("No such contact.")
            raise typer.Exit(code=EXIT_ERROR)
        typer.echo(f"id               : {contact.id}")
        typer.echo(f"account          : {contact.account_id}")
        typer.echo(f"telegram user id : {contact.telegram_user_id}")
        typer.echo(f"username         : {contact.username or '(none)'}")
        typer.echo(f"display name     : {contact.display_name}")
        typer.echo(f"status           : {contact.status}")
        typer.echo(f"created          : {contact.created_at.isoformat()}")
        typer.echo(f"updated          : {contact.updated_at.isoformat()}")

    _run_async(container, run())


@contact_app.command("list")
def contact_list(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Rows per page.")] = 20,
    include_archived: Annotated[
        bool, typer.Option("--archived/--no-archived", help="Include archived contacts.")
    ] = False,
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """List an account's contacts, newest first.

    Deleted contacts are never listed. Archived ones appear only with
    ``--archived``, because the reason to archive somebody is to stop seeing
    them.
    """
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        page = await container.list_contacts().execute(
            PageRequest(limit=limit),
            account_id=AccountId(account_id) if account_id is not None else None,
            include_archived=include_archived,
        )
        if not page:
            typer.echo("No contacts." if not include_archived else "No contacts, archived or not.")
            return
        for contact in page:
            marker = "-" if contact.is_archived else " "
            handle = f"@{contact.username}" if contact.username else ""
            typer.echo(
                f"{marker} {contact.id:>6}  {contact.display_name:<24} "
                f"{handle:<20} telegram:{contact.telegram_user_id}"
            )
        if include_archived:
            typer.echo("")
            typer.echo("- = archived")
        if page.has_more:
            typer.echo("More contacts available; raise --limit to see them.")

    _run_async(container, run())


@contact_app.command("archive")
def contact_archive(
    contact_id: Annotated[int, typer.Argument(help="Contact to archive.")],
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Hide a contact from the default list, keeping everything."""
    _change_contact(
        ContactTransition.ARCHIVE,
        contact_id,
        account_id=account_id,
        profile=profile,
        config_dir=config_dir,
    )


@contact_app.command("restore")
def contact_restore(
    contact_id: Annotated[int, typer.Argument(help="Contact to restore.")],
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Return a contact to active, whether archived or deleted."""
    _change_contact(
        ContactTransition.RESTORE,
        contact_id,
        account_id=account_id,
        profile=profile,
        config_dir=config_dir,
    )


@contact_app.command("delete")
def contact_delete(
    contact_id: Annotated[int, typer.Argument(help="Contact to delete.")],
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Delete a contact.

    Soft deletion: the row and its history stay until the purge described in
    ``PRIVACY.md`` section 7 removes them, and ``contact restore`` undoes this.
    """
    _change_contact(
        ContactTransition.DELETE,
        contact_id,
        account_id=account_id,
        profile=profile,
        config_dir=config_dir,
    )


def _change_contact(
    transition: ContactTransition,
    contact_id: int,
    *,
    account_id: int | None,
    profile: str | None,
    config_dir: Path | None,
) -> None:
    """Apply a lifecycle transition and report the result.

    One helper for three commands: they differ only in the transition, and three
    copies of this body would be three places for the reporting to drift.
    """
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        contact = await container.change_contact_status().execute(
            contact_id,
            transition,
            account_id=AccountId(account_id) if account_id is not None else None,
        )
        typer.echo(f"Contact {contact.id} ({contact.display_name}) is now {contact.status}.")

    _run_async(container, run())


@chat_app.command("open")
def chat_open(  # noqa: PLR0913, PLR0917 - Typer reads the options from the signature
    telegram_chat_id: Annotated[
        int,
        typer.Argument(
            help=(
                "Telegram chat identifier. Negative for groups and channels, "
                "which the shell reads as an option -- put it after -- "
                "(chat open --type group --title T -- -1001234)."
            ),
        ),
    ],
    contact_id: Annotated[
        int | None,
        typer.Option("--contact", "-c", help="Contact this private chat is with."),
    ] = None,
    title: Annotated[
        str | None, typer.Option("--title", "-t", help="Title, for a non-private chat.")
    ] = None,
    chat_type: Annotated[
        ChatType | None, typer.Option("--type", help="Kind of chat. Defaults to private.")
    ] = None,
    no_sync: Annotated[
        bool, typer.Option("--no-sync", help="Do not synchronise this chat.")
    ] = False,
    ai_mode: Annotated[
        AiProcessingMode | None,
        typer.Option("--ai", help="What AI may do with this chat. Defaults to local-only."),
    ] = None,
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Record a conversation.

    Telegram synchronisation does not exist yet, so the chat is described rather
    than discovered. From Milestone 3 chats arrive from the account's chat list,
    and this command becomes a development affordance.

    A private chat needs ``--contact`` and takes no title; every other kind needs
    ``--title`` and takes no contact.
    """
    container = _open(profile, config_dir)
    resolved_type = chat_type or ChatType.PRIVATE
    scope = AccountId(account_id) if account_id is not None else None

    async def run() -> None:
        await container.start()
        if resolved_type is ChatType.PRIVATE:
            if contact_id is None:
                typer.echo(
                    "A private chat needs --contact. Use --type to record another kind.",
                    err=True,
                )
                raise typer.Exit(code=EXIT_ERROR)
            if title is not None:
                typer.echo(
                    "A private chat takes its name from the contact; drop --title.", err=True
                )
                raise typer.Exit(code=EXIT_ERROR)
            chat = await container.open_private_chat().execute(
                contact_id=contact_id,
                telegram_chat_id=telegram_chat_id,
                account_id=scope,
                sync_enabled=not no_sync,
                ai_processing_mode=ai_mode or AiProcessingMode.LOCAL_ONLY,
            )
        else:
            if title is None:
                typer.echo(f"A {resolved_type.value} chat needs --title.", err=True)
                raise typer.Exit(code=EXIT_ERROR)
            if contact_id is not None:
                typer.echo("Only a private chat can name a single contact.", err=True)
                raise typer.Exit(code=EXIT_ERROR)
            chat = await container.open_group_chat().execute(
                telegram_chat_id=telegram_chat_id,
                title=title,
                chat_type=resolved_type,
                account_id=scope,
                sync_enabled=not no_sync,
                ai_processing_mode=ai_mode or AiProcessingMode.LOCAL_ONLY,
            )
        typer.echo(f"Opened {chat.chat_type.value} chat {chat.id} ({_chat_label(chat)}).")

    _run_async(container, run())


@chat_app.command("show")
def chat_show(
    chat_id: Annotated[
        int | None, typer.Argument(help="Chat to show. Omit and use --contact instead.")
    ] = None,
    contact_id: Annotated[
        int | None, typer.Option("--contact", "-c", help="Show the private chat with a contact.")
    ] = None,
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show one chat, by identifier or by the contact it is with."""
    container = _open(profile, config_dir)
    scope = AccountId(account_id) if account_id is not None else None

    async def run() -> None:
        await container.start()
        if (chat_id is None) == (contact_id is None):
            typer.echo("Give a chat identifier or --contact, not both.", err=True)
            raise typer.Exit(code=EXIT_ERROR)

        use_case = container.get_chat()
        chat = (
            await use_case.execute(chat_id, account_id=scope)
            if chat_id is not None
            else await use_case.with_contact(contact_id or 0, account_id=scope)
        )
        if chat is None:
            typer.echo("No such chat.")
            raise typer.Exit(code=EXIT_ERROR)
        typer.echo(f"id               : {chat.id}")
        typer.echo(f"account          : {chat.account_id}")
        typer.echo(f"telegram chat id : {chat.telegram_chat_id}")
        typer.echo(f"type             : {chat.chat_type.value}")
        typer.echo(f"contact          : {chat.contact_id or '(none)'}")
        typer.echo(f"title            : {chat.title or '(none)'}")
        typer.echo(f"sync             : {chat.sync_enabled}")
        typer.echo(f"ai processing    : {chat.ai_processing_mode.value}")
        typer.echo(f"created          : {chat.created_at.isoformat()}")
        typer.echo(f"updated          : {chat.updated_at.isoformat()}")

    _run_async(container, run())


@chat_app.command("list")
def chat_list(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Rows per page.")] = 20,
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """List an account's chats, newest first."""
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        page = await container.list_chats().execute(
            PageRequest(limit=limit),
            account_id=AccountId(account_id) if account_id is not None else None,
        )
        if not page:
            typer.echo("No chats.")
            return
        for chat in page:
            sync = "sync" if chat.sync_enabled else "----"
            typer.echo(
                f"  {chat.id:>6}  {_chat_label(chat):<28} {chat.chat_type.value:<11} "
                f"{sync}  ai:{chat.ai_processing_mode.value}"
            )
        if page.has_more:
            typer.echo("More chats available; raise --limit to see them.")

    _run_async(container, run())


@chat_app.command("set")
def chat_set(  # noqa: PLR0913, PLR0917 - Typer reads the options from the signature
    chat_id: Annotated[int, typer.Argument(help="Chat to change.")],
    sync: Annotated[
        bool | None, typer.Option("--sync/--no-sync", help="Synchronise this chat.")
    ] = None,
    ai_mode: Annotated[
        AiProcessingMode | None, typer.Option("--ai", help="What AI may do with this chat.")
    ] = None,
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Change what may be done with a chat.

    ``--ai disabled`` is how a user answers "stop using AI on our chats", and
    ``--ai local_only`` -- the default -- keeps content on this device.
    """
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        chat = await container.set_chat_policy().execute(
            chat_id,
            sync_enabled=sync,
            ai_processing_mode=ai_mode,
            account_id=AccountId(account_id) if account_id is not None else None,
        )
        typer.echo(f"Chat {chat.id} ({_chat_label(chat)}):")
        typer.echo(f"  sync          : {chat.sync_enabled}")
        typer.echo(f"  ai processing : {chat.ai_processing_mode.value}")

    _run_async(container, run())


def _chat_label(chat: Chat) -> str:
    """Describe a chat in one phrase.

    A private chat has no title of its own -- its name lives on the contact --
    so it is identified by whom it is with. Resolving the contact's name would
    mean a second query for a label, which the listing does not need.
    """
    return chat.title if chat.title is not None else f"contact {chat.contact_id}"


@message_app.command("ingest")
def message_ingest(  # noqa: PLR0913, PLR0917 - Typer reads the options from the signature
    chat_id: Annotated[int, typer.Argument(help="Chat to ingest into.")],
    text: Annotated[str, typer.Argument(help="What the message says.")],
    sender: Annotated[SenderKind, typer.Option("--from", help="Who sent it.")] = SenderKind.CONTACT,
    sent_at: Annotated[
        str | None,
        typer.Option(
            "--sent-at",
            help=(
                "When it was sent, ISO 8601. An offset is honoured; a value "
                "without one is read as UTC. Defaults to now."
            ),
        ),
    ] = None,
    telegram_message_id: Annotated[
        int | None,
        typer.Option(
            "--telegram-id",
            help="Its identifier in Telegram. Supplying one makes re-ingestion a no-op.",
        ),
    ] = None,
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Ingest one message.

    The same pipeline synchronisation will use in Milestone 3. Supplying
    ``--telegram-id`` makes the ingestion idempotent: running the command twice
    stores one message and reports the second as already present.
    """
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        moment = _parse_instant(sent_at) if sent_at is not None else container.clock.now()
        report = await container.ingest_messages().execute(
            chat_id,
            [
                IncomingMessage(
                    sender_kind=sender,
                    sent_at=moment,
                    text=text,
                    message_type=MessageType.TEXT,
                    telegram_message_id=telegram_message_id,
                )
            ],
            account_id=AccountId(account_id) if account_id is not None else None,
        )
        if report.stored:
            typer.echo(f"Ingested message {report.message_ids[0]}.")
        else:
            typer.echo("Already ingested; nothing stored.")

    _run_async(container, run())


@message_app.command("history")
def message_history(
    chat_id: Annotated[int, typer.Argument(help="Chat to read.")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Messages per page.")] = 20,
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show a chat's history, newest first.

    Ordered by when each message was sent rather than by when it was ingested,
    because a backfill stores old messages after new ones.
    """
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        page = await container.read_chat_history().execute(
            chat_id,
            PageRequest(limit=limit),
            account_id=AccountId(account_id) if account_id is not None else None,
        )
        if not page:
            typer.echo("No messages.")
            return
        for message in page:
            marker = ">" if message.is_outgoing else "<"
            when = message.sent_at.isoformat(timespec="minutes")
            typer.echo(f"{marker} {when}  {message.id:>6}  {_preview(message.text)}")
        if page.has_more:
            typer.echo("More messages available; raise --limit to see them.")

    _run_async(container, run())


@message_app.command("show")
def message_show(
    message_id: Annotated[int, typer.Argument(help="Message to show.")],
    account_id: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show one message in full."""
    container = _open(profile, config_dir)

    async def run() -> None:
        await container.start()
        message = await container.get_message().execute(
            message_id,
            account_id=AccountId(account_id) if account_id is not None else None,
        )
        if message is None:
            typer.echo("No such message.")
            raise typer.Exit(code=EXIT_ERROR)
        typer.echo(f"id            : {message.id}")
        typer.echo(f"chat          : {message.chat_id}")
        typer.echo(f"telegram id   : {message.telegram_message_id or '(none)'}")
        typer.echo(f"from          : {message.sender_kind.value}")
        typer.echo(f"type          : {message.message_type.value}")
        typer.echo(f"sent          : {message.sent_at.isoformat()}")
        typer.echo(f"ingested      : {message.ingested_at.isoformat()}")
        typer.echo("")
        typer.echo(message.text or "(no text)")

    _run_async(container, run())


def _parse_instant(value: str) -> datetime:
    """Parse an ISO 8601 timestamp, reading a naive value as UTC.

    Typer's own ``datetime`` type accepts three fixed formats, none carrying a
    timezone offset -- so an import tool supplying a real ISO 8601 timestamp
    would have been rejected. ``fromisoformat`` accepts the whole grammar.

    Raises:
        DomainValidationError: If the value is not a timestamp.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"{value!r} is not an ISO 8601 timestamp"
        raise DomainValidationError(
            msg,
            user_message=(
                f"{value!r} is not a timestamp. Use ISO 8601, "
                f"for example 2026-01-31T09:30:00+00:00."
            ),
            cause=exc,
        ) from exc
    # A naive value is read as UTC rather than as local time: the application
    # stores UTC throughout, and guessing a local zone here would silently
    # shift every imported message.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


PREVIEW_LENGTH = 60


def _preview(text: str | None) -> str:
    """Render a message's text as one line for a listing."""
    if text is None:
        return "(no text)"
    flattened = " ".join(text.split())
    if len(flattened) <= PREVIEW_LENGTH:
        return flattened
    return flattened[: PREVIEW_LENGTH - 1] + "\u2026"


@tdlib_app.command("doctor")
def tdlib_doctor(
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Check the Telegram library end to end and report what is wrong.

    Performs real work: it searches, hashes, loads and queries the library. It
    does not print configuration back.
    """
    runtime = _open(profile, config_dir).tdlib_runtime()

    typer.echo(f"platform     : {runtime.platform.key}")
    typer.echo(f"interpreter  : {runtime.expected_architecture.value}")
    typer.echo(f"looking for  : {runtime.platform.library_filename}")
    typer.echo(
        f"manifest     : {runtime.manifest_entries} trusted entr"
        f"{'y' if runtime.manifest_entries == 1 else 'ies'} for this platform"
    )
    typer.echo("")

    typer.echo("search:")
    for candidate in runtime.candidates:
        marker = "found" if candidate.exists else "  -  "
        location = str(candidate.path) if candidate.path else "(nothing)"
        typer.echo(f"  {marker}  {candidate.source.value:<11} {location}")
        if not candidate.exists:
            typer.echo(f"         {candidate.detail}")
    typer.echo("")

    for label, status, detail in _tdlib_checks(runtime):
        marker = "ok  " if status is True else ("FAIL" if status is False else "--  ")
        typer.echo(f"{marker} {label}: {detail}")

    if runtime.dependencies is not None and runtime.dependencies.system:
        typer.echo("")
        typer.echo("runtime dependencies:")
        for name in runtime.dependencies.system:
            typer.echo(f"  system         {name}")
        for name in runtime.dependencies.redistributable:
            typer.echo(f"  redistributable {name}")
        for name in runtime.dependencies.forbidden:
            typer.echo(f"  FORBIDDEN      {name}")
        for name in runtime.dependencies.unrecognised:
            typer.echo(f"  UNRECOGNISED   {name}")

    if runtime.is_usable:
        typer.echo("")
        typer.echo("The Telegram library is ready.")
        return

    typer.echo("")
    typer.echo(f"Problem: {runtime.problem}")
    if runtime.remedy:
        typer.echo("")
        typer.echo("To fix:")
        for step in runtime.remedy:
            for line in step.splitlines():
                typer.echo(f"  {line}")
    raise typer.Exit(code=EXIT_ERROR)


@tdlib_app.command("version")
def tdlib_version(
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Print the version the library reports.

    Obtained by loading it and asking, not from configuration.
    """
    runtime = _open(profile, config_dir).tdlib_runtime()

    if runtime.version is None:
        typer.echo(f"No version available: {runtime.problem}", err=True)
        raise typer.Exit(code=EXIT_ERROR)

    typer.echo(f"TDLib {runtime.version}")
    typer.echo(f"minimum supported: {runtime.minimum_version}")
    typer.echo(f"library: {runtime.library_path}")


@tdlib_app.command("verify")
def tdlib_verify(
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Check the library's digest against the pinned manifest.

    On a digest the manifest does not hold, prints the entry to add -- after
    establishing where the file came from. Verification is a claim about
    provenance, and only a person can make it (ADR-047).
    """
    runtime = _open(profile, config_dir).tdlib_runtime()

    if runtime.selected is None or runtime.library_path is None:
        typer.echo("No library to verify.", err=True)
        for step in runtime.remedy:
            typer.echo(f"  {step}", err=True)
        raise typer.Exit(code=EXIT_ERROR)

    typer.echo(f"library : {runtime.library_path}")
    typer.echo(f"platform: {runtime.platform.key}")
    typer.echo(f"sha256  : {runtime.digest or '(unreadable)'}")
    typer.echo("")

    if runtime.verification is VerificationOutcome.VERIFIED:
        typer.echo("Verified: this digest is in the pinned manifest.")
        return

    typer.echo("NOT VERIFIED: this digest is not in the pinned manifest.", err=True)
    typer.echo("", err=True)
    for step in runtime.remedy:
        for line in step.splitlines():
            typer.echo(f"  {line}", err=True)
    raise typer.Exit(code=EXIT_ERROR)


def _not_checked(*labels: str) -> list[tuple[str, bool | None, str]]:
    """Mark stages that were never reached.

    Distinct from a failure: not checked and failed send people to different
    places, and reporting one as the other wastes their time.
    """
    return [(label, None, "not checked") for label in labels]


def _architecture_check(runtime: TdlibRuntime) -> tuple[str, bool | None, str]:
    """Describe whether the library matches the interpreter."""
    if runtime.architecture is Architecture.UNKNOWN:
        return ("Architecture", None, "not a format this can read")
    if runtime.architecture_matches:
        return ("Architecture", True, f"{runtime.architecture.value}, matches this interpreter")
    return (
        "Architecture",
        False,
        f"{runtime.architecture.value}, but this interpreter is "
        f"{runtime.expected_architecture.value}",
    )


def _dependency_check(runtime: TdlibRuntime) -> tuple[str, bool | None, str]:
    """Describe what the library loads at runtime."""
    report = runtime.dependencies
    if report is None or report.verdict is DependencyVerdict.NOT_CHECKED:
        detail = report.detail if report is not None else "not read"
        return ("Dependencies", None, detail)
    return ("Dependencies", report.is_acceptable, report.detail)


def _tdlib_checks(  # noqa: PLR0911 - one exit per stage, so later stages read "not checked"
    runtime: TdlibRuntime,
) -> list[tuple[str, bool | None, str]]:
    """Describe each stage of the inspection, in the order it was performed.

    A stage that was never reached reports ``None`` rather than a failure: not
    checked and failed are different things, and conflating them sends people
    to fix the wrong stage.
    """
    found = runtime.selected is not None
    checks: list[tuple[str, bool | None, str]] = [
        (
            "Library found",
            found,
            str(runtime.library_path) if found else "no candidate exists",
        )
    ]

    if not found:
        checks.extend(
            _not_checked(
                "Checksum verified",
                "Architecture",
                "Dependencies",
                "Loaded",
                "Client API",
                "Version",
            )
        )
        return checks

    verified = runtime.is_verified
    checks.append(
        (
            "Checksum verified",
            verified,
            f"sha256 {runtime.digest[:16]}..."
            if verified and runtime.digest
            else "digest not in the manifest",
        )
    )
    if not verified:
        checks.extend(
            _not_checked("Architecture", "Dependencies", "Loaded", "Client API", "Version")
        )
        return checks

    checks.append(_architecture_check(runtime))
    if runtime.architecture is not Architecture.UNKNOWN and not runtime.architecture_matches:
        checks.extend(_not_checked("Dependencies", "Loaded", "Client API", "Version"))
        return checks

    checks.append(_dependency_check(runtime))
    if runtime.dependencies is not None and runtime.dependencies.verdict in {
        DependencyVerdict.FORBIDDEN,
        DependencyVerdict.UNRECOGNISED,
    }:
        checks.extend(_not_checked("Loaded", "Client API", "Version"))
        return checks

    checks.append(
        ("Loaded", runtime.loaded, "opened" if runtime.loaded else "the platform refused it")
    )
    if not runtime.loaded:
        checks.extend([("Client API", None, "not checked"), ("Version", None, "not checked")])
        return checks

    complete = not runtime.missing_symbols
    checks.append(
        (
            "Client API",
            complete,
            "all entry points present"
            if complete
            else f"missing {', '.join(runtime.missing_symbols)}",
        )
    )
    if not complete:
        checks.append(("Version", None, "not checked"))
        return checks

    checks.append(
        (
            "Version",
            runtime.version is not None and runtime.problem is None,
            f"{runtime.version} (minimum {runtime.minimum_version})"
            if runtime.version
            else "the library did not answer",
        )
    )
    return checks


@app.command("login")
def login(
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Sign an account in to Telegram.

    Prompts only for what Telegram asks for. A session that is still valid needs
    nothing, so running this after a restart connects and reports rather than
    asking for a code that was never sent.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        target = account_id or await _active_account_id(container)
        async with container.telegram_for(target) as gateway:
            result = await container.authenticate_account().execute(
                gateway, ConsoleAuthorizationHandler(), target
            )

        if result.was_already_authorized:
            typer.echo(f"Already signed in as {result.user.display_name}.")
        else:
            typer.echo(f"Signed in as {result.user.display_name}.")
        typer.echo(f"  authorization: {result.session.authorization_state.value}")
        typer.echo(f"  connection: {result.session.connection_state.value}")

    _run_async(container, run())


@app.command("logout")
def logout(
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Sign an account out and destroy its local session store and key.

    Irreversible in the way that matters: the encryption key is deleted, so the
    stored session cannot be reopened even if the files survive. Conversation
    history in the database is untouched.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        target = account_id or await _active_account_id(container)

        if not yes and not typer.confirm(
            "Sign out and delete the local Telegram session store and its key?"
        ):
            typer.echo("Cancelled.")
            return

        async with container.telegram_for(target) as gateway:
            await gateway.connect()
            session = await container.log_out_account().execute(gateway, target)

        if session is None:
            typer.echo("That account had no Telegram session.")
            return
        typer.echo("Signed out. The local session store and its key were deleted.")

    _run_async(container, run())


async def _active_account_id(container: Container) -> AccountId:
    """Resolve the account to operate on, failing clearly when there is none.

    The gateway has to be built before the use case runs -- it needs the session
    the use case will update -- so the account cannot be resolved inside the use
    case as it is everywhere else. The message matches the one
    ``resolve_account`` produces, so the user sees the same thing whichever path
    reported it.
    """
    account = await container.get_account().execute(None)
    if account is None:
        msg = "No account is active"
        raise RecordNotFoundError(msg, user_message="No account is active. Create one first.")
    return account.id


@telegram_app.command("chats")
def telegram_chats(
    account: AccountOption = None,
    limit: Annotated[
        int, typer.Option("--limit", "-n", min=1, help="How many chats to show.")
    ] = DEFAULT_CHAT_LIMIT,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """List the chats Telegram has for this account.

    Reads Telegram, not the local database -- nothing here is stored. Use
    `tgassist chat list` for what this application has recorded.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        target = account_id or await _active_account_id(container)
        async with container.telegram_for(target) as gateway:
            await gateway.connect()
            chats = await gateway.list_chats(limit=limit)

        if not chats:
            typer.echo("No chats.")
            return
        for chat in chats:
            unread = f"  {chat.unread_count} unread" if chat.unread_count else ""
            typer.echo(f"{int(chat.id):>15}  {chat.chat_type.value:<11} {chat.title}{unread}")
        typer.echo("")
        typer.echo(f"{len(chats)} chat(s). Nothing was stored.")

    _run_async(container, run())


@telegram_app.command("history")
def telegram_history(
    chat_id: Annotated[int, typer.Argument(help="Telegram chat id, from `telegram chats`.")],
    account: AccountOption = None,
    limit: Annotated[
        int, typer.Option("--limit", "-n", min=1, help="How many messages to show.")
    ] = 20,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show the newest messages in a Telegram chat, without storing them.

    A read, not an import. Ingestion arrives with the synchronisation engine;
    this exists so a chat can be looked at before deciding to sync it.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        target = account_id or await _active_account_id(container)
        async with container.telegram_for(target) as gateway:
            await gateway.connect()
            chat = await gateway.get_chat(TelegramChatId(chat_id))
            if chat is None:
                typer.echo("That chat is not visible to this account.", err=True)
                raise typer.Exit(code=EXIT_ERROR)
            page = await gateway.fetch_history(TelegramChatId(chat_id), limit=limit)

        typer.echo(f"{chat.title} ({chat.chat_type.value})")
        typer.echo("")
        if page.is_empty:
            typer.echo("No messages.")
            return
        # Oldest first: a conversation reads downwards, whatever order the
        # transport returned it in.
        for item in reversed(page.messages):
            who = "you" if item.is_outgoing else "them"
            when = item.sent_at.strftime("%Y-%m-%d %H:%M")
            body = item.text if item.text is not None else f"({item.message_type.value})"
            typer.echo(f"{when}  {who:<5} {body}")
        typer.echo("")
        typer.echo(f"{len(page.messages)} message(s). Nothing was stored.")
        if not page.reached_beginning:
            # Deliberately "may": a short page is not proof of the beginning,
            # which is why only an empty one reports it.
            typer.echo(f"Older messages may continue before {int(page.oldest_message_id or 0)}.")

    _run_async(container, run())


@sync_app.command("contacts")
def sync_contacts(
    account: AccountOption = None,
    limit: Annotated[
        int, typer.Option("--limit", "-n", min=1, help="How many address-book entries to read.")
    ] = DEFAULT_CONTACT_LIMIT,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Record the people in this account's Telegram address book.

    Safe to run repeatedly: a second run over unchanged data writes nothing.
    Nothing is ever deleted, and a contact you archived or deleted stays that
    way.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        target = account_id or await _active_account_id(container)
        async with container.telegram_for(target) as gateway:
            await gateway.connect()
            report = await container.sync_contacts().execute(gateway, target, limit=limit)
        _report(report, "contact")

    _run_async(container, run())


@sync_app.command("chats")
def sync_chats(
    account: AccountOption = None,
    limit: Annotated[
        int, typer.Option("--limit", "-n", min=1, help="How many chats to read.")
    ] = DEFAULT_CHAT_LIMIT,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Record the chats Telegram has for this account, and the people in them.

    Messages are not read: this records who you talk to and where, which is what
    history ingestion will need before it can run. Safe to run repeatedly.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        target = account_id or await _active_account_id(container)
        async with container.telegram_for(target) as gateway:
            await gateway.connect()
            report = await container.sync_chats().execute(gateway, target, limit=limit)
        _report(report, "chat")

    _run_async(container, run())


@sync_app.command("history")
def sync_history(  # noqa: PLR0913, PLR0917 - one parameter per command-line option
    chat: Annotated[
        int | None,
        typer.Argument(help="Local chat id, from `tgassist chat list`. Omit for every chat."),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Continue where the last run stopped. The default; state it to be explicit.",
        ),
    ] = False,
    reset: Annotated[
        bool,
        typer.Option("--reset", help="Discard the bookmark and start again from the newest."),
    ] = False,
    max_batches: Annotated[
        int | None,
        typer.Option("--max-batches", min=1, help="Stop after this many batches, per chat."),
    ] = None,
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Store a chat's history, oldest-ward, one batch at a time.

    Interrupt it whenever you like. Every batch commits its messages and its
    bookmark together, so the next run continues from exactly where this one
    stopped -- it never re-reads what it stored, and never skips what it did not.

    `--reset` starts again from the newest message. It deletes nothing: the
    messages already stored are recognised and skipped, so a reset costs network
    traffic and nothing else.
    """
    if resume and reset:
        typer.echo("--resume and --reset ask for opposite things. Choose one.", err=True)
        raise typer.Exit(code=EXIT_ERROR)

    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        target = account_id or await _active_account_id(container)
        backfill = container.sync_history()
        async with container.telegram_for(target) as gateway:
            await gateway.connect()
            if chat is None:
                reports = await backfill.execute_all(
                    gateway, target, reset=reset, max_batches=max_batches
                )
            else:
                reports = (
                    await backfill.execute(
                        gateway, chat, target, reset=reset, max_batches=max_batches
                    ),
                )
        _backfill_report(reports)

    _run_async(container, run())


@sync_app.command("live")
def sync_live(
    account: AccountOption = None,
    catch_up: Annotated[
        bool,
        typer.Option(
            "--catch-up/--no-catch-up",
            help="Recover what arrived while nothing was running, before following.",
        ),
    ] = True,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Follow Telegram, storing messages as they arrive. Press Ctrl+C to stop.

    Runs until interrupted. Each update is stored in a transaction of its own,
    so stopping at any moment leaves the database consistent -- and the next run
    recovers whatever arrived while this one was not listening.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        target = account_id or await _active_account_id(container)
        typer.echo("Following Telegram. Press Ctrl+C to stop.")

        async with container.telegram_for(target) as gateway:
            await gateway.connect()
            outcome = await container.run_live_sync(gateway, target, catch_up=catch_up)

        _live_report(outcome)

    _run_async(container, run())


def _live_report(outcome: LiveOutcome) -> None:
    """Print what a live run did, and why it stopped."""
    report = outcome.report
    typer.echo("")
    typer.echo(
        f"{report.caught_up} caught up, {report.stored} new, "
        f"{report.skipped} already stored, {report.ignored} ignored."
    )
    typer.echo(f"{report.updates_seen} update(s) seen, {report.events} event(s) published.")

    if report.failed:
        typer.echo(f"{report.failed} update(s) could not be stored:", err=True)
        for failure in report.failures:
            typer.echo(f"  {failure}", err=True)
    if outcome.restarts:
        typer.echo(f"Synchronisation restarted {outcome.restarts} time(s).", err=True)
    if outcome.failure is not None:
        typer.echo(f"Synchronisation stopped: {outcome.failure}", err=True)


@sync_app.command("status")
def sync_status(
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show how much of this account's history is stored.

    Reads the database only. It opens no connection to Telegram, so it reports
    what the last run achieved rather than what is true in Telegram right now.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        report = await container.sync_status().execute(account_id)

        authorization = report.authorization_state
        typer.echo(
            f"Account {int(report.account_id)}: "
            f"{authorization.value if authorization is not None else 'no session'}"
        )
        typer.echo("")
        if not report.chats:
            typer.echo("No chats. Run `tgassist sync chats` first.")
            return

        for chat in report.chats:
            span = (
                f"{int(chat.oldest_synced_message_id or 0)}"
                f"-{int(chat.newest_synced_message_id or 0)}"
                if chat.has_synced
                else "-"
            )
            typer.echo(f"{int(chat.chat_id):>8}  {chat.state:<9} {span:<18} {chat.title}")

        typer.echo("")
        typer.echo(
            f"{report.synchronised} chat(s) fully stored, {report.pending} with more to fetch."
        )
        if not report.is_current:
            typer.echo("Run `tgassist sync history` to continue.")

    _run_async(container, run())


@conversation_app.command("rebuild")
def conversation_rebuild(
    chat: Annotated[int, typer.Argument(help="Local chat id, from `tgassist chat list`.")],
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Recompute a chat's conversations from its stored messages.

    Deterministic and safe to repeat: the same messages always produce the same
    boundaries, so a rebuild that changes nothing reports everything unchanged.
    Run it after changing the gap or the message cap, or after a bulk import
    with segmentation switched off.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        report = await container.segment_conversations().execute(chat, account_id)

        typer.echo(f"{report.messages} message(s) in {report.conversations} conversation(s).")
        typer.echo(
            f"{report.created} new, {report.updated} changed, "
            f"{report.unchanged} unchanged, {report.deleted} removed."
        )
        if not report.changed:
            typer.echo("Nothing changed. The boundaries were already correct.")

    _run_async(container, run())


@conversation_app.command("list")
def conversation_list(
    chat: Annotated[int, typer.Argument(help="Local chat id, from `tgassist chat list`.")],
    limit: Annotated[
        int, typer.Option("--limit", "-n", min=1, help="Conversations per page.")
    ] = 20,
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """List a chat's conversations, newest first."""
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        page = await container.list_conversations().execute(
            chat, PageRequest(limit=limit), account_id=account_id
        )

        if not page.items:
            typer.echo("No conversations. Run `tgassist conversation rebuild` first.")
            return

        for conversation in page:
            typer.echo(
                f"{int(conversation.id):>8}  "
                f"{conversation.started_at:%Y-%m-%d %H:%M} - "
                f"{conversation.ended_at:%H:%M}  "
                f"{conversation.message_count:>4} message(s)"
            )
        typer.echo("")
        typer.echo(f"{len(page.items)} conversation(s).")
        if page.has_more:
            typer.echo("More available; raise --limit to see them.")

    _run_async(container, run())


@conversation_app.command("show")
def conversation_show(
    conversation: Annotated[int, typer.Argument(help="Conversation id, from `conversation list`.")],
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show one conversation and the messages it covers.

    The messages are found by time range rather than by a stored link: a message
    belongs to the conversation whose span contains it, and conversations do not
    overlap.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        found = await container.get_conversation().execute(conversation, account_id=account_id)
        if found is None:
            typer.echo("That conversation was not found.", err=True)
            raise typer.Exit(code=EXIT_ERROR)

        episode, messages = found
        typer.echo(
            f"Conversation {int(episode.id)} in chat {int(episode.chat_id)}: "
            f"{episode.started_at:%Y-%m-%d %H:%M} to {episode.ended_at:%Y-%m-%d %H:%M}"
        )
        typer.echo(f"{episode.message_count} message(s), lasting {episode.duration}.")
        typer.echo("")
        for message in messages:
            who = "you" if message.is_outgoing else "them"
            body = message.text if message.text is not None else f"({message.message_type.value})"
            typer.echo(f"{message.sent_at:%Y-%m-%d %H:%M}  {who:<5} {body}")

    _run_async(container, run())


@ai_app.command("run")
def ai_run(  # noqa: PLR0913, PLR0917 - Typer reads the options from the signature
    content: Annotated[str, typer.Argument(help="What to give the model.")],
    instructions: Annotated[
        str | None,
        typer.Option("--instructions", "-i", help="The system prompt, if the task has one."),
    ] = None,
    task: Annotated[
        str, typer.Option("--task", help="What this call is for. Recorded on it.")
    ] = "manual",
    prompt_id: Annotated[
        str, typer.Option("--prompt", help="Which prompt this is. Recorded on the call.")
    ] = "manual",
    prompt_version: Annotated[
        str, typer.Option("--prompt-version", help="Which revision of it.")
    ] = "1",
    chat: Annotated[
        int | None,
        typer.Option(
            "--chat",
            help=(
                "The chat this content belongs to. Naming it is what grants "
                "permission for a cloud model."
            ),
        ),
    ] = None,
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Run one AI task and record it.

    The whole boundary, end to end: the privacy gate, the timeout, the token
    accounting and the audit record. What the model is asked is exactly the
    content given -- there is no prompt template yet, and no parsing of what
    comes back. Both arrive with the first real task.

    A cloud model refuses content that names no chat: content with no chat has
    no permission attached, and in a local-first application the absence of a
    permission is not a permission.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        task_runner = await container.execute_ai_task()
        result = await task_runner.execute(
            content=content,
            instructions=instructions,
            prompt=PromptVersion(prompt_id=prompt_id, version=prompt_version),
            task_kind=task,
            chat_id=chat,
            account_id=account_id,
        )

        typer.echo(result.text or "")
        typer.echo("")
        _call_summary(result.call)

    _run_async(container, run())


@ai_app.command("show")
def ai_show(
    call: Annotated[int, typer.Argument(help="Call id, from `tgassist ai list`.")],
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show one recorded call in full.

    Metadata only. The prompt is never stored, and the response is stored only
    when `ai.store_responses` is on -- which the production profile refuses.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        found = await container.get_ai_call().execute(call, account_id=account_id)
        if found is None:
            typer.echo("That AI call was not found.", err=True)
            raise typer.Exit(code=EXIT_ERROR)

        typer.echo(f"Call {int(found.id)}  {found.created_at:%Y-%m-%d %H:%M:%S}")
        typer.echo(f"  model       {found.model}  ({found.model.data_boundary.value})")
        typer.echo(f"  prompt      {found.prompt}")
        typer.echo(f"  task        {found.task_kind}")
        typer.echo(f"  chat        {int(found.chat_id) if found.chat_id else '-'}")
        typer.echo(f"  outcome     {found.outcome.value}")
        if found.finish_reason is not None:
            typer.echo(f"  finished    {found.finish_reason.value}")
        typer.echo(f"  latency     {found.latency_seconds:.3f}s")
        typer.echo(
            f"  tokens      "
            f"{found.usage.input_tokens if found.usage.input_tokens is not None else '?'} in, "
            f"{found.usage.output_tokens if found.usage.output_tokens is not None else '?'} out"
        )
        typer.echo(f"  cost        {found.cost if found.cost is not None else 'unknown'}")
        typer.echo(f"  digest      {found.response_digest or '-'}")
        if found.response_text is not None:
            typer.echo("")
            typer.echo(found.response_text)

    _run_async(container, run())


@ai_app.command("list")
def ai_list(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Calls per page.")] = 20,
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """List recorded AI calls, newest first.

    Includes the failures. Success-only instrumentation hides exactly the
    expensive cases.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        page = await container.list_ai_calls().execute(
            PageRequest(limit=limit), account_id=account_id
        )

        if not page.items:
            typer.echo("No AI calls recorded.")
            return

        for record in page:
            typer.echo(
                f"{int(record.id):>8}  {record.created_at:%Y-%m-%d %H:%M}  "
                f"{record.outcome.value:<14} {record.latency_ms:>6}ms  "
                f"{record.task_kind:<16} {record.model}"
            )
        typer.echo("")
        typer.echo(f"{len(page.items)} call(s).")
        if page.has_more:
            typer.echo("More available; raise --limit to see them.")

    _run_async(container, run())


@memory_app.command("extract")
def memory_extract(
    conversation: Annotated[int, typer.Argument(help="Conversation id, from `conversation list`.")],
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Ask a model what is worth remembering from one conversation.

    Nothing is remembered. Everything this produces is a *proposal* waiting for
    you to accept or reject it, and this milestone has no way to accept one --
    the model proposes, you decide (ADR-019, ADR-058).

    Running it twice on the same conversation is free: what has already been
    proposed is not proposed again, including anything you have rejected.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        extractor = await container.extract_memories()
        report = await extractor.execute(conversation, account_id=account_id)

        if not report.returned:
            typer.echo("The model proposed nothing. Most conversations contain nothing worth")
            typer.echo("remembering, so this is a common and correct answer.")
        else:
            typer.echo(f"{report.returned} candidate(s) returned, {report.stored} stored.")

        for proposal in report.proposed:
            typer.echo(
                f"  {int(proposal.id):>8}  {proposal.confidence}  "
                f"{proposal.category.value:<18} {proposal.value}"
            )

        if report.discarded:
            typer.echo("")
            typer.echo(f"{report.discarded} discarded:")
            for label, count in (
                ("quoted text that is not in the conversation", report.ungrounded),
                ("confidence below the threshold", report.low_confidence),
                ("already proposed", report.duplicates),
                ("beyond this run's cap", report.over_cap),
            ):
                if count:
                    typer.echo(f"  {count:>3}  {label}")

        if report.repaired:
            typer.echo("")
            typer.echo("The first answer did not match the schema and was corrected once.")
        typer.echo("")
        typer.echo(f"Recorded as AI call {int(report.ai_call_id)}.")

    _run_async(container, run())


@memory_app.command("proposals")
def memory_proposals(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Proposals per page.")] = 20,
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """List candidate memories awaiting a decision, newest first."""
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        page = await container.list_memory_proposals().execute(
            PageRequest(limit=limit), account_id=account_id
        )

        if not page.items:
            typer.echo("No proposals. Run `tgassist memory extract <conversation>`.")
            return

        for proposal in page:
            typer.echo(
                f"{int(proposal.id):>8}  {proposal.created_at:%Y-%m-%d %H:%M}  "
                f"{proposal.status.value:<9} {proposal.confidence}  "
                f"{proposal.category.value:<18} {proposal.value}"
            )
        typer.echo("")
        typer.echo(f"{len(page.items)} proposal(s).")
        if page.has_more:
            typer.echo("More available; raise --limit to see them.")

    _run_async(container, run())


@memory_app.command("proposal")
def memory_proposal(
    proposal: Annotated[int, typer.Argument(help="Proposal id, from `memory proposals`.")],
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show one proposal, with the text it was read from.

    The evidence is the point of this command. A proposal you cannot check is a
    proposal you can only guess about, and the quotation is what makes accepting
    one a decision rather than a leap.

    Named ``proposal`` rather than ``show`` since Slice 9c: ``memory show`` now
    shows a *memory*, and one name for two different things is a name that
    will be typed wrongly.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        found = await container.get_memory_proposal().execute(proposal, account_id=account_id)
        if found is None:
            typer.echo("That proposal was not found.", err=True)
            raise typer.Exit(code=EXIT_ERROR)

        typer.echo(f"Proposal {int(found.id)}  {found.created_at:%Y-%m-%d %H:%M:%S}")
        typer.echo(f"  status       {found.status.value}")
        typer.echo(f"  category     {found.category.value}")
        typer.echo(f"  confidence   {found.confidence}")
        typer.echo(f"  conversation {int(found.conversation_id)}")
        typer.echo(f"  prompt       {found.prompt}")
        typer.echo(f"  ai call      {int(found.ai_call_id)}")
        typer.echo("")
        typer.echo(f"  {found.value}")
        typer.echo("")
        typer.echo("Read from:")
        typer.echo(f"  {found.evidence.quote}")

    _run_async(container, run())


@memory_app.command("accept")
def memory_accept(
    proposal: Annotated[int, typer.Argument(help="Proposal id, from `memory proposals`.")],
    importance: Annotated[
        str,
        typer.Option(
            "--importance",
            "-i",
            help=(
                "How much this matters: low, normal, high or critical. Ranked "
                "above the model's confidence when a context is assembled, "
                "because your judgement of what is worth knowing outranks a "
                "machine's estimate of what is true."
            ),
        ),
    ] = "normal",
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Keep a proposed fact, permanently.

    This is the only way anything enters long-term memory. The proposal becomes
    accepted, exactly one memory is created, and both happen in one transaction
    -- an acceptance you could not find afterwards would be worse than none.

    **There is no undo.** A decision is made once (ADR-059). If you change your
    mind, `memory forget` removes the memory, which also frees the fact to be
    proposed again.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    weight = IMPORTANCE_LEVELS.get(importance.strip().lower())
    if weight is None:
        typer.echo(
            f"Unknown importance {importance!r}. One of: {', '.join(IMPORTANCE_LEVELS)}.",
            err=True,
        )
        raise typer.Exit(code=EXIT_ERROR)

    async def run() -> None:
        await container.start()
        result = await container.accept_memory_proposal().execute(
            proposal, account_id=account_id, importance=Importance(weight)
        )

        typer.echo(f"Remembered as memory {int(result.memory.id)}.")
        typer.echo(f"  {result.memory.category.value}: {result.memory.value}")
        typer.echo(f"  key        {result.memory.key}")
        typer.echo(f"  importance {result.memory.importance.label}")
        if result.memory.contact_id is not None:
            typer.echo(f"  about contact {int(result.memory.contact_id)}")

    _run_async(container, run())


@memory_app.command("reject")
def memory_reject(
    proposal: Annotated[int, typer.Argument(help="Proposal id, from `memory proposals`.")],
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Decline a proposed fact, permanently.

    Nothing is created and nothing is deleted: the proposal is kept as rejected,
    so extraction does not offer the same fact again. A decision is made once,
    and there is no undo.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        rejected = await container.reject_memory_proposal().execute(proposal, account_id=account_id)

        typer.echo(f"Rejected proposal {int(rejected.id)}. Nothing was remembered.")
        typer.echo(f"  {rejected.category.value}: {rejected.value}")

    _run_async(container, run())


@memory_app.command("list")
def memory_list(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Memories per page.")] = 20,
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """List what this account remembers, newest first.

    Approved facts only. Everything here was accepted by a person; nothing a
    model produced reaches this list on its own.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        page = await container.list_memories().execute(
            PageRequest(limit=limit), account_id=account_id
        )

        if not page.items:
            typer.echo("Nothing remembered yet. Accept a proposal from `memory proposals`.")
            return

        for memory in page:
            about = int(memory.contact_id) if memory.contact_id is not None else "-"
            typer.echo(
                f"{int(memory.id):>8}  {memory.created_at:%Y-%m-%d %H:%M}  "
                f"{about!s:>8}  {memory.category.value:<18} {memory.value}"
            )
        typer.echo("")
        typer.echo(f"{len(page.items)} memory/memories.")
        if page.has_more:
            typer.echo("More available; raise --limit to see them.")

    _run_async(container, run())


@memory_app.command("show")
def memory_show_one(
    memory: Annotated[int, typer.Argument(help="Memory id, from `memory list`.")],
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show one memory and where it came from.

    The provenance is the point: through it a memory leads back to the proposal
    a person accepted, the conversation it was read from and the model call that
    produced it.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        found = await container.get_memory().execute(memory, account_id=account_id)
        if found is None:
            typer.echo("That memory was not found.", err=True)
            raise typer.Exit(code=EXIT_ERROR)

        state = "remembered" if found.is_active else "forgotten"
        typer.echo(f"Memory {int(found.id)}  {found.created_at:%Y-%m-%d %H:%M:%S}  ({state})")
        typer.echo(f"  category     {found.category.value}")
        typer.echo(f"  key          {found.key}")
        typer.echo(f"  importance   {found.importance.label}")
        typer.echo(f"  confidence   {found.confidence}")
        typer.echo(
            f"  retrieved    {found.retrieval_count} time(s)"
            + (
                f", last {found.last_retrieved_at:%Y-%m-%d %H:%M}"
                if found.last_retrieved_at is not None
                else ""
            )
        )
        typer.echo(f"  source       {found.source.value}")
        typer.echo(
            f"  about        "
            f"{int(found.contact_id) if found.contact_id is not None else 'no one in particular'}"
        )
        typer.echo(f"  proposal     {int(found.proposal_id) if found.proposal_id else '(deleted)'}")
        typer.echo(
            f"  conversation {int(found.conversation_id) if found.conversation_id else '(deleted)'}"
        )
        typer.echo(f"  ai call      {int(found.ai_call_id) if found.ai_call_id else '(deleted)'}")
        if found.deleted_at is not None:
            typer.echo(f"  forgotten    {found.deleted_at:%Y-%m-%d %H:%M:%S}")
        typer.echo("")
        typer.echo(f"  {found.value}")

    _run_async(container, run())


@memory_app.command("forget")
def memory_forget(
    memory: Annotated[int, typer.Argument(help="Memory id, from `memory list`.")],
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Forget one memory.

    Soft: the row is kept with a timestamp, so retention can ask what was
    deleted and when, and `memory show` can still answer for it. It stops being
    listed, stops being retrievable, and frees its key -- so the same fact can
    be accepted again if it is proposed. That is the only route to a correction,
    because nothing edits a memory (ADR-059).
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        forgotten = await container.delete_memory().execute(memory, account_id=account_id)

        if forgotten:
            typer.echo(f"Forgot memory {memory}.")
        else:
            typer.echo(f"Memory {memory} had already been forgotten.")

    _run_async(container, run())


@memory_app.command("context")
def memory_context(
    chat: Annotated[int, typer.Argument(help="Chat id, from `chat list`.")],
    record: Annotated[
        bool,
        typer.Option(
            "--record",
            help=(
                "Count this retrieval against the memories it selects, as a "
                "real one would. Off by default: looking is not using."
            ),
        ),
    ] = False,
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show what a model would be told about the person in a chat.

    The whole of retrieval, before anything sends it anywhere. Deterministic:
    the same memories and the same budget produce the same context every time,
    and every line explains why it placed where it did (ADR-060).

    Nothing here reaches a model. Retrieval is inspectable on its own so that
    the first time a memory reaches a prompt, the selection that put it there
    has already been read by a person.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        builder = container.build_memory_context() if record else container.get_memory_context()
        context = await builder.execute(chat, account_id=account_id)

        about = (
            f"contact {int(context.contact_id)}"
            if context.contact_id is not None
            else "nobody in particular (this chat has no single counterpart)"
        )
        typer.echo(f"Context for chat {int(context.chat_id)}, about {about}.")
        typer.echo(
            f"{context.selection.candidates} candidate(s), "
            f"{len(context.memories)} selected, "
            f"{context.tokens}/{context.selection.budget} tokens."
        )
        if context.truncated:
            typer.echo(
                "Candidates were capped, so ranking did not see everything known.",
                err=True,
            )
        typer.echo("")

        if context.is_empty:
            typer.echo("Nothing to tell a model about this chat yet.")
        for position, memory in enumerate(context.memories, start=1):
            typer.echo(f"{position:>3}. {memory.category.value}: {memory.value}")
            typer.echo(f"     {context.why(memory)}")

        if context.omitted:
            typer.echo("")
            typer.echo(f"{len(context.omitted)} omitted:")
            for item in context.omitted:
                typer.echo(f"{item.rank:>3}. {item.memory.category.value}: {item.memory.value}")
                typer.echo(f"     {item.reason.value}")

        if context.recorded:
            typer.echo("")
            typer.echo("Recorded as a retrieval against every memory selected.")

    _run_async(container, run())


@chat_app.command("suggest")
def chat_suggest(
    chat: Annotated[int, typer.Argument(help="Chat id, from `chat list`.")],
    show_prompt: Annotated[
        bool,
        typer.Option("--show-prompt", help="Print the exact text that was sent."),
    ] = False,
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Draft a reply for a chat, using what is known about the person.

    **Nothing is sent.** This produces a draft for you to read, edit and decide
    about; no code path leads from here to Telegram (ADR-061).

    What it prints is the whole chain: which memories were retrieved and which
    were left out, how much of the conversation was included, which prompt
    version asked, what it cost, and which memories the model says it used.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        generator = await container.generate_suggestion()
        suggestion = await generator.execute(chat, account_id=account_id)
        assembled = suggestion.prompt
        context = assembled.context

        typer.echo(f"Suggestion for chat {int(assembled.chat_id)}")
        typer.echo("")

        _suggestion_memories(suggestion)
        _suggestion_omissions(suggestion)
        typer.echo("")
        typer.echo(
            f"Conversation: {len(context.conversation.turns)} of "
            f"{context.conversation.available} recent message(s)"
            + (
                f", {context.conversation.truncated} truncated"
                if context.conversation.truncated
                else ""
            )
        )
        for turn in context.conversation.turns:
            typer.echo(f"    {turn.who}: {turn.text}")

        typer.echo("")
        typer.echo(f"Prompt:  {assembled.version}")
        typer.echo(f"Tokens:  {context.tokens}/{context.budget} estimated")
        typer.echo(f"AI call: {int(suggestion.ai_call_id)}")
        if suggestion.repaired:
            typer.echo("The first answer did not match the schema and was corrected once.")
        if not suggestion.is_grounded:
            typer.echo(
                "The model cited memories that were never supplied: "
                f"{', '.join(suggestion.fabricated_keys)}. Treat this suggestion "
                "with suspicion.",
                err=True,
            )

        typer.echo("")
        typer.echo(f"Suggested reply (confidence {suggestion.confidence}):")
        typer.echo("")
        typer.echo(suggestion.text)
        typer.echo("")
        if suggestion.suggestion_id is not None:
            typer.echo(
                f"Saved for review as suggestion {int(suggestion.suggestion_id)}: "
                f"`tgassist suggestion accept` or `dismiss`."
            )
        typer.echo("Nothing has been sent. This is a draft.")

        if show_prompt:
            typer.echo("")
            typer.echo("--- system prompt ---")
            typer.echo(assembled.instructions)
            typer.echo("--- task prompt ---")
            typer.echo(assembled.text)

    _run_async(container, run())


def _suggestion_memories(suggestion: GeneratedSuggestion) -> None:
    """Print the memories that reached the model, marking the reported ones."""
    memories = suggestion.prompt.context.memories
    typer.echo(f"Memories supplied ({len(memories)}):")
    if not memories:
        typer.echo("  (nothing is known about this person yet)")
    used = {int(memory.id) for memory in suggestion.used_memories}
    for memory in memories:
        mark = "*" if int(memory.id) in used else " "
        typer.echo(f"  {mark} [{memory.key}] {memory.category.value}: {memory.value}")
    if used:
        typer.echo("  (* the model reports using these)")


def _suggestion_omissions(suggestion: GeneratedSuggestion) -> None:
    """Print what retrieval left out and what the prompt budget then trimmed.

    Two lists rather than one: a memory retrieval never selected and one the
    assembler dropped were excluded by different budgets, and only a report that
    distinguishes them says which is too small (ADR-061).
    """
    omitted = list(suggestion.prompt.retrieval.omitted)
    if omitted:
        typer.echo("")
        typer.echo(f"Memories not retrieved ({len(omitted)}):")
        for item in omitted:
            typer.echo(f"    {item.memory.value}  --  {item.reason.value}")

    trimmed = suggestion.prompt.context.trimmed
    if trimmed:
        typer.echo("")
        typer.echo(f"Trimmed to fit the prompt budget ({len(trimmed)}):")
        for item_trimmed in trimmed:
            typer.echo(f"    {item_trimmed.what}  --  {item_trimmed.reason.value}")


@suggestion_app.command("list")
def suggestion_list(
    chat: Annotated[
        int | None,
        typer.Option(
            "--chat",
            help=(
                "Show one chat's suggestions, decided ones included. Without "
                "it, the queue: everything still awaiting a decision."
            ),
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Per page.")] = 20,
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """List suggestions awaiting a decision, newest first.

    With ``--chat``, lists that conversation's suggestions including the ones
    already decided -- reviewing a conversation means seeing what was dismissed
    as well as what was kept.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        page = await container.list_suggestions().execute(
            PageRequest(limit=limit), chat_id=chat, account_id=account_id
        )

        if not page.items:
            typer.echo("Nothing to review." if chat is None else "No suggestions for that chat.")
            return

        for suggestion in page:
            typer.echo(
                f"{int(suggestion.id):>8}  {suggestion.created_at:%Y-%m-%d %H:%M}  "
                f"{suggestion.status.value:<9} chat {int(suggestion.chat_id):<10} "
                f"{suggestion.title}"
            )
        typer.echo("")
        typer.echo(f"{len(page.items)} suggestion(s).")
        if page.has_more:
            typer.echo("More available; raise --limit to see them.")

    _run_async(container, run())


@suggestion_app.command("show")
def suggestion_show(
    suggestion: Annotated[int, typer.Argument(help="Suggestion id, from `suggestion list`.")],
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Show one suggestion and where it came from.

    The provenance is the point: through it a suggestion leads back to the
    conversation it is about and the model call that produced it, so a decision
    is made on evidence rather than on how well the draft reads.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        found = await container.get_suggestion().execute(suggestion, account_id=account_id)
        if found is None:
            typer.echo("That suggestion was not found.", err=True)
            raise typer.Exit(code=EXIT_ERROR)

        typer.echo(f"Suggestion {int(found.id)}  {found.created_at:%Y-%m-%d %H:%M:%S}")
        typer.echo(f"  status       {found.status.value}")
        typer.echo(f"  type         {found.proposal_type.value}")
        typer.echo(f"  chat         {int(found.chat_id)}")
        typer.echo(
            f"  conversation "
            f"{int(found.conversation_id) if found.conversation_id else '(not recorded)'}"
        )
        typer.echo(f"  ai call      {int(found.ai_call_id)}")
        if found.decided_at is not None:
            typer.echo(f"  decided      {found.decided_at:%Y-%m-%d %H:%M:%S}")
        typer.echo("")
        typer.echo(found.description)
        typer.echo("")
        details = found.details()
        for key in sorted(details):
            typer.echo(f"  {key:<24} {details[key]}")
        typer.echo("")
        typer.echo("Nothing has been sent. Deciding about this sends nothing either.")

    _run_async(container, run())


@suggestion_app.command("accept")
def suggestion_accept(
    suggestion: Annotated[int, typer.Argument(help="Suggestion id, from `suggestion list`.")],
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Record that you agree with a suggestion.

    **This sends nothing and does nothing else.** Accepting marks the suggestion
    as agreed with; acting on it is yours to do. Nothing in this application
    executes a suggestion, and nothing will until you switch on something that
    does (ADR-062).

    A decision is made once. There is no undo.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        decided = await container.accept_suggestion().execute(suggestion, account_id=account_id)

        typer.echo(f"Accepted suggestion {int(decided.id)}.")
        typer.echo("")
        typer.echo(decided.description)
        typer.echo("")
        typer.echo("Nothing was sent. Acting on this is yours to do.")

    _run_async(container, run())


@suggestion_app.command("dismiss")
def suggestion_dismiss(
    suggestion: Annotated[int, typer.Argument(help="Suggestion id, from `suggestion list`.")],
    account: AccountOption = None,
    profile: ProfileOption = None,
    config_dir: ConfigDirOption = None,
) -> None:
    """Record that you do not want a suggestion.

    Nothing is created and nothing is deleted: the suggestion is kept as
    dismissed, because a record of only what was agreed with cannot show what
    the generator is getting wrong.

    A decision is made once. There is no undo.
    """
    container = _open(profile, config_dir)
    account_id = AccountId(account) if account is not None else None

    async def run() -> None:
        await container.start()
        decided = await container.dismiss_suggestion().execute(suggestion, account_id=account_id)

        typer.echo(f"Dismissed suggestion {int(decided.id)}. Nothing was done with it.")

    _run_async(container, run())


def _call_summary(record: AiCall) -> None:
    """Print the one-line account of a call that just ran."""
    tokens = record.usage.total
    typer.echo(
        f"call {int(record.id)}: {record.outcome.value}, "
        f"{record.latency_ms}ms, "
        f"{tokens if tokens is not None else 'unmeasured'} token(s), "
        f"{record.cost if record.cost is not None else 'cost unknown'}"
    )


def _backfill_report(reports: tuple[BackfillReport, ...]) -> None:
    """Print what a backfill did, and what is left.

    Says where each chat stopped rather than only what it stored. "0 new" after
    a completed backfill and "0 new" after a batch limit are the same number
    with opposite meanings, and only the reason distinguishes them.
    """
    if not reports:
        typer.echo("No chats have synchronisation switched on.")
        return

    for report in reports:
        typer.echo(
            f"chat {int(report.chat_id):>6}  "
            f"{report.stored} new, {report.skipped} already stored  "
            f"({report.batches} batch(es), {_STOP_REASONS[report.stop_reason]})"
        )

    stored = sum(report.stored for report in reports)
    unfinished = [report for report in reports if not report.is_complete]
    typer.echo("")
    typer.echo(f"{stored} message(s) stored across {len(reports)} chat(s).")
    if unfinished:
        typer.echo(f"{len(unfinished)} chat(s) have more history. Run this again to continue.")


#: What each stop reason means, in the words a person reading a terminal wants.
_STOP_REASONS: dict[str, str] = {
    BackfillStop.BEGINNING: "reached the beginning",
    BackfillStop.HORIZON: "reached the history horizon",
    BackfillStop.ALREADY_COMPLETE: "already complete",
    BackfillStop.BATCH_LIMIT: "stopped at the batch limit",
    BackfillStop.NO_PROGRESS: "stopped: Telegram returned no further history",
}


def _report(report: SyncReport, noun: str) -> None:
    """Print what a synchronisation run did.

    Problems are printed even when the run otherwise succeeded. A run that
    quietly skipped somebody would look identical to one that had nothing to
    skip, and the difference is a person missing from the operator's list.
    """
    typer.echo(
        f"{report.considered} {noun}(s): {report.created} new, "
        f"{report.updated} updated, {report.unchanged} unchanged, "
        f"{report.skipped} skipped."
    )
    if report.is_clean:
        return

    typer.echo("")
    typer.echo(f"{len(report.problems)} problem(s):", err=True)
    for problem in report.problems:
        typer.echo(f"  {problem}", err=True)


def _open(profile: str | None, config_dir: Path | None) -> Container:
    """Build a container, reporting configuration failures as a clean CLI error.

    Logging is configured here, like everywhere else. An earlier version skipped
    it to keep command output clean, which had the opposite effect: unconfigured,
    structlog falls back to a ``PrintLogger`` on standard output with no level
    filtering and no redaction, so every record landed in the middle of the
    command's own output (ADR-040). Configured, records go to the sinks the
    user asked for -- the console handler writes to standard error -- and
    standard output carries only what the command printed.
    """
    try:
        return Container.create(profile=profile, config_dir=config_dir)
    except ConfigurationError as exc:
        typer.echo(f"Configuration error: {exc.user_message}", err=True)
        typer.echo(f"  {exc.code}: {exc.message}", err=True)
        raise typer.Exit(code=EXIT_CONFIG_ERROR) from exc
    except AppError as exc:  # pragma: no cover - defensive
        typer.echo(f"Error: {exc.user_message}", err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc


def _render(values: dict[str, Any], origins: dict[str, str], prefix: str = "") -> list[str]:
    """Render configuration values as indented lines, masking sensitive ones."""
    lines: list[str] = []
    for key, value in values.items():
        dotted = f"{prefix}{key}"
        indent = "  " * (dotted.count(".") + 1)
        if isinstance(value, dict) and value:
            lines.append(f"{indent}{key}:")
            lines.extend(_render(value, origins, prefix=f"{dotted}."))
            continue
        shown = MASKED if is_sensitive_key(key) else value
        origin = origins.get(dotted)
        suffix = f"    [{origin}]" if origin else ""
        lines.append(f"{indent}{key}: {shown}{suffix}")
    return lines


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
