"""The migrations must reproduce Base.metadata exactly on PostgreSQL, and 0002/0003 must carry existing rows over."""

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

HEAD_REVISION = "0004_k402_utxo"


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
            assert versions == [HEAD_REVISION]
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


async def _scalar(url: str, sql: str) -> Any:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).scalar_one()
    finally:
        await engine.dispose()


async def _execute(url: str, *statements: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


def _settlement_insert(tx_hex: str, nonce: str | None = None) -> str:
    """One settlement row copied from the first endpoint; `nonce` is omitted for the pre-0002 schema."""
    columns = (
        "id, tx_id, endpoint_id, merchant_id, network, payer_address, merchant_address, treasury_address, "
        "amount_sompi, merchant_amount_sompi, fee_amount_sompi, status, request_method, request_path"
    )
    values = f"gen_random_uuid(), '{tx_hex}', e.id, e.merchant_id, 'kaspa:testnet-10', 'p', 'm', 't', 100, 90, 10, "
    values += "'settled', 'GET', '/p/x'"
    if nonce is not None:
        columns += ", payment_nonce"
        values += f", '{nonce}'"
    return f"INSERT INTO settlements ({columns}) SELECT {values} FROM endpoints e LIMIT 1"  # noqa: S608


async def test_0002_backfills_existing_settlements_and_roundtrips(pg_server: PgServer) -> None:
    url = pg_server.make_url("kasgate_upgrade")
    pg_server.create_database("kasgate_upgrade")
    config = alembic_config(url)
    await asyncio.to_thread(command.upgrade, config, "0001_initial")
    await _execute(
        url,
        "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
        "VALUES (gen_random_uuid(), 'a', 'm', repeat('a', 64))",
        "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_sompi, allowed_methods) "
        "SELECT gen_random_uuid(), id, 'weather', 'w', 'https://x.test', 100, '{GET}' FROM merchants LIMIT 1",
        _settlement_insert("a" * 64),
        _settlement_insert("b" * 64),
    )

    await asyncio.to_thread(command.upgrade, config, "head")
    nonces = await _scalar(url, "SELECT string_agg(payment_nonce, ',' ORDER BY tx_id) FROM settlements")
    first, second = nonces.split(",")
    assert first != second
    assert len(first) == len(second) == 32
    assert first == await _scalar(
        url, "SELECT md5('legacy:' || id::text) FROM settlements WHERE tx_id = '" + "a" * 64 + "'"
    )
    assert await _scalar(url, "SELECT count(*) FROM settlements WHERE payment_nonce ~ '^[0-9a-f]{32}$'") == 2
    assert await _scalar(url, "SELECT min(delivery_attempts) FROM settlements") == 1
    assert await _scalar(url, "SELECT max(delivery_attempts) FROM settlements") == 1

    migrated = create_async_engine(url, poolclass=NullPool)
    try:
        async with migrated.connect() as conn:
            assert await conn.run_sync(_diff) == []
    finally:
        await migrated.dispose()

    # down: the two columns, the index and both checks disappear, and the rows survive
    await asyncio.to_thread(command.downgrade, config, "0001_initial")
    assert await _scalar(url, "SELECT count(*) FROM settlements") == 2
    assert (
        await _scalar(
            url,
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'settlements' "
            "AND column_name IN ('payment_nonce', 'delivery_attempts')",
        )
        == 0
    )
    assert await _scalar(url, "SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_settlements_payment_nonce'") == 0
    assert (
        await _scalar(
            url,
            "SELECT count(*) FROM pg_constraint WHERE conname IN "
            "('ck_settlements_payment_nonce_hex32', 'ck_settlements_delivery_attempts_positive')",
        )
        == 0
    )

    # and up again, with data in place
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await _scalar(url, "SELECT count(DISTINCT payment_nonce) FROM settlements") == 2
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION


async def test_0002_constraints_and_server_defaults(database_url: str) -> None:
    from sqlalchemy.exc import IntegrityError

    await _execute(
        database_url,
        "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
        "VALUES (gen_random_uuid(), 'a', 'm', repeat('a', 64))",
        "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_sompi, allowed_methods) "
        "SELECT gen_random_uuid(), id, 'weather', 'w', 'https://x.test', 100, '{GET}' FROM merchants LIMIT 1",
    )
    try:
        # payment_nonce is NOT NULL and has no default: the application always supplies it
        with pytest.raises(IntegrityError):
            await _execute(database_url, _settlement_insert("c" * 64))
        await _execute(database_url, _settlement_insert("c" * 64, "1" * 32))
        assert await _scalar(database_url, "SELECT delivery_attempts FROM settlements") == 1  # server default
        for bad_nonce in ("1" * 31, "G" * 32, "A" * 32):  # ck_settlements_payment_nonce_hex32
            with pytest.raises(IntegrityError):
                await _execute(database_url, _settlement_insert("d" * 64, bad_nonce))
        with pytest.raises(IntegrityError):  # unique ix_settlements_payment_nonce
            await _execute(database_url, _settlement_insert("e" * 64, "1" * 32))
        with pytest.raises(IntegrityError):  # ck_settlements_delivery_attempts_positive
            await _execute(database_url, "UPDATE settlements SET delivery_attempts = 0")
    finally:
        await _execute(database_url, "TRUNCATE settlements, endpoints, merchants CASCADE")


async def test_0003_backfills_kas_defaults_and_roundtrips(pg_server: PgServer) -> None:
    url = pg_server.make_url("kasgate_usd_upgrade")
    pg_server.create_database("kasgate_usd_upgrade")
    config = alembic_config(url)
    await asyncio.to_thread(command.upgrade, config, "0002_payment_nonce")
    await _execute(
        url,
        "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
        "VALUES (gen_random_uuid(), 'a', 'm', repeat('a', 64))",
        "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_sompi, allowed_methods) "
        "SELECT gen_random_uuid(), id, 'weather', 'w', 'https://x.test', 100, '{GET}' FROM merchants LIMIT 1",
    )

    await asyncio.to_thread(command.upgrade, config, "head")
    assert await _scalar(url, "SELECT price_currency::text FROM endpoints") == "KAS"
    assert await _scalar(url, "SELECT price_sompi FROM endpoints") == 100
    assert await _scalar(url, "SELECT price_usd_micros FROM endpoints") is None
    labels = await _scalar(
        url,
        "SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'price_currency'",
    )
    assert labels == "KAS,USD"
    migrated = create_async_engine(url, poolclass=NullPool)
    try:
        async with migrated.connect() as conn:
            assert await conn.run_sync(_diff) == []
    finally:
        await migrated.dispose()

    await asyncio.to_thread(command.downgrade, config, "0002_payment_nonce")
    assert await _scalar(url, "SELECT price_sompi FROM endpoints") == 100
    assert (
        await _scalar(
            url,
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'endpoints' "
            "AND column_name IN ('price_currency', 'price_usd_micros')",
        )
        == 0
    )
    assert await _scalar(url, "SELECT count(*) FROM pg_type WHERE typname = 'price_currency'") == 0
    assert (
        await _scalar(
            url,
            "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_endpoints_price_positive'",
        )
        == 1
    )

    await asyncio.to_thread(command.upgrade, config, "head")
    assert await _scalar(url, "SELECT price_currency::text FROM endpoints") == "KAS"
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION
    assert await _scalar(url, "SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_endpoints_slug'") == 1
    assert await _scalar(url, "SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_settlements_tx_id'") == 1


async def test_0003_price_matches_currency_constraints(database_url: str) -> None:
    from sqlalchemy.exc import IntegrityError

    await _execute(
        database_url,
        "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
        "VALUES (gen_random_uuid(), 'a', 'm', repeat('a', 64))",
    )
    try:
        # USD with a sompi price and no micros
        with pytest.raises(IntegrityError):
            await _execute(
                database_url,
                "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_currency, "
                "price_sompi, allowed_methods) "
                "SELECT gen_random_uuid(), id, 'bad-usd', 'w', 'https://x.test', 'USD', 100, '{GET}' "
                "FROM merchants LIMIT 1",
            )
        # KAS with micros
        with pytest.raises(IntegrityError):
            await _execute(
                database_url,
                "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_currency, "
                "price_sompi, price_usd_micros, allowed_methods) "
                "SELECT gen_random_uuid(), id, 'bad-kas', 'w', 'https://x.test', 'KAS', 100, 1, '{GET}' "
                "FROM merchants LIMIT 1",
            )
        # USD with zero micros
        with pytest.raises(IntegrityError):
            await _execute(
                database_url,
                "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_currency, "
                "price_usd_micros, allowed_methods) "
                "SELECT gen_random_uuid(), id, 'zero', 'w', 'https://x.test', 'USD', 0, '{GET}' "
                "FROM merchants LIMIT 1",
            )
        # a valid USD row
        await _execute(
            database_url,
            "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_currency, "
            "price_usd_micros, allowed_methods) "
            "SELECT gen_random_uuid(), id, 'usd-ok', 'w', 'https://x.test', 'USD', 1000000, '{GET}' "
            "FROM merchants LIMIT 1",
        )
        assert await _scalar(database_url, "SELECT price_sompi FROM endpoints WHERE slug = 'usd-ok'") is None
        # default currency is KAS when omitted
        await _execute(
            database_url,
            "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_sompi, allowed_methods) "
            "SELECT gen_random_uuid(), id, 'kas-ok', 'w', 'https://x.test', 100, '{GET}' FROM merchants LIMIT 1",
        )
        assert await _scalar(database_url, "SELECT price_currency::text FROM endpoints WHERE slug = 'kas-ok'") == "KAS"
    finally:
        await _execute(database_url, "TRUNCATE settlements, endpoints, merchants CASCADE")


async def test_0003_refuses_to_downgrade_usd_rows(pg_server: PgServer) -> None:
    url = pg_server.make_url("kasgate_usd_downgrade")
    pg_server.create_database("kasgate_usd_downgrade")
    config = alembic_config(url)
    await asyncio.to_thread(command.upgrade, config, "head")
    await _execute(
        url,
        "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
        "VALUES (gen_random_uuid(), 'a', 'm', repeat('a', 64))",
        "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_currency, "
        "price_usd_micros, allowed_methods) "
        "SELECT gen_random_uuid(), id, 'usd', 'w', 'https://x.test', 'USD', 1000000, '{GET}' FROM merchants LIMIT 1",
    )
    with pytest.raises(Exception, match="priced in USD"):
        await asyncio.to_thread(command.downgrade, config, "0002_payment_nonce")
    assert await _scalar(url, "SELECT price_currency::text FROM endpoints") == "USD"


def _k402_insert(tx_hex: str, nonce: str, payment_id: str | None, payer: str | None = None) -> str:
    scheme = "'k402-kaspa-utxo'" if payment_id is not None or payer is None else "'kaspa-exact-v1'"
    pid = "NULL" if payment_id is None else f"'{payment_id}'"
    payer_sql = "NULL" if payer is None else f"'{payer}'"
    return (
        "INSERT INTO settlements (id, tx_id, endpoint_id, merchant_id, network, payment_scheme, payer_address, "  # noqa: S608
        "merchant_address, treasury_address, amount_sompi, merchant_amount_sompi, fee_amount_sompi, status, "
        "request_method, request_path, payment_nonce, k402_payment_id) "
        f"SELECT gen_random_uuid(), '{tx_hex}', e.id, e.merchant_id, 'kaspa:testnet-10', {scheme}, {payer_sql}, "
        f"'m', 't', 100, 90, 10, 'settled', 'GET', '/p/x', '{nonce}', {pid} FROM endpoints e LIMIT 1"
    )


async def test_0004_k402_constraints_and_guarded_downgrade(pg_server: PgServer) -> None:
    from sqlalchemy.exc import DBAPIError, IntegrityError

    url = pg_server.make_url("kasgate_k402")
    pg_server.create_database("kasgate_k402")
    config = alembic_config(url)
    await asyncio.to_thread(command.upgrade, config, "head")
    await _execute(
        url,
        "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
        "VALUES (gen_random_uuid(), 'a', 'm', repeat('a', 64))",
        "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_sompi, allowed_methods) "
        "SELECT gen_random_uuid(), id, 'weather', 'w', 'https://x.test', 100, '{GET}' FROM merchants LIMIT 1",
        _settlement_insert("a" * 64, "1" * 32),
    )
    assert await _scalar(url, "SELECT payment_scheme FROM settlements") == "kaspa-exact-v1"  # server default
    await _execute(url, _k402_insert("b" * 64, "2" * 32, "p_0123456789abcdef"))
    assert await _scalar(url, "SELECT payer_address IS NULL FROM settlements WHERE tx_id = '" + "b" * 64 + "'")
    for bad in (
        _k402_insert("c" * 64, "3" * 32, "p_0123456789abcdef"),  # unique ix_settlements_k402_payment_id
        _k402_insert("c" * 64, "3" * 32, "p_0123"),  # ck_settlements_k402_payment_id_format
        _k402_insert("c" * 64, "3" * 32, None),  # ck_settlements_k402_payment_id_iff_k402
        _k402_insert("c" * 64, "3" * 32, None, payer="p").replace("'kaspa-exact-v1', 'p'", "'kaspa-exact-v1', NULL"),
        "UPDATE settlements SET payment_scheme = 'other' WHERE tx_id = '" + "a" * 64 + "'",
        "UPDATE settlements SET k402_payment_id = 'p_1111111111111111' WHERE tx_id = '" + "a" * 64 + "'",
    ):
        with pytest.raises(IntegrityError):
            await _execute(url, bad)

    # a downgrade would have to invent payer addresses, so it refuses while k402 rows exist
    with pytest.raises(DBAPIError, match="k402 settlements exist"):
        await asyncio.to_thread(command.downgrade, config, "0003_usd_pricing")
    await _execute(url, "DELETE FROM settlements WHERE payment_scheme = 'k402-kaspa-utxo'")
    await asyncio.to_thread(command.downgrade, config, "0003_usd_pricing")
    assert (
        await _scalar(
            url,
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'settlements' "
            "AND column_name IN ('payment_scheme', 'k402_payment_id')",
        )
        == 0
    )
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION
