"""The migrations must reproduce Base.metadata exactly on PostgreSQL, and 0002-0009 must carry existing rows over."""

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

HEAD_REVISION = "0009_failed_delivery_credits"


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


def _exact_v2_insert(
    tx_hex: str,
    payment_identifier: str | None,
    request_hash: str | None = "f" * 64,
    *,
    fee: int = 0,
    treasury: str | None = None,
    nonce: str | None = None,
    payer: str | None = "p",
) -> str:
    """One CONTRACT v2.10 kaspa-exact-v2 settlement row copied from the first endpoint."""

    def lit(value: str | None) -> str:
        return "NULL" if value is None else f"'{value}'"

    columns = (
        "id, tx_id, endpoint_id, merchant_id, network, payment_scheme, payer_address, merchant_address, "
        "treasury_address, amount_sompi, merchant_amount_sompi, fee_amount_sompi, status, request_method, "
        "request_path, payment_nonce, payment_identifier, request_hash"
    )
    values = (
        f"gen_random_uuid(), '{tx_hex}', e.id, e.merchant_id, 'kaspa:testnet-10', 'kaspa-exact-v2', {lit(payer)}, "
        f"'m', {lit(treasury)}, 100000000, {100000000 - fee}, {fee}, 'settled', 'GET', '/p/x', {lit(nonce)}, "
        f"{lit(payment_identifier)}, {lit(request_hash)}"
    )
    return f"INSERT INTO settlements ({columns}) SELECT {values} FROM endpoints e LIMIT 1"  # noqa: S608


async def test_0005_exact_v2_constraints_and_guarded_downgrade(pg_server: PgServer) -> None:
    from sqlalchemy.exc import DBAPIError, IntegrityError

    url = pg_server.make_url("kasgate_exact_v2")
    pg_server.create_database("kasgate_exact_v2")
    config = alembic_config(url)
    await asyncio.to_thread(command.upgrade, config, "0004_k402_utxo")
    await _execute(
        url,
        "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
        "VALUES (gen_random_uuid(), 'a', 'm', repeat('a', 64))",
        "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_sompi, allowed_methods) "
        "SELECT gen_random_uuid(), id, 'weather', 'w', 'https://x.test', 100, '{GET}' FROM merchants LIMIT 1",
        _settlement_insert("a" * 64, "1" * 32),
    )
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION
    # the pre-existing v1 row is carried over unchanged
    assert await _scalar(url, "SELECT payment_scheme FROM settlements") == "kaspa-exact-v1"
    assert await _scalar(url, "SELECT payment_identifier IS NULL AND request_hash IS NULL FROM settlements")

    await _execute(url, _exact_v2_insert("b" * 64, "pay_0123456789abcdef"))
    assert await _scalar(url, "SELECT fee_amount_sompi FROM settlements WHERE tx_id = '" + "b" * 64 + "'") == 0
    for bad in (
        _exact_v2_insert("c" * 64, "pay_0123456789abcdef"),  # unique ix_settlements_payment_identifier
        _exact_v2_insert("b" * 64, "pay_fedcba9876543210"),  # unique tx_id, shared with every binding
        _exact_v2_insert("c" * 64, "short"),  # ck_settlements_payment_identifier_format
        _exact_v2_insert("c" * 64, "pay_0123456789abcde!"),  # ck_settlements_payment_identifier_format
        _exact_v2_insert("c" * 64, "pay_1111111111111111", "F" * 64),  # ck_settlements_request_hash_hex64
        _exact_v2_insert("c" * 64, None),  # ck_settlements_exact_v2_identifiers
        _exact_v2_insert("c" * 64, "pay_1111111111111111", None),  # ck_settlements_exact_v2_identifiers
        _exact_v2_insert("c" * 64, "pay_1111111111111111", fee=10),  # ck_settlements_exact_v2_shape
        _exact_v2_insert("c" * 64, "pay_1111111111111111", treasury="t"),  # ck_settlements_exact_v2_shape
        _exact_v2_insert("c" * 64, "pay_1111111111111111", nonce="2" * 32),  # ck_settlements_exact_v2_shape
        _exact_v2_insert("c" * 64, "pay_1111111111111111", payer=None),  # ck_settlements_exact_v2_shape
        # v1 rows keep their treasury split, nonce, and NO payment identifier
        "UPDATE settlements SET fee_amount_sompi = 0 WHERE tx_id = '" + "a" * 64 + "'",
        "UPDATE settlements SET treasury_address = NULL WHERE tx_id = '" + "a" * 64 + "'",
        "UPDATE settlements SET payment_nonce = NULL WHERE tx_id = '" + "a" * 64 + "'",
        "UPDATE settlements SET payment_identifier = 'pay_2222222222222222' WHERE tx_id = '" + "a" * 64 + "'",
        "UPDATE settlements SET payment_scheme = 'kaspa-exact-v3' WHERE tx_id = '" + "b" * 64 + "'",
    ):
        with pytest.raises(IntegrityError):
            await _execute(url, bad)

    # a downgrade would have to invent treasury outputs and nonces, so it refuses while kaspa-exact-v2 rows exist
    with pytest.raises(DBAPIError, match="kaspa-exact-v2 settlements exist"):
        await asyncio.to_thread(command.downgrade, config, "0004_k402_utxo")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION
    await _execute(url, "DELETE FROM settlements WHERE payment_scheme = 'kaspa-exact-v2'")
    await asyncio.to_thread(command.downgrade, config, "0004_k402_utxo")
    assert await _scalar(url, "SELECT count(*) FROM settlements") == 1
    assert (
        await _scalar(
            url,
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'settlements' "
            "AND column_name IN ('payment_identifier', 'request_hash')",
        )
        == 0
    )
    await asyncio.to_thread(command.upgrade, config, "head")
    migrated = create_async_engine(url, poolclass=NullPool)
    try:
        async with migrated.connect() as conn:
            assert await conn.run_sync(_diff) == []
    finally:
        await migrated.dispose()


