"""Fake implementations of the domain ports.

Fakes are not stubs. Each is a behaviourally correct implementation honouring
the same contract as its production counterpart, which is why the shared
contract suite runs against both: a fake that drifts from real behaviour turns
every test using it into a false positive.
"""

from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.clock import EPOCH, AdvanceableClock, FixedClock
from tests.fakes.event_bus import RecordingEventBus
from tests.fakes.id_generator import SequentialIdGenerator
from tests.fakes.secret_store import InMemorySecretStore, UnavailableSecretStore
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory

__all__ = [
    "EPOCH",
    "AdvanceableClock",
    "FixedClock",
    "InMemoryAccountRepository",
    "InMemorySecretStore",
    "InMemoryUnitOfWork",
    "InMemoryUnitOfWorkFactory",
    "RecordingEventBus",
    "SequentialIdGenerator",
    "UnavailableSecretStore",
]
