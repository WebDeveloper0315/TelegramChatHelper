"""Domain ports: every interface the application depends on.

Infrastructure supplies the adapters. Declaring ports here -- rather than beside
their implementations -- is what allows the domain to be exercised without a
database, a network or a model. See ``docs/API.md``.
"""

from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.database import Database, HealthReport, PragmaState
from tgassist.domain.ports.event_bus import EventBus, EventHandler, Subscription
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.migration_runner import (
    MigrationInfo,
    MigrationReport,
    MigrationRunner,
    SchemaState,
    SchemaStatus,
)
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.secret_store import SecretStore
from tgassist.domain.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tgassist.domain.ports.user_profile_repository import UserProfileRepository

__all__ = [
    "AccountRepository",
    "Clock",
    "Database",
    "EventBus",
    "EventHandler",
    "HealthReport",
    "IdGenerator",
    "MigrationInfo",
    "MigrationReport",
    "MigrationRunner",
    "PragmaState",
    "RepositoryFactory",
    "SchemaState",
    "SchemaStatus",
    "ScopedRepositoryFactory",
    "SecretStore",
    "Subscription",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "UserProfileRepository",
]
