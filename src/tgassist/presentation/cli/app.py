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
from pathlib import Path
from typing import Annotated, Any

import typer

from tgassist import __version__
from tgassist.application.container import Container
from tgassist.application.use_cases.account import CreateAccountRequest
from tgassist.application.use_cases.contact import ContactTransition
from tgassist.application.use_cases.user_profile import ProfileChanges
from tgassist.domain.errors import AppError, ConfigurationError, DomainValidationError
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.user_profile import (
    EmojiUsage,
    MessageLength,
    TimeRange,
    TonePreference,
)
from tgassist.domain.services.sensitivity import is_sensitive_key

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
        await container.start_database()
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
        await container.start_database()
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
        await container.start_database()
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
        await container.start_database()
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
        await container.start_database()
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
        await container.start_database()
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
        await container.start_database()
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
        await container.start_database()
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
        await container.start_database()
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
        await container.start_database()
        contact = await container.change_contact_status().execute(
            contact_id,
            transition,
            account_id=AccountId(account_id) if account_id is not None else None,
        )
        typer.echo(f"Contact {contact.id} ({contact.display_name}) is now {contact.status}.")

    _run_async(container, run())


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
