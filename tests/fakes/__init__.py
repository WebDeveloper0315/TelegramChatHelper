"""Fake implementations of the domain ports.

Fakes are not stubs. Each is a behaviourally correct implementation honouring
the same contract as its production counterpart, which is why the shared
contract suite runs against both: a fake that drifts from real behaviour turns
every test using it into a false positive.
"""

from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.chat_repository import InMemoryChatRepository, InMemoryChatStore
from tests.fakes.clock import EPOCH, AdvanceableClock, FixedClock
from tests.fakes.contact_repository import (
    InMemoryContactRepository,
    InMemoryContactStore,
)
from tests.fakes.event_bus import RecordingEventBus
from tests.fakes.id_generator import SequentialIdGenerator
from tests.fakes.message_repository import (
    InMemoryMessageRepository,
    InMemoryMessageStore,
)
from tests.fakes.secret_store import InMemorySecretStore, UnavailableSecretStore
from tests.fakes.session_repository import (
    InMemorySessionRepository,
    InMemorySessionStore,
)
from tests.fakes.telegram_gateway import (
    DEFAULT_USER,
    FakeTelegramGateway,
    ScriptedHandler,
)
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tests.fakes.user_profile_repository import (
    InMemoryProfileStore,
    InMemoryUserProfileRepository,
)

__all__ = [
    "DEFAULT_USER",
    "EPOCH",
    "AdvanceableClock",
    "FakeTelegramGateway",
    "FixedClock",
    "InMemoryAccountRepository",
    "InMemoryChatRepository",
    "InMemoryChatStore",
    "InMemoryContactRepository",
    "InMemoryContactStore",
    "InMemoryMessageRepository",
    "InMemoryMessageStore",
    "InMemoryProfileStore",
    "InMemorySecretStore",
    "InMemorySessionRepository",
    "InMemorySessionStore",
    "InMemoryUnitOfWork",
    "InMemoryUnitOfWorkFactory",
    "InMemoryUserProfileRepository",
    "RecordingEventBus",
    "ScriptedHandler",
    "SequentialIdGenerator",
    "UnavailableSecretStore",
]
