"""The Session aggregate.

Where an Account stands with Telegram: whether it has credentials, whether it
has a connection, and where its encrypted local store lives.

Two axes, not one
-----------------

``DOMAIN_MODEL.md`` version 1.0 gave Session a single state machine running from
``disconnected`` through ``awaiting_code`` to ``ready`` and ``reconnecting``.
TDLib reports **two** states, and they vary independently: ``authorizationState``
answers "do we have credentials", ``connectionState`` answers "is the socket up".

A single enum cannot express *authorized but currently reconnecting*, which is
the ordinary condition after any network interruption. Under the old model a
reconnect had to overwrite ``ready``, discarding the fact that the account was
authorized, and the code then had to *infer* that authorization survived. That
inference works until a login expires during a reconnect.

So there are two fields, each mirroring what TDLib reports, and the adapter
translates rather than infers. ``can_send`` answers the question the old
invariant asked ambiguously -- "only the ``ready`` state permits sending", ready
in which sense? -- once, so no call site decides for itself. See ADR-049.

Identity
--------

``account_id`` is the primary key. An Account has exactly one Session, so a
surrogate key would be a second name for one row -- the same reasoning ADR-038
applied to ``UserProfile``.

The key is a name, never a value
--------------------------------

``encryption_key_ref`` holds a *name* in the ``SecretStore``. The key itself
lives in the operating system's credential store and never touches this row, a
log, a backup or an export (``SECURITY.md`` section 7). A key value in this
column is a security defect, not a shortcut.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import AccountId, require_positive_identifier

MAX_KEY_REF_LENGTH: Final = 128


class AuthorizationState(StrEnum):
    """Whether this account has usable Telegram credentials.

    Mirrors TDLib's ``authorizationState`` so the adapter translates rather than
    interprets. Varies independently of :class:`ConnectionState`.
    """

    UNAUTHORIZED = "unauthorized"
    """No credentials, and none being collected."""

    WAITING_PHONE = "waiting_phone"
    WAITING_CODE = "waiting_code"
    # The suppression below is for a value that names a state, not a credential.
    # No password is ever held on this entity, only the fact that one is awaited.
    WAITING_PASSWORD = "waiting_password"  # noqa: S105
    """Two-factor authentication is enabled and the password is outstanding."""

    READY = "ready"
    """Authorized. Says nothing about whether a connection currently exists."""

    LOGGED_OUT = "logged_out"
    """Deliberately signed out. Local session material is destroyed."""


class ConnectionState(StrEnum):
    """Whether a connection to Telegram currently exists.

    Mirrors TDLib's ``connectionState``. Transient: it changes with the network
    and says nothing about whether credentials are valid.
    """

    OFFLINE = "offline"
    CONNECTING = "connecting"
    UPDATING = "updating"
    """Connected and catching up on missed updates."""

    READY = "ready"
    WAITING_FOR_NETWORK = "waiting_for_network"


#: The states in which a connection to Telegram actually exists.
#:
#: TDLib's sequence is ``connecting`` -> ``updating`` -> ``ready``, and the
#: socket is up from ``updating`` onwards: that state means *connected and
#: catching up*. Treating only ``ready`` as connected would date every
#: connection from the moment its backlog finished draining, which for a client
#: that has been offline for a week is a long way from when it connected.
CONNECTED_STATES: Final = frozenset({ConnectionState.UPDATING, ConnectionState.READY})


@dataclass(frozen=True, slots=True)
class Session:
    """One account's standing with Telegram.

    Immutable, like every entity here: transitions return a new instance.

    Attributes:
        account_id: The Account this session belongs to, and its identity.
        authorization_state: Whether credentials exist.
        connection_state: Whether a connection exists.
        session_path: Directory holding TDLib's encrypted local store. Its
            *contents* are never read, logged or exported by this application.
        encryption_key_ref: The **name** under which the store's key is held in
            the ``SecretStore``. Never key material.
        client_version: The TDLib version that last wrote the store, or ``None``
            before anything has. Recorded because a store written by a newer
            TDLib may not be readable by an older one.
        connected_at: When the current connection was established, or ``None``.
        last_activity_at: When Telegram was last heard from, or ``None``.
        created_at: When this session record was first written, UTC.
        updated_at: When it last changed, UTC.
    """

    account_id: AccountId
    authorization_state: AuthorizationState
    connection_state: ConnectionState
    session_path: Path
    encryption_key_ref: str
    client_version: str | None
    connected_at: datetime | None
    last_activity_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        """Validate every invariant this entity is responsible for.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        require_positive_identifier(self.account_id, name="Account id")

        if not str(self.session_path).strip():
            msg = "A session requires a path to its local store"
            raise DomainValidationError(msg, user_message="That session has no storage location.")

        reference = self.encryption_key_ref.strip()
        if not reference:
            msg = "A session requires the name of its encryption key"
            raise DomainValidationError(
                msg, user_message="That session has no encryption key reference."
            )
        if len(reference) > MAX_KEY_REF_LENGTH:
            msg = f"A key reference may be at most {MAX_KEY_REF_LENGTH} characters"
            raise DomainValidationError(
                msg, user_message="That encryption key reference is too long."
            )

        for value, name in (
            (self.created_at, "created_at"),
            (self.updated_at, "updated_at"),
            (self.connected_at, "connected_at"),
            (self.last_activity_at, "last_activity_at"),
        ):
            if value is not None:
                _require_utc(value, name=name)

        if self.updated_at < self.created_at:
            msg = (
                f"A session cannot be updated before it was created: "
                f"{self.updated_at} < {self.created_at}"
            )
            raise DomainValidationError(msg, user_message="That session has inconsistent dates.")

        # A connection timestamp without a connection is a leftover from a
        # previous one, and would make "how long have we been connected"
        # answer with a duration that never happened.
        if self.connection_state not in CONNECTED_STATES and self.connected_at is not None:
            msg = (
                f"A session in state {self.connection_state.value} is not connected "
                f"and cannot record a connection time"
            )
            raise DomainValidationError(
                msg, user_message="That session has inconsistent connection state."
            )

    # -- Construction -----------------------------------------------------

    @classmethod
    def prepare(
        cls,
        *,
        account_id: AccountId,
        session_path: Path,
        encryption_key_ref: str,
        now: datetime,
    ) -> Session:
        """Build a session that has storage and a key but no credentials.

        The state authentication starts from: somewhere to put the encrypted
        store, and a key to encrypt it with. Everything else is what logging in
        establishes.
        """
        return cls(
            account_id=account_id,
            authorization_state=AuthorizationState.UNAUTHORIZED,
            connection_state=ConnectionState.OFFLINE,
            session_path=session_path,
            encryption_key_ref=encryption_key_ref,
            client_version=None,
            connected_at=None,
            last_activity_at=None,
            created_at=now,
            updated_at=now,
        )

    # -- Derived state ----------------------------------------------------

    @property
    def is_authorized(self) -> bool:
        """Whether this account has usable credentials."""
        return self.authorization_state is AuthorizationState.READY

    @property
    def is_connected(self) -> bool:
        """Whether a connection to Telegram currently exists.

        True from ``updating`` onwards. A client draining a backlog is
        connected; whether it is *caught up* is what ``can_send`` adds.
        """
        return self.connection_state in CONNECTED_STATES

    @property
    def can_send(self) -> bool:
        """Whether a message could be sent right now.

        Both axes must be ``ready``. This replaces "only the ``ready`` state
        permits sending", which could not say which sense of ready it meant, and
        makes the ambiguity unrepresentable rather than merely documented.

        Deliberately stricter than :attr:`is_connected`: a session that is
        ``updating`` has a connection but has not finished replaying what it
        missed, so it may not yet know that the conversation it is about to
        reply to has moved on. Suggesting a reply into a stale view of a chat is
        exactly the mistake this application exists to avoid, and waiting costs
        seconds.
        """
        return self.is_authorized and self.connection_state is ConnectionState.READY

    @property
    def needs_credentials(self) -> bool:
        """Whether the authorization flow is waiting for something from a person."""
        return self.authorization_state in {
            AuthorizationState.WAITING_PHONE,
            AuthorizationState.WAITING_CODE,
            AuthorizationState.WAITING_PASSWORD,
        }

    # -- Transitions ------------------------------------------------------

    def with_authorization(self, state: AuthorizationState, now: datetime) -> Session:
        """Return this Session with a new authorization state.

        Returns ``self`` when unchanged, so a repeated report from TDLib does
        not move ``updated_at`` and make nothing look like something.
        """
        if state is self.authorization_state:
            return self
        return replace(self, authorization_state=state, updated_at=now)

    def with_connection(self, state: ConnectionState, now: datetime) -> Session:
        """Return this Session with a new connection state.

        Stamps ``connected_at`` on becoming connected and clears it on losing
        the connection, because a connection time outliving its connection would
        answer "how long connected" with a duration that never happened.

        Moving between two connected states -- ``updating`` to ``ready`` -- keeps
        the original stamp: the connection did not restart, it finished catching
        up.
        """
        if state is self.connection_state:
            return self

        if state not in CONNECTED_STATES:
            connected_at = None
        elif self.connected_at is None:
            connected_at = now
        else:
            connected_at = self.connected_at

        return replace(self, connection_state=state, connected_at=connected_at, updated_at=now)

    def with_activity(self, now: datetime) -> Session:
        """Return this Session with its last-heard-from time moved forward."""
        return replace(self, last_activity_at=now, updated_at=now)

    def with_client_version(self, version: str, now: datetime) -> Session:
        """Return this Session recording which TDLib last wrote its store."""
        if version == self.client_version:
            return self
        return replace(self, client_version=version, updated_at=now)

    def logged_out(self, now: datetime) -> Session:
        """Return this Session signed out and disconnected.

        Both axes move together, because logging out ends the connection as
        well as the credentials. Destroying the local store and the key is the
        caller's work: an entity cannot delete a directory.
        """
        return replace(
            self,
            authorization_state=AuthorizationState.LOGGED_OUT,
            connection_state=ConnectionState.OFFLINE,
            connected_at=None,
            updated_at=now,
        )


def _require_utc(value: datetime, *, name: str) -> None:
    """Raise unless ``value`` is timezone-aware and in UTC."""
    if value.tzinfo is None:
        msg = f"{name} must be timezone-aware; naive datetimes have no defined instant"
        raise DomainValidationError(msg, user_message="That session has an invalid timestamp.")
    if value.utcoffset() != UTC.utcoffset(None):
        msg = f"{name} must be UTC, got offset {value.utcoffset()}"
        raise DomainValidationError(msg, user_message="That session has an invalid timestamp.")


__all__ = [
    "CONNECTED_STATES",
    "MAX_KEY_REF_LENGTH",
    "AuthorizationState",
    "ConnectionState",
    "Session",
]