_PK = "a" * 64


def _balance_insert(balance: int, deposited: int, debited: int, *, key: str = _PK) -> str:
    return (
        "INSERT INTO prepaid_balances (id, merchant_id, payer_address, payer_public_key, balance_sompi, "  # noqa: S608
        f"deposited_sompi, debited_sompi) SELECT gen_random_uuid(), id, 'p', '{key}', {balance}, {deposited}, "
        f"{debited} FROM merchants LIMIT 1"
    )


def _deposit_insert(tx_hex: str, deposit: int, fee: int, credit: int, *, settlement_tx: str | None = None) -> str:
    """One deposit row funded by the settlement whose tx_id is `settlement_tx` (default: `tx_hex`)."""
    source = tx_hex if settlement_tx is None else settlement_tx
    return (
        "INSERT INTO prepaid_deposits (id, balance_id, merchant_id, settlement_id, tx_id, payer_address, "  # noqa: S608
        "deposit_sompi, fee_sompi, credit_sompi) SELECT gen_random_uuid(), b.id, b.merchant_id, s.id, "
        f"'{tx_hex}', 'p', {deposit}, {fee}, {credit} FROM prepaid_balances b, settlements s "
        f"WHERE s.tx_id = '{source}' LIMIT 1"
    )


def _debit_insert(
    nonce: str, amount: int = 1000, *, status: str = "debited", refunded: bool = False, body_hash: str | None = None
) -> str:
    refunded_at = "now()" if refunded else "NULL"
    body = "NULL" if body_hash is None else f"'{body_hash}'"
    return (
        "INSERT INTO prepaid_debits (id, balance_id, merchant_id, endpoint_id, nonce, payer_address, amount_sompi, "  # noqa: S608
        "status, request_method, request_path, body_hash, refunded_at) SELECT gen_random_uuid(), b.id, "
        f"b.merchant_id, e.id, '{nonce}', 'p', {amount}, '{status}', 'GET', '/p/weather', {body}, {refunded_at} "
        "FROM prepaid_balances b, endpoints e LIMIT 1"
    )


