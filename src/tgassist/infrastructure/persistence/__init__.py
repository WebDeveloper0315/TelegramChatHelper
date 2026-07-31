"""SQLite persistence: engine, unit of work, repositories and migrations.

Uses SQLAlchemy Core rather than the ORM (ADR-015), and runs every database
operation on one dedicated worker thread (ADR-013).
"""

from tgassist.infrastructure.persistence.accounts import (
    AccountMapper,
    SqlAccountRepository,
    account_repository,
)
from tgassist.infrastructure.persistence.ai_calls import (
    AiCallMapper,
    SqlAiCallRepository,
    ai_call_repository,
)
from tgassist.infrastructure.persistence.chats import (
    ChatMapper,
    SqlChatRepository,
    chat_repository,
)
from tgassist.infrastructure.persistence.contacts import (
    ContactMapper,
    SqlContactRepository,
    contact_repository,
)
from tgassist.infrastructure.persistence.conversations import (
    ConversationMapper,
    SqlConversationRepository,
    conversation_repository,
)
from tgassist.infrastructure.persistence.cursor import Cursor
from tgassist.infrastructure.persistence.engine import MEMORY_URL, SqliteDatabase, build_url
from tgassist.infrastructure.persistence.executor import DatabaseExecutor
from tgassist.infrastructure.persistence.memories import (
    MemoryMapper,
    SqlMemoryRepository,
    memory_repository,
)
from tgassist.infrastructure.persistence.memory_proposals import (
    MemoryProposalMapper,
    SqlMemoryProposalRepository,
    memory_proposal_repository,
)
from tgassist.infrastructure.persistence.messages import (
    MessageMapper,
    SqlMessageRepository,
    message_repository,
)
from tgassist.infrastructure.persistence.migrations import (
    AlembicMigrationRunner,
    build_alembic_config,
)
from tgassist.infrastructure.persistence.pagination import KeysetPaginator
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import metadata, schema_metadata
from tgassist.infrastructure.persistence.sessions import (
    SessionMapper,
    SqlSessionRepository,
    session_repository,
)
from tgassist.infrastructure.persistence.suggestions import (
    SqlSuggestionRepository,
    SuggestionMapper,
    suggestion_repository,
)
from tgassist.infrastructure.persistence.sync_cursors import (
    SqlSyncCursorRepository,
    SyncCursorMapper,
    sync_cursor_repository,
)
from tgassist.infrastructure.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
    translate_database_error,
)
from tgassist.infrastructure.persistence.user_profiles import (
    SqlUserProfileRepository,
    UserProfileMapper,
    user_profile_repository,
)

__all__ = [
    "MEMORY_URL",
    "AccountMapper",
    "AiCallMapper",
    "AlembicMigrationRunner",
    "ChatMapper",
    "ContactMapper",
    "ConversationMapper",
    "Cursor",
    "DatabaseExecutor",
    "KeysetPaginator",
    "MemoryMapper",
    "MemoryProposalMapper",
    "MessageMapper",
    "Repository",
    "SessionMapper",
    "SqlAccountRepository",
    "SqlAiCallRepository",
    "SqlAlchemyUnitOfWork",
    "SqlChatRepository",
    "SqlContactRepository",
    "SqlConversationRepository",
    "SqlMemoryProposalRepository",
    "SqlMemoryRepository",
    "SqlMessageRepository",
    "SqlSessionRepository",
    "SqlSuggestionRepository",
    "SqlSyncCursorRepository",
    "SqlUserProfileRepository",
    "SqliteDatabase",
    "SuggestionMapper",
    "SyncCursorMapper",
    "UnitOfWorkFactory",
    "UserProfileMapper",
    "account_repository",
    "ai_call_repository",
    "build_alembic_config",
    "build_url",
    "chat_repository",
    "contact_repository",
    "conversation_repository",
    "memory_proposal_repository",
    "memory_repository",
    "message_repository",
    "metadata",
    "schema_metadata",
    "session_repository",
    "suggestion_repository",
    "sync_cursor_repository",
    "translate_database_error",
    "user_profile_repository",
]
