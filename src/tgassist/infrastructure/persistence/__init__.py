"""SQLite persistence: engine, unit of work, repositories and migrations.

Uses SQLAlchemy Core rather than the ORM (ADR-015), and runs every database
operation on one dedicated worker thread (ADR-013).
"""

from tgassist.infrastructure.persistence.accounts import (
    AccountMapper,
    SqlAccountRepository,
    account_repository,
)
from tgassist.infrastructure.persistence.cursor import Cursor
from tgassist.infrastructure.persistence.engine import MEMORY_URL, SqliteDatabase, build_url
from tgassist.infrastructure.persistence.executor import DatabaseExecutor
from tgassist.infrastructure.persistence.migrations import (
    AlembicMigrationRunner,
    build_alembic_config,
)
from tgassist.infrastructure.persistence.pagination import KeysetPaginator
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import metadata, schema_metadata
from tgassist.infrastructure.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
    translate_database_error,
)

__all__ = [
    "MEMORY_URL",
    "AccountMapper",
    "AlembicMigrationRunner",
    "Cursor",
    "DatabaseExecutor",
    "KeysetPaginator",
    "Repository",
    "SqlAccountRepository",
    "SqlAlchemyUnitOfWork",
    "SqliteDatabase",
    "UnitOfWorkFactory",
    "account_repository",
    "build_alembic_config",
    "build_url",
    "metadata",
    "schema_metadata",
    "translate_database_error",
]
