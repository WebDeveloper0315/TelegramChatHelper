"""Mapping, error translation and the gateway's failure paths.

The contract suite proves the happy flow against both implementations. This
covers what only the TDLib adapter has: translating a wire format, refusing what
it cannot translate, and surviving a client that dies mid-login.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.tdjson import AuthorizingTdjson, FakeTdjson
from tests.fakes.telegram_gateway import ScriptedHandler
from tgassist.domain.errors import (
    AuthorizationError,
    DomainValidationError,
    SessionRevokedError,
    TdlibNotRunningError,
    TdlibRequestFailedError,
    TelegramError,
    TelegramNotConfiguredError,
)
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.secret import MASK, SecretValue
from tgassist.domain.model.session import AuthorizationState, ConnectionState
from tgassist.domain.model.telegram import (
    CodeHint,
    PasswordHint,
    TelegramUser,
    require_credential,
)
from tgassist.infrastructure.telegram import errors as error_mapping
from tgassist.infrastructure.telegram import mapping
from tgassist.infrastructure.telegram.client import TdjsonClient
from tgassist.infrastructure.telegram.gateway import GatewaySettings, TdlibGateway

ACCOUNT = AccountId(1)
TIMEOUT = 3.0


def settings(tmp_path: Path) -> GatewaySettings:
    """Parameters for a gateway that never reaches a network."""
    return GatewaySettings(
        api_id=12345,
        api_hash=SecretValue("0123456789abcdef0123456789abcdef"),
        session_path=tmp_path / "session",
        database_encryption_key=SecretValue("test-session-key"),
    )


def build(library: Any, tmp_path: Path) -> TdlibGateway:
    """Build a gateway over a scripted library."""
    return TdlibGateway(
        ACCOUNT,
        TdjsonClient(library),
        settings(tmp_path),
        state_timeout=TIMEOUT,
        startup_timeout=TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


class TestTelegramUser:
    def test_requires_a_positive_identifier(self) -> None:
        with pytest.raises(DomainValidationError):
            TelegramUser(id=TelegramUserId(0), first_name="Nobody")

    def test_display_name_joins_both_names(self) -> None:
        user = TelegramUser(id=TelegramUserId(7), first_name="Ada", last_name="Lovelace")

        assert user.display_name == "Ada Lovelace"

    def test_display_name_falls_back_to_the_handle(self) -> None:
        user = TelegramUser(id=TelegramUserId(7), first_name="", username="ada")

        assert user.display_name == "@ada"

    def test_display_name_falls_back_to_the_identifier(self) -> None:
        # An account with no name at all is a real state, not an error.
        assert TelegramUser(id=TelegramUserId(7), first_name="").display_name == "7"

    def test_there_is_no_field_for_a_phone_number(self) -> None:
        # A field that exists is a field that eventually reaches a log.
        fields = set(TelegramUser.__dataclass_fields__)

        assert fields & {"phone", "phone_number"} == set()


class TestRequireCredential:
    def test_strips_surrounding_whitespace(self) -> None:
        # Codes are routinely pasted with a trailing space.
        assert require_credential(" 12345 ", name="Login code") == "12345"

    @pytest.mark.parametrize("value", ["", "   ", "\n"])
    def test_refuses_an_empty_value(self, value: str) -> None:
        # Sending it would spend one of a small number of attempts to be told
        # what was already knowable here.
        with pytest.raises(DomainValidationError, match="is empty"):
            require_credential(value, name="Login code")

    def test_the_message_names_the_field(self) -> None:
        with pytest.raises(DomainValidationError) as excinfo:
            require_credential("", name="Password")

        assert "password" in excinfo.value.user_message.lower()


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


class TestAuthorizationStateMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("authorizationStateWaitTdlibParameters", AuthorizationState.UNAUTHORIZED),
            ("authorizationStateWaitPhoneNumber", AuthorizationState.WAITING_PHONE),
            ("authorizationStateWaitCode", AuthorizationState.WAITING_CODE),
            ("authorizationStateWaitPassword", AuthorizationState.WAITING_PASSWORD),
            ("authorizationStateReady", AuthorizationState.READY),
            ("authorizationStateLoggingOut", AuthorizationState.LOGGED_OUT),
        ],
    )
    def test_known_states(self, raw: str, expected: AuthorizationState) -> None:
        assert mapping.authorization_state_from(raw) is expected

    def test_an_unknown_state_maps_to_nothing(self) -> None:
        assert mapping.authorization_state_from("authorizationStateWatFuture") is None

    @pytest.mark.parametrize("raw", sorted(mapping.CLOSING_AUTHORIZATION_STATES))
    def test_closing_is_not_a_logout(self, raw: str) -> None:
        # Recording it as one would tell the user they had been signed out
        # every time they quit the application.
        assert mapping.authorization_state_from(raw) is not AuthorizationState.LOGGED_OUT
        assert mapping.authorization_state_from(raw) is None

    @pytest.mark.parametrize("raw", sorted(mapping.UNSUPPORTED_AUTHORIZATION_STATES))
    def test_unsupported_states_carry_an_explanation(self, raw: str) -> None:
        # The difference between "TDLib changed" and "you need a flow we do not
        # support" is the difference between a bug report and an answer.
        explanation = mapping.UNSUPPORTED_AUTHORIZATION_STATES[raw]

        assert explanation.endswith(".")
        assert mapping.authorization_state_from(raw) is None

    def test_reads_the_type_out_of_an_update(self) -> None:
        frame = {
            "@type": "updateAuthorizationState",
            "authorization_state": {"@type": "authorizationStateReady"},
        }

        assert mapping.authorization_state_type(frame) == "authorizationStateReady"

    def test_a_malformed_update_yields_an_empty_type(self) -> None:
        assert mapping.authorization_state_type({"@type": "updateAuthorizationState"}) == ""


class TestConnectionStateMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("connectionStateWaitingForNetwork", ConnectionState.WAITING_FOR_NETWORK),
            ("connectionStateConnecting", ConnectionState.CONNECTING),
            ("connectionStateConnectingToProxy", ConnectionState.CONNECTING),
            ("connectionStateUpdating", ConnectionState.UPDATING),
            ("connectionStateReady", ConnectionState.READY),
        ],
    )
    def test_known_states(self, raw: str, expected: ConnectionState) -> None:
        assert mapping.connection_state_from(raw) is expected

    def test_an_unknown_state_maps_to_nothing(self) -> None:
        assert mapping.connection_state_from("connectionStateSomethingNew") is None


class TestHintMapping:
    def test_reads_delivery_length_and_timeout(self) -> None:
        hint = mapping.code_hint_from(
            {
                "@type": "authorizationStateWaitCode",
                "code_info": {
                    "type": {"@type": "authenticationCodeTypeSms", "length": 5},
                    "timeout": 60,
                },
            }
        )

        assert hint == CodeHint(delivery="sms", length=5, timeout_seconds=60)

    def test_splits_a_multi_word_delivery(self) -> None:
        hint = mapping.code_hint_from(
            {"code_info": {"type": {"@type": "authenticationCodeTypeTelegramMessage"}}}
        )

        assert hint.delivery == "telegram message"

    def test_an_empty_frame_still_produces_a_hint(self) -> None:
        # A login must not fail because a hint was missing: the hint improves a
        # prompt, it does not gate one.
        assert mapping.code_hint_from({}) == CodeHint(delivery="other")

    def test_a_zero_length_is_treated_as_absent(self) -> None:
        hint = mapping.code_hint_from({"code_info": {"type": {"length": 0}, "timeout": 0}})

        assert hint.length is None
        assert hint.timeout_seconds is None

    def test_reads_the_password_hint(self) -> None:
        hint = mapping.password_hint_from(
            {
                "password_hint": "the usual",
                "has_recovery_email_address": True,
                "recovery_email_address_pattern": "a**@e*****.com",
            }
        )

        assert hint == PasswordHint(
            hint="the usual",
            has_recovery_email=True,
            recovery_email_pattern="a**@e*****.com",
        )

    def test_an_empty_password_frame_yields_nothing_known(self) -> None:
        assert mapping.password_hint_from({}) == PasswordHint()


class TestUserMapping:
    def test_reads_the_nested_username(self) -> None:
        # Since 1.8.x the handles live in a nested object and the flat field is
        # absent, so both shapes are read.
        user = mapping.telegram_user_from(
            {
                "id": 42,
                "first_name": "Ada",
                "last_name": "Lovelace",
                "usernames": {"editable_username": "ada"},
            }
        )

        assert user.username == "ada"

    def test_reads_the_flat_username(self) -> None:
        user = mapping.telegram_user_from({"id": 42, "first_name": "Ada", "username": "ada"})

        assert user.username == "ada"

    def test_falls_back_to_the_first_active_username(self) -> None:
        user = mapping.telegram_user_from(
            {"id": 42, "first_name": "Ada", "usernames": {"active_usernames": ["ada", "al"]}}
        )

        assert user.username == "ada"

    def test_an_absent_username_is_none_not_empty(self) -> None:
        assert mapping.telegram_user_from({"id": 42, "first_name": "Ada"}).username is None

    def test_a_missing_identifier_is_refused_by_the_entity(self) -> None:
        with pytest.raises(DomainValidationError, match="Telegram user id"):
            mapping.telegram_user_from({"first_name": "Ada"})


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


class TestErrorTranslation:
    @pytest.mark.parametrize("message", sorted(error_mapping.AUTHORIZATION_FAILURES))
    def test_credential_failures_are_retryable(self, message: str) -> None:
        error = error_mapping.translate({"code": 400, "message": message}, operation="check_code")

        assert isinstance(error, AuthorizationError)

    @pytest.mark.parametrize("message", sorted(error_mapping.REVOCATIONS))
    def test_revocations_are_not_retryable(self, message: str) -> None:
        # Nothing the user types will help; the remedy is a fresh login.
        error = error_mapping.translate({"code": 401, "message": message}, operation="get_me")

        assert isinstance(error, SessionRevokedError)
        assert not isinstance(error, AuthorizationError)

    @pytest.mark.parametrize("message", sorted(error_mapping.CONFIGURATION_FAILURES))
    def test_configuration_failures_point_at_the_configuration(self, message: str) -> None:
        error = error_mapping.translate(
            {"code": 400, "message": message}, operation="set_parameters"
        )

        assert "api_id" in error.user_message

    def test_a_suffixed_message_still_matches(self) -> None:
        error = error_mapping.translate(
            {"code": 400, "message": "PHONE_CODE_INVALID_X"}, operation="check_code"
        )

        assert isinstance(error, AuthorizationError)

    def test_an_unknown_message_is_still_reported_with_its_detail(self) -> None:
        # A caller that cannot see TDLib's own code and text cannot act on them.
        error = error_mapping.translate(
            {"code": 500, "message": "SOMETHING_NEW"}, operation="get_me"
        )

        assert type(error) is TelegramError
        assert error.context["telegram_code"] == 500
        assert error.context["telegram_message"] == "SOMETHING_NEW"

    def test_a_missing_message_does_not_crash(self) -> None:
        assert error_mapping.error_message({}) == "unspecified error"
        assert error_mapping.error_code({}) == 0

    def test_no_translated_error_carries_a_credential(self) -> None:
        # The operation is a safe label; nothing submitted travels with it.
        error = error_mapping.translate(
            {"code": 400, "message": "PHONE_CODE_INVALID"}, operation="check_code"
        )

        assert error.context["operation"] == "check_code"
        assert set(error.context) == {"operation", "telegram_code", "telegram_message"}

    def test_translates_a_failure_the_client_already_raised(self) -> None:
        raised = TdlibRequestFailedError(
            "refused",
            user_message="Telegram refused that request.",
            context={
                "request_type": "checkAuthenticationCode",
                "code": 400,
                "message": "PHONE_CODE_INVALID",
            },
        )

        translated = error_mapping.translate_failure(raised, operation="check_code")

        assert isinstance(translated, AuthorizationError)

    def test_recognises_a_flood_wait(self) -> None:
        assert error_mapping.is_flood_wait({"message": "FLOOD_WAIT_42"}) == 42

    def test_ignores_anything_else(self) -> None:
        assert error_mapping.is_flood_wait({"message": "PHONE_CODE_INVALID"}) is None
        assert error_mapping.is_flood_wait({"message": "FLOOD_WAIT_soon"}) is None


# ---------------------------------------------------------------------------
# Gateway settings
# ---------------------------------------------------------------------------


class TestGatewaySettings:
    def test_secrets_stay_masked_until_the_request_is_built(self, tmp_path: Path) -> None:
        built = settings(tmp_path)

        assert MASK in repr(built.api_hash)
        assert MASK in repr(built.database_encryption_key)

    def test_the_request_carries_the_real_values(self, tmp_path: Path) -> None:
        # Revealed here and nowhere else, at the moment they reach the library.
        request = settings(tmp_path).to_request()

        assert request["api_hash"] == "0123456789abcdef0123456789abcdef"
        assert request["database_encryption_key"] == "test-session-key"

    def test_secret_chats_are_off(self, tmp_path: Path) -> None:
        # Maintaining end-to-end sessions this application never reads would
        # create key material with no consumer.
        assert settings(tmp_path).to_request()["use_secret_chats"] is False

    def test_the_store_goes_where_the_session_says(self, tmp_path: Path) -> None:
        request = settings(tmp_path).to_request()

        assert request["database_directory"] == str(tmp_path / "session")
        assert request["files_directory"] == str(tmp_path / "session" / "files")


# ---------------------------------------------------------------------------
# The adapter's own behaviour
# ---------------------------------------------------------------------------


class TestDispatch:
    async def test_connection_updates_are_recorded(self, tmp_path: Path) -> None:
        library = AuthorizingTdjson(starts_authorized=True)
        gateway = build(library, tmp_path)
        try:
            await gateway.connect()
            library.announce_connection("connectionStateUpdating")
            await _until(lambda: gateway._view.connection is ConnectionState.UPDATING)

            assert await gateway.connection_state() is ConnectionState.UPDATING
            assert await gateway.is_connected()
        finally:
            await gateway.disconnect()

    async def test_an_unknown_connection_state_is_counted_not_applied(self, tmp_path: Path) -> None:
        library = AuthorizingTdjson(starts_authorized=True)
        gateway = build(library, tmp_path)
        try:
            await gateway.connect()
            before = gateway.unhandled_updates
            library.announce_connection("connectionStateSomethingNew")
            await _until(lambda: gateway.unhandled_updates > before)

            assert await gateway.connection_state() is ConnectionState.OFFLINE
        finally:
            await gateway.disconnect()

    async def test_updates_with_no_consumer_are_counted(self, tmp_path: Path) -> None:
        # Counted rather than discarded silently. Reading them is slice 4's work.
        library = AuthorizingTdjson(starts_authorized=True)
        gateway = build(library, tmp_path)
        try:
            await gateway.connect()
            before = gateway.unhandled_updates
            library.push({"@type": "updateNewMessage", "message": {"id": 1}})
            await _until(lambda: gateway.unhandled_updates > before)

            assert gateway.unhandled_updates == before + 1
        finally:
            await gateway.disconnect()


class TestConnectFailures:
    async def test_rejected_application_credentials_are_reported_as_configuration(
        self, tmp_path: Path
    ) -> None:
        library = FakeTdjson()
        library.push(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateWaitTdlibParameters"},
            }
        )
        library.reply_to(
            "setTdlibParameters", {"@type": "error", "code": 400, "message": "API_ID_INVALID"}
        )
        gateway = build(library, tmp_path)

        with pytest.raises(TelegramNotConfiguredError) as excinfo:
            await gateway.connect()

        assert "api_id" in excinfo.value.user_message

    async def test_a_failed_connect_leaves_nothing_running(self, tmp_path: Path) -> None:
        # A half-started gateway owns a native client and a task; leaving either
        # behind would leak a thread on every failed connection.
        library = FakeTdjson()
        library.push(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateWaitTdlibParameters"},
            }
        )
        library.reply_to(
            "setTdlibParameters", {"@type": "error", "code": 400, "message": "API_ID_INVALID"}
        )
        gateway = build(library, tmp_path)

        with pytest.raises(TelegramNotConfiguredError):
            await gateway.connect()

        assert not await gateway.is_connected()
        with pytest.raises(TdlibNotRunningError):
            await gateway.get_me()

    async def test_a_silent_library_times_out_rather_than_hanging(self, tmp_path: Path) -> None:
        gateway = TdlibGateway(
            ACCOUNT, TdjsonClient(FakeTdjson()), settings(tmp_path), startup_timeout=0.2
        )

        with pytest.raises(TimeoutError):
            await gateway.connect()


class TestUnsupportedFlows:
    @pytest.mark.parametrize("raw", sorted(mapping.UNSUPPORTED_AUTHORIZATION_STATES))
    async def test_each_is_refused_with_its_own_explanation(self, tmp_path: Path, raw: str) -> None:
        library = AuthorizingTdjson()
        gateway = build(library, tmp_path)
        try:
            await gateway.connect()
            library.announce(raw)

            with pytest.raises(AuthorizationError) as excinfo:
                await gateway.start_authorization(ScriptedHandler())

            assert excinfo.value.user_message == mapping.UNSUPPORTED_AUTHORIZATION_STATES[raw]
        finally:
            await gateway.disconnect()

    async def test_an_unrecognised_state_is_reported_rather_than_ignored(
        self, tmp_path: Path
    ) -> None:
        library = AuthorizingTdjson()
        gateway = build(library, tmp_path)
        try:
            await gateway.connect()
            library.announce("authorizationStateWatFuture")

            with pytest.raises(TelegramError) as excinfo:
                await gateway.start_authorization(ScriptedHandler())

            assert excinfo.value.context["authorization_state"] == "authorizationStateWatFuture"
        finally:
            await gateway.disconnect()


class TestClosing:
    async def test_a_closing_client_stops_the_login_rather_than_hanging(
        self, tmp_path: Path
    ) -> None:
        library = AuthorizingTdjson()
        gateway = build(library, tmp_path)
        try:
            await gateway.connect()
            library.announce("authorizationStateClosed")
            await _until(lambda: gateway._view.closed)

            with pytest.raises(TdlibNotRunningError):
                await gateway.start_authorization(ScriptedHandler())
        finally:
            await gateway.disconnect()

    async def test_closing_is_not_recorded_as_a_logout(self, tmp_path: Path) -> None:
        library = AuthorizingTdjson(starts_authorized=True)
        gateway = build(library, tmp_path)
        try:
            await gateway.connect()
            library.announce("authorizationStateClosing")
            await _until(lambda: gateway._view.closed)

            assert await gateway.authorization_state() is AuthorizationState.READY
        finally:
            await gateway.disconnect()

    async def test_disconnect_after_a_login_leaves_it_unusable(self, tmp_path: Path) -> None:
        gateway = build(AuthorizingTdjson(starts_authorized=True), tmp_path)
        await gateway.connect()

        await gateway.disconnect()

        assert not await gateway.is_connected()
        with pytest.raises(TdlibNotRunningError):
            await gateway.logout()


class TestOperationsRequireAConnection:
    @pytest.mark.parametrize("operation", ["get_me", "logout"])
    async def test_each_refuses_without_one(self, tmp_path: Path, operation: str) -> None:
        # Never connecting implicitly: a method that silently opens a network
        # connection is a method whose cost is invisible.
        gateway = build(AuthorizingTdjson(), tmp_path)

        with pytest.raises(TdlibNotRunningError):
            await getattr(gateway, operation)()


async def _until(condition: Any, *, timeout: float = 2.0) -> None:
    """Wait for a condition the dispatch loop will make true."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            msg = "The dispatch loop did not reach the expected state"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)
