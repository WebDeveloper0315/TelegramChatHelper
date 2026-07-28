"""Alembic environment.

Runs migrations against a connection supplied by the application rather than
one Alembic opens itself. That matters for two reasons: the application's
connection already has foreign keys, journal mode and busy timeout applied, and
reusing it keeps every database operation on the single worker thread that
ADR-013 requires.
"""

from __future__ import annotations

from typing import Any

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

from tgassist.infrastructure.persistence.schema import metadata

config = context.config

target_metadata = metadata

# SQLite cannot drop or alter a column in place. Batch mode rewrites the table
# instead, which is transparent here and a no-op on PostgreSQL, so migrations
# stay portable (ADR-016).
RENDER_AS_BATCH = True


def _configure(connection: Connection, **kwargs: Any) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=RENDER_AS_BATCH,
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Emit migration SQL without a database connection.

    Used to review what a migration would do before letting it near real data.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=RENDER_AS_BATCH,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = config.attributes.get("connection", None)

    if isinstance(connectable, Connection):
        # The application supplied its connection; use it and do not close it.
        _configure(connectable)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with engine.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
