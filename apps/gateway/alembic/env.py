"""Async Alembic environment. The database URL comes from application settings."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import Base
from app.schema_guard import REVISION_0008, stale_0008_problem

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    override = config.get_main_option("sqlalchemy.url")
    return override if override else get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _targets_0008_or_later() -> bool:
    """True when the command moves the database to 0008 or a later revision (upgrade, stamp, a downgrade that
    stops at or above 0008); False for a downgrade below 0008 or commands without a destination."""
    destination = context.get_revision_argument()
    if destination is None:
        return False
    targets = destination if isinstance(destination, tuple) else (destination,)
    script = context.script
    for target in targets:
        if not isinstance(target, str) or target == "base" or target.startswith("-"):
            continue
        if any(rev.revision == REVISION_0008 for rev in script.iterate_revisions(target, "base")):
            return True
    return False


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        if _targets_0008_or_later():
            problem = stale_0008_problem(connection)
            if problem is not None:
                raise RuntimeError(problem)
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
