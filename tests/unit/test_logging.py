"""Logging and redaction tests.

Redaction is a security control (`docs/SECURITY.md` section 9), so these are
assertions about what must never appear in a log file, not merely about
formatting.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest
import structlog

from tgassist.domain.services.sensitivity import (
    REDACTED,
    REDACTED_CONTENT,
    is_content_key,
    is_secret_key,
)
from tgassist.infrastructure.config import AppConfig, LogLevel, Profile
from tgassist.infrastructure.logging import (
    LOG_FILE_NAME,
    build_redaction_processor,
    configure_logging,
    get_logger,
    purge_expired_logs,
)

# A deliberately realistic fake, so the value-shape detector is exercised.
SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz012345"  # noqa: S105


def redact(payload: dict[str, object], *, allow_content: bool = False) -> dict[str, object]:
    processor = build_redaction_processor(allow_content=allow_content)
    return dict(processor(None, "info", dict(payload)))


class TestKeyClassification:
    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "ANTHROPIC_API_KEY",
            "api_hash",
            "password",
            "passphrase",
            "auth_code",
            "session_key",
            "encryption_key",
            "phone_number",
            "access_token",
            "client_secret",
            "authorization",
        ],
    )
    def test_secret_keys_are_recognised(self, key: str) -> None:
        assert is_secret_key(key)

    @pytest.mark.parametrize("key", ["message_text", "prompt", "summary_text", "caption"])
    def test_content_keys_are_recognised(self, key: str) -> None:
        assert is_content_key(key)

    @pytest.mark.parametrize("key", ["event", "level", "logger", "timestamp"])
    def test_structural_keys_are_never_redacted(self, key: str) -> None:
        assert not is_secret_key(key)
        assert not is_content_key(key)

    @pytest.mark.parametrize("key", ["chat_id", "message_id", "duration_ms", "count"])
    def test_ordinary_keys_pass_through(self, key: str) -> None:
        assert not is_secret_key(key)
        assert not is_content_key(key)

    @pytest.mark.parametrize(
        "key",
        ["api_key_ref", "api_hash_ref", "require_secret_store", "secret_backend", "token_name"],
    )
    def test_keys_that_name_a_secret_are_not_masked(self, key: str) -> None:
        # Regression: configuration holds secret NAMES, not values (ADR-021).
        # Masking these hides information the user needs and protects nothing.
        assert not is_secret_key(key)
        assert not is_content_key(key)


class TestRedaction:
    def test_secret_values_are_removed(self) -> None:
        result = redact({"event": "provider_call", "api_key": SECRET})

        assert result["api_key"] == REDACTED
        assert SECRET not in json.dumps(result)

    def test_event_name_survives(self) -> None:
        result = redact({"event": "provider_call", "api_key": SECRET})

        assert result["event"] == "provider_call"

    def test_content_is_removed_by_default(self) -> None:
        result = redact({"event": "ingest", "message_text": "hello there"})

        assert result["message_text"] == REDACTED_CONTENT

    def test_content_passes_in_diagnostic_mode(self) -> None:
        result = redact({"event": "ingest", "message_text": "hello there"}, allow_content=True)

        assert result["message_text"] == "hello there"

    def test_secrets_are_removed_even_in_diagnostic_mode(self) -> None:
        result = redact({"event": "call", "api_key": SECRET}, allow_content=True)

        assert result["api_key"] == REDACTED

    def test_nested_secrets_are_removed(self) -> None:
        result = redact({"event": "cfg", "provider": {"name": "x", "api_key": SECRET}})

        assert SECRET not in json.dumps(result)
        assert result["provider"] == {"name": "x", "api_key": REDACTED}

    def test_secrets_inside_lists_are_removed(self) -> None:
        result = redact({"event": "cfg", "providers": [{"api_key": SECRET}]})

        assert SECRET not in json.dumps(result)

    def test_secret_shaped_values_are_removed_under_innocuous_keys(self) -> None:
        # Defence in depth: key-name matching is primary, value shape is backup.
        result = redact({"event": "oops", "note": SECRET})

        assert result["note"] == REDACTED

    def test_secret_embedded_in_a_larger_string_is_removed(self) -> None:
        # Regression: an anchored pattern matched only a bare secret, so a
        # formatted message from a third-party library carried the key through.
        result = redact({"event": f"request failed token={SECRET} status=401"})

        assert SECRET not in str(result["event"])
        assert REDACTED in str(result["event"])
        assert "status=401" in str(result["event"])

    def test_ordinary_values_are_untouched(self) -> None:
        result = redact({"event": "sync", "chat_id": 42, "count": 100, "note": "fine"})

        assert result["chat_id"] == 42
        assert result["note"] == "fine"

    def test_deep_nesting_terminates(self) -> None:
        payload: dict[str, object] = {"event": "deep"}
        node = payload
        for _ in range(20):
            child: dict[str, object] = {}
            node["child"] = child
            node = child

        redact(payload)  # must not raise or hang


@pytest.mark.usefixtures("restore_logging")
class TestConfiguration:
    def _config(self, data_dir: Path, **logging_overrides: object) -> AppConfig:
        options: dict[str, object] = {
            "console_enabled": False,
            "file_enabled": True,
            "format": "json",
        }
        options.update(logging_overrides)
        return AppConfig.model_validate(
            {"profile": Profile.TESTING, "app": {"data_dir": data_dir}, "logging": options}
        )

    def test_file_handler_writes_json_lines(self, data_dir: Path) -> None:
        configure_logging(self._config(data_dir, level=LogLevel.INFO))

        get_logger("test").info("something_happened", chat_id=7)
        logging.shutdown()

        record = json.loads(self._read_log(data_dir).splitlines()[0])
        assert record["event"] == "something_happened"
        assert record["chat_id"] == 7
        assert record["level"] == "info"
        assert "timestamp" in record

    def test_secrets_never_reach_the_log_file(self, data_dir: Path) -> None:
        configure_logging(self._config(data_dir, level=LogLevel.INFO))

        get_logger("test").info("provider_configured", api_key=SECRET, message_text="private")
        logging.shutdown()

        contents = self._read_log(data_dir)
        assert SECRET not in contents
        assert "private" not in contents
        assert REDACTED in contents

    def test_standard_library_logs_are_also_redacted(self, data_dir: Path) -> None:
        # Third-party packages log through stdlib. If redaction only covered
        # structlog call sites, the larger surface would be unguarded.
        configure_logging(self._config(data_dir, level=LogLevel.INFO))

        logging.getLogger("third_party").warning("failure token=%s", SECRET)
        logging.shutdown()

        assert SECRET not in self._read_log(data_dir)

    def test_exceptions_are_recorded_with_a_traceback(self, data_dir: Path) -> None:
        configure_logging(self._config(data_dir, level=LogLevel.INFO))

        try:
            raise ValueError("boom")
        except ValueError:
            get_logger("test").exception("operation_failed")
        logging.shutdown()

        contents = self._read_log(data_dir)
        assert "operation_failed" in contents
        assert "ValueError" in contents
        assert "boom" in contents

    def test_level_filtering_applies(self, data_dir: Path) -> None:
        configure_logging(self._config(data_dir, level=LogLevel.WARNING))

        get_logger("test").debug("invisible")
        get_logger("test").warning("visible")
        logging.shutdown()

        contents = self._read_log(data_dir)
        assert "invisible" not in contents
        assert "visible" in contents

    def test_component_levels_override_the_global_level(self, data_dir: Path) -> None:
        configure_logging(
            self._config(
                data_dir,
                level=LogLevel.WARNING,
                component_levels={"chatty": LogLevel.DEBUG},
            )
        )

        assert logging.getLogger("chatty").level == logging.DEBUG

    def test_reconfiguration_does_not_duplicate_output(self, data_dir: Path) -> None:
        configure_logging(self._config(data_dir, level=LogLevel.INFO))
        configure_logging(self._config(data_dir, level=LogLevel.INFO))

        get_logger("test").info("once")
        logging.shutdown()

        lines = [line for line in self._read_log(data_dir).splitlines() if "once" in line]
        assert len(lines) == 1

    def test_disabling_file_output_writes_nothing(self, data_dir: Path) -> None:
        configure_logging(
            AppConfig.model_validate(
                {
                    "profile": Profile.TESTING,
                    "app": {"data_dir": data_dir},
                    "logging": {"console_enabled": False, "file_enabled": False},
                }
            )
        )

        get_logger("test").info("nowhere")
        logging.shutdown()

        assert not (data_dir / "logs" / LOG_FILE_NAME).exists()

    @staticmethod
    def _read_log(data_dir: Path) -> str:
        return (data_dir / "logs" / LOG_FILE_NAME).read_text(encoding="utf-8")


class TestRetention:
    def test_expired_files_are_removed(self, tmp_path: Path) -> None:
        old = tmp_path / f"{LOG_FILE_NAME}.1"
        old.write_text("old", encoding="utf-8")
        stale = time.time() - (30 * 86400)
        os.utime(old, (stale, stale))

        recent = tmp_path / LOG_FILE_NAME
        recent.write_text("recent", encoding="utf-8")

        removed = purge_expired_logs(tmp_path, retention_days=14)

        assert removed == 1
        assert not old.exists()
        assert recent.exists()

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert purge_expired_logs(tmp_path / "absent", retention_days=14) == 0


class TestLoggerFactory:
    def test_get_logger_returns_a_bound_logger(self) -> None:
        logger = get_logger("x")

        assert isinstance(logger.bind(a=1), structlog.stdlib.BoundLogger)