async def test_0006_prepaid_constraints_and_guarded_downgrade(pg_server: PgServer) -> None:
    from sqlalchemy.exc import DBAPIError, IntegrityError

    url = pg_server.make_url("kasgate_prepaid")
    pg_server.create_database("kasgate_prepaid")
    config = alembic_config(url)
    await asyncio.to_thread(command.upgrade, config, "0005_exact_v2")
    await _execute(
        url,
        "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
        "VALUES (gen_random_uuid(), 'a', 'm', repeat('a', 64))",
        "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_sompi, allowed_methods) "
        "SELECT gen_random_uuid(), id, 'weather', 'w', 'https://x.test', 100, '{GET}' FROM merchants LIMIT 1",
        _settlement_insert("a" * 64, "1" * 32),
        _settlement_insert("b" * 64, "5" * 32),
    )
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION
    # existing endpoints are not prepaid-only (server default false)
    assert await _scalar(url, "SELECT prepaid_only FROM endpoints") is False

    await _execute(
        url,
        _balance_insert(70_000_000, 70_000_000, 0),
        _deposit_insert("a" * 64, 90_000_000, 20_000_000, 70_000_000),
        _debit_insert("2" * 32),
        _debit_insert("3" * 32, status="refunded", refunded=True, body_hash="e" * 64),
    )
    for bad in (
        _balance_insert(0, 0, 0),  # unique (merchant_id, payer_public_key)
        _balance_insert(-1, 0, 1, key="b" * 64),  # ck_prepaid_balances_balance_not_negative
        _balance_insert(5, 10, 4, key="b" * 64),  # ck_prepaid_balances_balance_matches_totals
        _balance_insert(1, 1, 0, key="B" * 64),  # ck_prepaid_balances_payer_public_key_hex64
        _balance_insert(1, 1, 0, key="b" * 63),  # ck_prepaid_balances_payer_public_key_hex64
        # ck_prepaid_balances_balance_not_negative: no overdraft even with consistent totals
        "UPDATE prepaid_balances SET balance_sompi = balance_sompi - 70000001, "
        "debited_sompi = debited_sompi + 70000001",
        _deposit_insert("a" * 64, 90_000_000, 20_000_000, 70_000_000, settlement_tx="b" * 64),  # unique tx_id
        _deposit_insert("c" * 64, 90_000_000, 20_000_000, 70_000_000, settlement_tx="a" * 64),  # unique settlement
        _deposit_insert("B" * 64, 90_000_000, 20_000_000, 70_000_000, settlement_tx="b" * 64),  # tx_id_hex64
        _deposit_insert("b" * 64, 90_000_000, 20_000_000, 60_000_000),  # split_sums_to_deposit
        _deposit_insert("b" * 64, 70_000_000, 0, 70_000_000),  # amounts_positive
        _debit_insert("2" * 32),  # unique ix_prepaid_debits_nonce
        _debit_insert("4" * 31),  # ck_prepaid_debits_nonce_hex32
        _debit_insert("4" * 32, 0),  # ck_prepaid_debits_amount_positive
        _debit_insert("4" * 32, status="refunded"),  # ck_prepaid_debits_refunded_at_iff_refunded
        _debit_insert("4" * 32, refunded=True),  # ck_prepaid_debits_refunded_at_iff_refunded
        _debit_insert("4" * 32, status="settled"),  # ck_prepaid_debits_status_known
        _debit_insert("4" * 32, body_hash="xyz"),  # ck_prepaid_debits_body_hash_hex64
        "DELETE FROM settlements",  # fk RESTRICT: a deposit's settlement cannot disappear
        "DELETE FROM prepaid_balances",  # fk RESTRICT: deposits and debits keep their balance
    ):
        with pytest.raises(IntegrityError):
            await _execute(url, bad)

    # a downgrade would drop buyer credit the merchant was already paid for, so it refuses
    with pytest.raises(DBAPIError, match="prepaid balances, deposits, debits or prepaid-only endpoints exist"):
        await asyncio.to_thread(command.downgrade, config, "0005_exact_v2")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION
    await _execute(url, "DELETE FROM prepaid_debits", "DELETE FROM prepaid_deposits", "DELETE FROM prepaid_balances")
    await _execute(url, "UPDATE endpoints SET prepaid_only = true")
    with pytest.raises(DBAPIError, match="prepaid-only endpoints exist"):
        await asyncio.to_thread(command.downgrade, config, "0005_exact_v2")
    await _execute(url, "UPDATE endpoints SET prepaid_only = false")
    await asyncio.to_thread(command.downgrade, config, "0005_exact_v2")
    assert await _scalar(url, "SELECT count(*) FROM settlements") == 2
    assert (
        await _scalar(
            url,
            "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'prepaid_%'",
        )
        == 0
    )
    await asyncio.to_thread(command.upgrade, config, "head")
    migrated = create_async_engine(url, poolclass=NullPool)
    try:
        async with migrated.connect() as conn:
            assert await conn.run_sync(_diff) == []
    finally:
        await migrated.dispose()


async def test_0007_trials_webhooks_constraints_and_guarded_downgrade(pg_server: PgServer) -> None:
    from sqlalchemy.exc import DBAPIError, IntegrityError

    url = pg_server.make_url("kasgate_trials")
    pg_server.create_database("kasgate_trials")
    config = alembic_config(url)
    await asyncio.to_thread(command.upgrade, config, "0006_prepaid")
    await _execute(
        url,
        "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
        "VALUES (gen_random_uuid(), 'a', 'm', repeat('a', 64))",
        "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_sompi, allowed_methods) "
        "SELECT gen_random_uuid(), id, 'weather', 'w', 'https://x.test', 100000000, '{GET}' FROM merchants LIMIT 1",
    )
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION
    # existing endpoints get no trial and the default daily cap
    assert await _scalar(url, "SELECT free_trial_calls FROM endpoints") == 0
    assert await _scalar(url, "SELECT trial_daily_cap FROM endpoints") == 100

    usage = (
        "INSERT INTO trial_usages (id, endpoint_id, merchant_id, payer_address, payer_public_key, calls_used) "
        "SELECT gen_random_uuid(), e.id, e.merchant_id, 'p', '{key}', {used} FROM endpoints e LIMIT 1"
    )
    call = (
        "INSERT INTO trial_calls (id, usage_id, endpoint_id, merchant_id, nonce, payer_address, status, "
        "request_method, request_path, body_hash, refunded_at) SELECT gen_random_uuid(), u.id, u.endpoint_id, "
        "u.merchant_id, '{nonce}', 'p', '{status}', 'GET', '/p/weather', {body}, {refunded} FROM trial_usages u LIMIT 1"
    )
    webhook = (
        "INSERT INTO webhooks (id, merchant_id, url, event_types, secret_encrypted, secret_prefix) "
        "SELECT gen_random_uuid(), id, '{url}', '{types}', 'x'::bytea, 'whsec_abcd' FROM merchants LIMIT 1"
    )
    event = (
        "INSERT INTO webhook_events (id, event_id, merchant_id, event_type, payload) "
        "SELECT gen_random_uuid(), '{event_id}', id, '{event_type}', '{{}}'::jsonb FROM merchants LIMIT 1"
    )
    delivery = (
        "INSERT INTO webhook_deliveries (id, event_pk, webhook_id, merchant_id, status, attempts, delivered_at) "
        "SELECT gen_random_uuid(), e.id, w.id, w.merchant_id, '{status}', {attempts}, {delivered} "
        "FROM webhook_events e, webhooks w LIMIT 1"
    )
    await _execute(
        url,
        "UPDATE endpoints SET free_trial_calls = 3",
        usage.format(key="a" * 64, used=1),
        call.format(nonce="1" * 32, status="used", body="NULL", refunded="NULL"),
        call.format(nonce="2" * 32, status="refunded", body=f"'{'e' * 64}'", refunded="now()"),
        "INSERT INTO trial_daily_counts (endpoint_id, day, calls) SELECT id, current_date, 2 FROM endpoints",
        webhook.format(url="https://hooks.example.com/k", types="{payment.settled}"),
        event.format(event_id="evt_" + "a" * 32, event_type="payment.settled"),
        delivery.format(status="pending", attempts=0, delivered="NULL"),
    )
    for bad in (
        "UPDATE endpoints SET free_trial_calls = 101",  # ck_endpoints_free_trial_calls_range
        "UPDATE endpoints SET free_trial_calls = -1",  # ck_endpoints_free_trial_calls_range
        "UPDATE endpoints SET trial_daily_cap = 0",  # ck_endpoints_trial_daily_cap_range
        usage.format(key="a" * 64, used=0),  # unique (endpoint_id, payer_public_key)
        usage.format(key="b" * 64, used=-1),  # ck_trial_usages_calls_used_not_negative
        usage.format(key="B" * 64, used=0),  # ck_trial_usages_payer_public_key_hex64
        call.format(nonce="1" * 32, status="used", body="NULL", refunded="NULL"),  # unique nonce
        call.format(nonce="3" * 31, status="used", body="NULL", refunded="NULL"),  # nonce_hex32
        call.format(nonce="3" * 32, status="refunded", body="NULL", refunded="NULL"),  # refunded_at iff refunded
        call.format(nonce="3" * 32, status="settled", body="NULL", refunded="NULL"),  # status_known
        call.format(nonce="3" * 32, status="used", body="'xyz'", refunded="NULL"),  # body_hash_hex64
        "INSERT INTO trial_daily_counts (endpoint_id, day, calls) SELECT id, current_date, 1 FROM endpoints",  # pk
        "UPDATE trial_daily_counts SET calls = -1",  # ck_trial_daily_counts_calls_not_negative
        webhook.format(url="http://hooks.example.com/k", types="{payment.settled}"),  # ck_webhooks_url_https
        webhook.format(url="https://hooks.example.com/k", types="{}"),  # ck_webhooks_event_types_not_empty
        event.format(event_id="evt_" + "a" * 32, event_type="payment.settled"),  # unique event_id
        event.format(event_id="evt_" + "A" * 32, event_type="payment.settled"),  # event_id_format
        event.format(event_id="evt_" + "b" * 32, event_type="payment.refunded"),  # event_type_known
        delivery.format(status="pending", attempts=0, delivered="NULL"),  # unique (event_pk, webhook_id)
        "UPDATE webhook_deliveries SET status = 'lost'",  # ck_webhook_deliveries_status_known
        "UPDATE webhook_deliveries SET attempts = -1",  # ck_webhook_deliveries_attempts_not_negative
        "UPDATE webhook_deliveries SET status = 'succeeded'",  # delivered_at iff succeeded
    ):
        with pytest.raises(IntegrityError):
            await _execute(url, bad)

    with pytest.raises(DBAPIError, match="trial calls, trial-enabled endpoints or webhooks exist"):
        await asyncio.to_thread(command.downgrade, config, "0006_prepaid")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION
    await _execute(url, "DELETE FROM webhooks", "DELETE FROM trial_calls")
    with pytest.raises(DBAPIError, match="trial-enabled endpoints"):
        await asyncio.to_thread(command.downgrade, config, "0006_prepaid")
    await _execute(url, "UPDATE endpoints SET free_trial_calls = 0")
    await asyncio.to_thread(command.downgrade, config, "0006_prepaid")
    assert (
        await _scalar(
            url,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name LIKE 'trial_%' OR table_name LIKE 'webhook%'",
        )
        == 0
    )
    await asyncio.to_thread(command.upgrade, config, "head")
    migrated = create_async_engine(url, poolclass=NullPool)
    try:
        async with migrated.connect() as conn:
            assert await conn.run_sync(_diff) == []
    finally:
        await migrated.dispose()


