"""Shared test fixtures.

Tests never touch the developer's real data directory and never mutate
process-wide logging state without restoring it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tgassist.application.container import Container
from tgassist.infrastructure.config import AppConfig, LoadedConfig, Profile, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove application environment variables so tests start from a clean slate."""
    for name in list(__import__("os").environ):
        if name.startswith("TGASSIST_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo_config_dir() -> Path:
    """Return the repository's committed configuration directory."""
    return REPO_ROOT / "config"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Return an isolated data directory for one test."""
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def config(data_dir: Path) -> AppConfig:
    """Return a valid configuration pointed at an isolated data directory."""
    return AppConfig.model_validate(
        {
            "profile": Profile.TESTING,
            "app": {"data_dir": data_dir},
            "logging": {"console_enabled": False, "file_enabled": False},
        }
    )


@pytest.fixture
def container(config: AppConfig) -> Iterator[Container]:
    """Return a container that does not reconfigure global logging."""
    with Container(LoadedConfig(config=config)) as instance:
        yield instance


@pytest.fixture
def write_config(tmp_path: Path) -> Any:
    """Return a helper that writes configuration files into a temporary directory."""

    def _write(files: dict[str, str]) -> Path:
        config_dir = tmp_path / "config"
        for relative, content in files.items():
            path = config_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir

    return _write


@pytest.fixture
def load_from(write_config: Any) -> Any:
    """Return a helper that writes configuration files and loads them."""

    def _load(files: dict[str, str], **kwargs: Any) -> Any:
        return load_config(config_dir=write_config(files), **kwargs)

    return _load


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Restore the root logger after a test that reconfigures logging."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)
