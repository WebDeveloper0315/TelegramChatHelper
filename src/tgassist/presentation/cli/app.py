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
from pathlib import Path
from typing import Annotated, Any

import typer

from tgassist import __version__
from tgassist.application.container import Container
from tgassist.domain.errors import AppError, ConfigurationError
from tgassist.domain.services.sensitivity import is_sensitive_key

MASKED = "********"

EXIT_ERROR = 1
EXIT_CONFIG_ERROR = 2

MIN_PYTHON = (3, 12)

# Subsystems that do not exist yet. Reported explicitly so that `doctor` output
# is an honest picture of what has and has not been checked, rather than a
# green result that quietly omits everything unimplemented.
PENDING_SUBSYSTEMS = (
    "Database",
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
    return checks


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