async def test_0008_refund_expiry_constraints_and_guarded_downgrade(pg_server: PgServer) -> None:
    from sqlalchemy.exc import DBAPIError, IntegrityError

    url = pg_server.make_url("kasgate_refunds")
    pg_server.create_database("kasgate_refunds")
    config = alembic_config(url)
    await asyncio.to_thread(command.upgrade, config, "0007_trials_webhooks")
    await _execute(
        url,
        "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
        "VALUES (gen_random_uuid(), 'a', 'm', repeat('a', 64))",
        "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_sompi, allowed_methods) "
        "SELECT gen_random_uuid(), id, 'weather', 'w', 'https://x.test', 100000000, '{GET}' FROM merchants LIMIT 1",
        "INSERT INTO prepaid_balances (id, merchant_id, payer_address, payer_public_key, balance_sompi, "
        "deposited_sompi, debited_sompi) SELECT gen_random_uuid(), id, 'p', repeat('a', 64), 70, 100, 30 "
        "FROM merchants LIMIT 1",
    )
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION
    # Existing balances carry over: nothing refunded, expired or held, and no expiry (never retroactive).
    assert (
        await _scalar(
            url,
            "SELECT refunded_sompi + expired_sompi + refund_hold_sompi FROM prepaid_balances WHERE expires_at IS NULL",
        )
        == 0
    )
    assert await _scalar(url, "SELECT count(*) FROM merchants WHERE prepaid_expiry_days IS NULL") == 1

    refund = (
        "INSERT INTO prepaid_refunds (id, balance_id, merchant_id, payer_address, payer_public_key, amount_sompi, "
        "status, request_nonce, tx_id, payout_submitted_at, paid_at, resolved_at) SELECT gen_random_uuid(), b.id, "
        "b.merchant_id, 'p', '{key}', {amount}, '{status}', '{nonce}', {tx}, {submitted}, {paid}, {resolved} "
        "FROM prepaid_balances b LIMIT 1"
    )
    entry = (
        "INSERT INTO prepaid_ledger_entries (id, balance_id, merchant_id, payer_address, kind, direction, "
        "amount_sompi, balance_after_sompi, tx_id, refund_id) SELECT gen_random_uuid(), b.id, b.merchant_id, 'p', "
        "'{kind}', '{direction}', {amount}, {after}, {tx}, {refund} FROM prepaid_balances b LIMIT 1"
    )
    paid_refund = "(SELECT id FROM prepaid_refunds WHERE status = 'paid')"
    tx = f"'{'c' * 64}'"
    await _execute(
        url,
        "UPDATE merchants SET prepaid_expiry_days = 30",
        "UPDATE endpoints SET prepaid_expiry_days = 7",
        "UPDATE prepaid_balances SET refund_hold_sompi = 20, expires_at = now() + interval '1 day'",
        refund.format(
            key="a" * 64,
            amount=20,
            status="requested",
            nonce="1" * 32,
            tx="NULL",
            submitted="NULL",
            paid="NULL",
            resolved="NULL",
        ),
        refund.format(
            key="a" * 64,
            amount=10,
            status="paid",
            nonce="2" * 32,
            tx=tx,
            submitted="NULL",
            paid="now()",
            resolved="now()",
        ),
        "UPDATE prepaid_balances SET refunded_sompi = 10, balance_sompi = 60",
        entry.format(kind="refund", direction="debit", amount=10, after=60, tx=tx, refund=paid_refund),
        entry.format(kind="expiry", direction="debit", amount=5, after=55, tx="NULL", refund="NULL"),
    )

    def requested(**overrides: object) -> str:
        values: dict[str, object] = {
            "key": "a" * 64,
            "amount": 5,
            "status": "rejected",
            "nonce": "3" * 32,
            "tx": "NULL",
            "submitted": "NULL",
            "paid": "NULL",
            "resolved": "now()",
        }
        values.update(overrides)
        return refund.format(**values)

    for bad in (
        "UPDATE merchants SET prepaid_expiry_days = 0",  # ck_merchants_prepaid_expiry_days_range
        "UPDATE endpoints SET prepaid_expiry_days = 3651",  # ck_endpoints_prepaid_expiry_days_range
        "UPDATE prepaid_balances SET refund_hold_sompi = 61",  # ck_prepaid_balances_refund_hold_within_balance
        "UPDATE prepaid_balances SET refunded_sompi = 11",  # ck_prepaid_balances_balance_matches_totals
        "UPDATE prepaid_balances SET expired_sompi = -1",  # ck_prepaid_balances_totals_not_negative
        requested(status="requested", resolved="NULL"),  # ix_prepaid_refunds_open_balance_id: one open refund
        requested(nonce="1" * 32),  # ix_prepaid_refunds_request_nonce
        requested(status="paid", tx=tx, paid="now()"),  # ix_prepaid_refunds_tx_id
        requested(amount=0),  # ck_prepaid_refunds_amount_positive
        requested(status="lost"),  # ck_prepaid_refunds_status_known
        requested(nonce="3" * 31),  # ck_prepaid_refunds_request_nonce_hex32
        requested(key="A" * 64),  # ck_prepaid_refunds_payer_public_key_hex64
        requested(status="paid", paid="now()"),  # ck_prepaid_refunds_tx_id_iff_payout
        requested(status="paid", tx=f"'{'d' * 64}'"),  # ck_prepaid_refunds_paid_at_iff_paid
        requested(status="payout_submitted", tx=f"'{'d' * 64}'", resolved="NULL"),  # submitted_at iff submitted
        requested(resolved="NULL"),  # ck_prepaid_refunds_resolved_at_iff_closed
        requested(tx="'xyz'"),  # ck_prepaid_refunds_tx_id_hex64
        entry.format(kind="refund", direction="debit", amount=10, after=60, tx=tx, refund=paid_refund),  # unique
        entry.format(kind="refund", direction="debit", amount=1, after=1, tx="NULL", refund="NULL"),  # carry payout
        entry.format(kind="expiry", direction="debit", amount=0, after=1, tx="NULL", refund="NULL"),  # positive
        entry.format(kind="expiry", direction="debit", amount=1, after=-1, tx="NULL", refund="NULL"),  # after >= 0
        entry.format(kind="gift", direction="debit", amount=1, after=1, tx="NULL", refund="NULL"),  # kind_known
        entry.format(kind="expiry", direction="up", amount=1, after=1, tx="NULL", refund="NULL"),  # direction
        "INSERT INTO webhook_events (id, event_id, merchant_id, event_type, payload) SELECT gen_random_uuid(), "
        "'evt_' || repeat('f', 32), id, 'prepaid.refund.lost', '{}'::jsonb FROM merchants LIMIT 1",
    ):
        with pytest.raises(IntegrityError):
            await _execute(url, bad)
    await _execute(
        url,
        "INSERT INTO webhook_events (id, event_id, merchant_id, event_type, payload) SELECT gen_random_uuid(), "
        "'evt_' || repeat('e', 32), id, 'prepaid.refund.paid', '{}'::jsonb FROM merchants LIMIT 1",
    )

    with pytest.raises(DBAPIError, match="prepaid refunds, expiries or expiry policies exist"):
        await asyncio.to_thread(command.downgrade, config, "0007_trials_webhooks")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION
    await _execute(
        url,
        "DELETE FROM prepaid_ledger_entries",
        "DELETE FROM prepaid_refunds",
        "DELETE FROM webhook_events",
        "UPDATE prepaid_balances SET refund_hold_sompi = 0, refunded_sompi = 0, balance_sompi = 70, expires_at = NULL",
    )
    with pytest.raises(DBAPIError, match="expiry policies"):
        await asyncio.to_thread(command.downgrade, config, "0007_trials_webhooks")
    await _execute(
        url, "UPDATE merchants SET prepaid_expiry_days = NULL", "UPDATE endpoints SET prepaid_expiry_days = NULL"
    )
    await asyncio.to_thread(command.downgrade, config, "0007_trials_webhooks")
    assert await _scalar(url, "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'prepaid_re%'") == 0
    assert await _scalar(url, "SELECT balance_sompi FROM prepaid_balances") == 70
    await asyncio.to_thread(command.upgrade, config, "head")
    migrated = create_async_engine(url, poolclass=NullPool)
    try:
        async with migrated.connect() as conn:
            assert await conn.run_sync(_diff) == []
    finally:
        await migrated.dispose()


