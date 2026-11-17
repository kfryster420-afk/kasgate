"""0001_initial must reproduce Base.metadata exactly on PostgreSQL."""

import asyncio
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Base
from tests.conftest import PgServer, alembic_config


def _diff(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
    return list(compare_metadata(context, Base.metadata))


def _structure(connection: Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    structure: dict[str, Any] = {}
    for table in sorted(Base.metadata.tables):
        structure[table] = {
            "indexes": sorted(
                (i["name"], tuple(i["column_names"]), bool(i["unique"]), str(i.get("dialect_options", {})))
                for i in inspector.get_indexes(table)
            ),
            "checks": sorted((c["name"], c["sqltext"]) for c in inspector.get_check_constraints(table)),
            "fks": sorted(
                (f["name"], tuple(f["constrained_columns"]), f["referred_table"], str(f.get("options")))
                for f in inspector.get_foreign_keys(table)
            ),
            "pk": inspector.get_pk_constraint(table)["name"],
        }
    return structure


async def test_migration_matches_models_and_roundtrips(pg_server: PgServer) -> None:
    migrated_url = pg_server.make_url("kasgate_migrated")
    created_url = pg_server.make_url("kasgate_created")
    pg_server.create_database("kasgate_migrated")
    pg_server.create_database("kasgate_created")

    await asyncio.to_thread(command.upgrade, alembic_config(migrated_url), "head")

    created = create_async_engine(created_url, poolclass=NullPool)
    async with created.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    migrated = create_async_engine(migrated_url, poolclass=NullPool)
    try:
        async with migrated.connect() as conn:
            assert await conn.run_sync(_diff) == []
            labels = (
                (
                    await conn.execute(
                        text(
                            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                            "WHERE t.typname = 'settlement_status' ORDER BY e.enumsortorder"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert labels == ["pending", "settled", "rejected"]
            migrated_structure = await conn.run_sync(_structure)
            versions = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
            assert versions == ["0001_initial"]
        async with created.connect() as conn:
            assert await conn.run_sync(_structure) == migrated_structure
    finally:
        await migrated.dispose()
        await created.dispose()

    await asyncio.to_thread(command.downgrade, alembic_config(migrated_url), "base")
    engine = create_async_engine(migrated_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tables = (
                (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))).scalars().all()
            )
            enums = (
                (await conn.execute(text("SELECT typname FROM pg_type WHERE typname='settlement_status'")))
                .scalars()
                .all()
            )
            assert list(tables) == ["alembic_version"]
            assert list(enums) == []
    finally:
        await engine.dispose()


async def test_constraints_are_enforced_by_the_migrated_schema(database_url: str) -> None:
    from sqlalchemy.exc import IntegrityError

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
                    "VALUES (gen_random_uuid(), 'a', 'm', repeat('a', 64))"
                )
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
                        "VALUES (gen_random_uuid(), 'a', 'm2', repeat('b', 64))"
                    )
                )
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE settlements, endpoints, merchants CASCADE"))
        await engine.dispose()
