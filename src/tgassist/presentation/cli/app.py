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
from tgassist.domain.errors import AppError, ConfigurationError
from tgassist.domain.model.query import PageRequest
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

ProfileOption = Annotated[
    str | None,
    typer.Option("--profile", "-p", help="Environment profile to load."),
]
ConfigDirOption = Annotated[
    Path | None,
    typer.Option("--config-dir", help="Directory holding configuration files."),
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


def _open(profile: str | None, config_dir: Path | None) -> Container:
    """Build a container, reporting configuration failures as a clean CLI error.

    Logging is deliberately not configured here: these commands report on the
    application rather than run it, and reconfiguring global logging would
    interleave log records with their output.
    """
    try:
        return Container.create(
            profile=profile,
            config_dir=config_dir,
            configure_logging_on_start=False,
        )
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
