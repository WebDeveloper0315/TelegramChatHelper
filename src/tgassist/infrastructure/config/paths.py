"""Filesystem locations used by the application.

Resolution is centralised here so that path policy, directory creation and
permission enforcement have exactly one implementation site.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

_APP_NAME = "tgassist"

_dirs = PlatformDirs(appname=_APP_NAME, appauthor=False, roaming=False)


def default_data_dir() -> Path:
    """Return the platform-appropriate application data directory."""
    return Path(_dirs.user_data_dir)


def default_config_dir() -> Path:
    """Return the repository configuration directory.

    Configuration ships with the source tree rather than the user data
    directory, because ``config/default.yaml`` is version-controlled
    documentation of every key (``docs/CONFIGURATION.md`` section 5).
    """
    return project_root() / "config"


def project_root() -> Path:
    """Return the repository root, resolved from this module's location."""
    # src/tgassist/infrastructure/config/paths.py -> repository root
    return Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved directory layout for one installation.

    Attributes:
        data_dir: Root for all generated data.
        logs_dir: Rotating log files.
        sessions_dir: Encrypted Telegram session store (Milestone 2).
        backups_dir: Database backups (Milestone 11).
        archives_dir: Archived message databases (Milestone 13).
        attachments_dir: Downloaded media (Milestone 2).
        exports_dir: User data exports (Milestone 11).
        models_dir: Downloaded embedding models (Milestone 5).
        cache_dir: Disposable derived data.
    """

    data_dir: Path
    logs_dir: Path
    sessions_dir: Path
    backups_dir: Path
    archives_dir: Path
    attachments_dir: Path
    exports_dir: Path
    models_dir: Path
    cache_dir: Path

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> AppPaths:
        """Derive the full layout from a single data directory."""
        root = data_dir.expanduser()
        return cls(
            data_dir=root,
            logs_dir=root / "logs",
            sessions_dir=root / "sessions",
            backups_dir=root / "backups",
            archives_dir=root / "archives",
            attachments_dir=root / "attachments",
            exports_dir=root / "exports",
            models_dir=root / "models",
            cache_dir=root / "cache",
        )

    def all_directories(self) -> tuple[Path, ...]:
        """Return every directory in the layout."""
        return (
            self.data_dir,
            self.logs_dir,
            self.sessions_dir,
            self.backups_dir,
            self.archives_dir,
            self.attachments_dir,
            self.exports_dir,
            self.models_dir,
            self.cache_dir,
        )

    def ensure(self, *, restrict_permissions: bool = True) -> None:
        """Create every directory, optionally restricting access to the owner.

        Args:
            restrict_permissions: Apply owner-only permissions on POSIX systems.
                Windows inherits access control entries from the parent
                directory; verifying and tightening those requires the Windows
                security API and is handled when the secret store lands.
        """
        for directory in self.all_directories():
            directory.mkdir(parents=True, exist_ok=True)
            if restrict_permissions:
                _restrict_to_owner(directory)


def _restrict_to_owner(path: Path) -> None:
    """Set owner-only permissions on POSIX systems; no-op elsewhere."""
    if os.name == "nt":
        return
    path.chmod(stat.S_IRWXU)


def is_owner_only(path: Path) -> bool | None:
    """Report whether a path is accessible only by its owner.

    Returns:
        ``True`` or ``False`` on POSIX systems, and ``None`` on Windows, where
        answering requires access control list inspection that is not yet
        implemented. ``None`` means "not verified", never "safe".
    """
    if os.name == "nt":
        return None
    mode = path.stat().st_mode
    return not bool(mode & (stat.S_IRWXG | stat.S_IRWXO))
