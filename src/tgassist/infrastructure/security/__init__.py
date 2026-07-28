"""Secret storage and filesystem permission enforcement.

Implements ADR-021: environment variable over operating system credential
store, with no plaintext fallback.
"""

from tgassist.infrastructure.security.secret_store import (
    SERVICE_NAME,
    ChainedSecretStore,
    EnvironmentSecretStore,
    KeyringSecretStore,
    build_default_secret_store,
)

__all__ = [
    "SERVICE_NAME",
    "ChainedSecretStore",
    "EnvironmentSecretStore",
    "KeyringSecretStore",
    "build_default_secret_store",
]