async def test_0009_failed_delivery_credit_constraints_and_guarded_downgrade(pg_server: PgServer) -> None:
    from sqlalchemy.exc import DBAPIError, IntegrityError

    url = pg_server.make_url("kasgate_credits")
    pg_server.create_database("kasgate_credits")
    config = alembic_config(url)
    await asyncio.to_thread(command.upgrade, config, "0008_prepaid_refunds")
    await _execute(
        url,
        "INSERT INTO merchants (id, kaspa_address, display_name, api_key_hash) "
        "VALUES (gen_random_uuid(), 'a', 'm', repeat('a', 64))",
        "INSERT INTO endpoints (id, merchant_id, slug, name, upstream_url, price_sompi, allowed_methods) "
        "SELECT gen_random_uuid(), id, 'weather', 'w', 'https://x.test', 100000000, '{GET}' FROM merchants LIMIT 1",
        _settlement_insert("a" * 64, "1" * 32),
        _settlement_insert("b" * 64, "2" * 32),
        "INSERT INTO prepaid_balances (id, merchant_id, payer_address, payer_public_key, balance_sompi, "
        "deposited_sompi, debited_sompi, refunded_sompi, expired_sompi) SELECT gen_random_uuid(), id, 'p', "
        "repeat('a', 64), 60, 100, 30, 6, 4 FROM merchants LIMIT 1",
    )
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION
    # existing balances carry over with nothing credited; existing merchants are opted in from the upgrade on
    assert await _scalar(url, "SELECT credited_sompi FROM prepaid_balances") == 0
    assert (
        await _scalar(
            url,
            "SELECT count(*) FROM merchants WHERE failed_delivery_credit_enabled_at <= now() "
            "AND failed_delivery_credit_disabled_at IS NULL",
        )
        == 1
    )

    tx_a, tx_b = "'" + "a" * 64 + "'", "'" + "b" * 64 + "'"
    settlement_a = "(SELECT id FROM settlements WHERE tx_id = " + tx_a + ")"  # noqa: S608
    settlement_b = "(SELECT id FROM settlements WHERE tx_id = " + tx_b + ")"  # noqa: S608
    credit = (
        "INSERT INTO prepaid_ledger_entries (id, balance_id, merchant_id, payer_address, kind, direction, "
        "amount_sompi, balance_after_sompi, tx_id, settlement_id, slug, credit_reason) SELECT gen_random_uuid(), "
        "b.id, b.merchant_id, 'p', '{kind}', '{direction}', 90, 150, {tx}, {settlement}, {slug}, {reason} "
        "FROM prepaid_balances b LIMIT 1"
    )

    def row(**overrides: object) -> str:
        values: dict[str, object] = {
            "kind": "failed_delivery_credit",
            "direction": "credit",
            "tx": tx_b,
            "settlement": settlement_b,
            "slug": "'weather'",
            "reason": "'window_expired'",
        }
        values.update(overrides)
        return credit.format(**values)

    await _execute(
        url,
        "UPDATE prepaid_balances SET credited_sompi = 90, balance_sompi = 150",
        row(tx=tx_a, settlement=settlement_a),
    )
    for bad in (
        "UPDATE prepaid_balances SET credited_sompi = 91",  # ck_prepaid_balances_balance_matches_totals
        "UPDATE prepaid_balances SET credited_sompi = -1, balance_sompi = 59",  # credited_not_negative
        row(tx=tx_a),  # ix_prepaid_ledger_entries_credit_tx_id: one credit per tx
        row(settlement=settlement_a),  # one credit per settlement
        row(settlement="NULL"),  # ck_prepaid_ledger_entries_credit_rows_carry_settlement
        row(slug="NULL"),  # ck_prepaid_ledger_entries_credit_rows_carry_settlement
        row(reason="NULL"),  # ck_prepaid_ledger_entries_credit_rows_carry_settlement
        row(tx="NULL"),  # ck_prepaid_ledger_entries_credit_rows_carry_settlement
        row(direction="debit"),  # ck_prepaid_ledger_entries_credit_rows_carry_settlement
        row(reason="'lost'"),  # ck_prepaid_ledger_entries_credit_reason_known
        row(kind="expiry", direction="debit", tx="NULL"),  # expiry rows carry no settlement
        row(kind="gift"),  # ck_prepaid_ledger_entries_kind_known
        "DELETE FROM settlements WHERE tx_id = " + tx_a,  # noqa: S608 (fk RESTRICT)
        "INSERT INTO webhook_events (id, event_id, merchant_id, event_type, payload) SELECT gen_random_uuid(), "
        "'evt_' || repeat('f', 32), id, 'prepaid.credit.lost', '{}'::jsonb FROM merchants LIMIT 1",
    ):
        with pytest.raises(IntegrityError):
            await _execute(url, bad)
    # a non-credit row may still reuse a credited tx_id (the unique index is partial)
    await _execute(
        url,
        "INSERT INTO webhook_events (id, event_id, merchant_id, event_type, payload) SELECT gen_random_uuid(), "
        "'evt_' || repeat('e', 32), id, 'prepaid.credit.failed_delivery', '{}'::jsonb FROM merchants LIMIT 1",
    )

    with pytest.raises(DBAPIError, match="failed-delivery credits, credited balances or credit webhooks exist"):
        await asyncio.to_thread(command.downgrade, config, "0008_prepaid_refunds")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == HEAD_REVISION
    await _execute(url, "DELETE FROM prepaid_ledger_entries")
    with pytest.raises(DBAPIError, match="credited balances"):
        await asyncio.to_thread(command.downgrade, config, "0008_prepaid_refunds")
    await _execute(url, "UPDATE prepaid_balances SET credited_sompi = 0, balance_sompi = 60")
    with pytest.raises(DBAPIError, match="credit webhooks"):
        await asyncio.to_thread(command.downgrade, config, "0008_prepaid_refunds")
    await _execute(url, "DELETE FROM webhook_events")
    await asyncio.to_thread(command.downgrade, config, "0008_prepaid_refunds")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == "0008_prepaid_refunds"
    assert await _scalar(url, "SELECT balance_sompi FROM prepaid_balances") == 60
    assert (
        await _scalar(
            url,
            "SELECT count(*) FROM information_schema.columns WHERE column_name IN "
            "('credited_sompi', 'credit_reason', 'failed_delivery_credit_enabled_at')",
        )
        == 0
    )
    with pytest.raises(IntegrityError):  # 0008's identity is back
        await _execute(url, "UPDATE prepaid_balances SET balance_sompi = 61")
    await asyncio.to_thread(command.upgrade, config, "head")
    migrated = create_async_engine(url, poolclass=NullPool)
    try:
        async with migrated.connect() as conn:
            assert await conn.run_sync(_diff) == []
    finally:
        await migrated.dispose()
