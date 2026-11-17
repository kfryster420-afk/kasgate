"""Shared fixtures: real PostgreSQL (pgserver or TEST_DATABASE_URL), fakeredis, stub engine/oracle, mock upstream."""

import asyncio
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from kaspa import PrivateKey

TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()
TREASURY = "kaspatest:qzxwgn9qu5mxn379rkp54zs729hrsr5v6045eu8hpze0x8r8caq57w49l62en"
MERCHANT_KEY = PrivateKey("01" * 32)
PAYER_KEY = PrivateKey("03" * 32)
OTHER_KEY = PrivateKey("05" * 32)
MERCHANT_ADDRESS = MERCHANT_KEY.to_address("testnet").to_string()
PAYER_ADDRESS = PAYER_KEY.to_address("testnet").to_string()
OTHER_ADDRESS = OTHER_KEY.to_address("testnet").to_string()
PRICE_SOMPI = 2_000_000_000  # 20 KAS: fee 30_000_000, merchant 1_970_000_000

os.environ.update(
    {
        "KASPA_NETWORK": "kaspa:testnet-10",
        "KASGATE_TREASURY_ADDRESS": TREASURY,
        "ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,
        "REDIS_URL": "redis://localhost:6379/15",
        "DATABASE_URL": "postgresql+asyncpg://postgres@/unused?host=/nonexistent",
        "PUBLIC_BASE_URL": "http://gateway.test",
        "CORS_ORIGINS": '["http://localhost:3000"]',
    }
)

import fakeredis  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.deps import get_sessionmaker  # noqa: E402
from app.kaspa_engine import PaymentVerificationError, VerifiedPayment  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Endpoint, Merchant  # noqa: E402
from app.schemas import PaymentRequired, PaymentSignature  # noqa: E402
from app.security import encrypt_headers, generate_api_key, hash_api_key  # noqa: E402

GATEWAY_DIR = Path(__file__).resolve().parent.parent

UPSTREAM_SECRET = "Bearer upstream-secret-token"


def alembic_config(url: str) -> Config:
    config = Config(str(GATEWAY_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


# ---------------------------------------------------------------- database


@dataclass(frozen=True)
class PgServer:
    admin_url: str
    make_url: Callable[[str], str]
    create_database: Callable[[str], None]


@pytest.fixture(scope="session")
def pg_server() -> Iterator[PgServer]:
    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        base = external.rsplit("/", 1)[0]

        def make_external(name: str) -> str:
            return f"{base}/{name}"

        def create_external(name: str) -> None:
            import psycopg2  # type: ignore[import-untyped]

            dsn = external.replace("+asyncpg", "")
            with psycopg2.connect(dsn) as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
                    cur.execute(f'CREATE DATABASE "{name}"')

        yield PgServer(admin_url=external, make_url=make_external, create_database=create_external)
        return
    try:
        import pgserver
    except ImportError:
        pytest.skip("install pgserver or set TEST_DATABASE_URL to run PostgreSQL-backed tests")
    directory = tempfile.mkdtemp(prefix="kgpg")
    server = pgserver.get_server(  # type: ignore[attr-defined]
        directory, cleanup_mode="stop"
    )

    def make_url(name: str) -> str:
        return f"postgresql+asyncpg://postgres@/{name}?host={directory}"

    def create_database(name: str) -> None:
        server.psql(f'DROP DATABASE IF EXISTS "{name}"')
        server.psql(f'CREATE DATABASE "{name}"')

    yield PgServer(admin_url=make_url("postgres"), make_url=make_url, create_database=create_database)
    server.cleanup()
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session")
def database_url(pg_server: PgServer) -> str:
    pg_server.create_database("kasgate_test")
    url = pg_server.make_url("kasgate_test")
    command.upgrade(alembic_config(url), "head")
    return url


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def db_engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
def sessionmaker(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def clean_database(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    yield
    if "sessionmaker" in request.fixturenames:
        maker: async_sessionmaker[AsyncSession] = request.getfixturevalue("sessionmaker")
        async with maker() as session:
            await session.execute(text("TRUNCATE settlements, endpoints, merchants CASCADE"))
            await session.commit()


# ---------------------------------------------------------------- doubles


class StubEngine:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None
        self.delay: float = 0.0

    async def verify_and_submit(self, signature: PaymentSignature, required: PaymentRequired) -> VerifiedPayment:
        self.calls.append(signature.txId)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return VerifiedPayment(
            tx_id=signature.txId,
            payer_address=signature.payerAddress,
            total_sompi=int(required.amount),
            merchant_sompi=required.merchant_amount_sompi,
            fee_sompi=required.fee_amount_sompi,
        )

    def fail(self, code: str, message: str = "rejected") -> None:
        self.error = PaymentVerificationError(code, message)


class StubOracle:
    def __init__(self) -> None:
        self.rate: Decimal | None = Decimal("0.12500000")

    async def get_rate(self) -> Decimal:
        if self.rate is None:
            raise RuntimeError("oracle down")
        return self.rate


@dataclass
class Upstream:
    requests: list[httpx.Request] = field(default_factory=list)
    handler: Callable[[httpx.Request], httpx.Response] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        return httpx.Response(
            200,
            json={"ok": True},
            headers={"X-Upstream": "yes", "Set-Cookie": "session=abc", "PAYMENT-RESPONSE": "spoofed"},
        )


@pytest.fixture
def stub_engine() -> StubEngine:
    return StubEngine()


@pytest.fixture
def stub_oracle() -> StubOracle:
    return StubOracle()


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest_asyncio.fixture(loop_scope="session")
async def redis() -> AsyncIterator[Any]:
    client = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture(loop_scope="session")
async def app(
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Any,
    stub_engine: StubEngine,
    stub_oracle: StubOracle,
    upstream: Upstream,
) -> AsyncIterator[FastAPI]:
    application = create_app()
    application.dependency_overrides[get_sessionmaker] = lambda: sessionmaker
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), follow_redirects=False)
    application.state.redis = redis
    application.state.http_client = http_client
    application.state.engine = stub_engine
    application.state.oracle = stub_oracle
    yield application
    await http_client.aclose()


@pytest_asyncio.fixture(loop_scope="session")
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as c:
        yield c


def make_asgi_client(client: httpx.AsyncClient, client_ip: str) -> httpx.AsyncClient:
    """A second client for the same ASGI app that appears to connect from `client_ip`."""
    transport = client._transport
    assert isinstance(transport, httpx.ASGITransport)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=transport.app, client=(client_ip, 4242)),
        base_url="http://gateway.test",
    )


# ---------------------------------------------------------------- seed data


@dataclass(frozen=True)
class Seed:
    merchant_id: uuid.UUID
    endpoint_id: uuid.UUID
    slug: str
    api_key: str


async def seed_endpoint(
    maker: async_sessionmaker[AsyncSession],
    *,
    slug: str = "weather",
    methods: list[str] | None = None,
    upstream_url: str = "https://api.upstream.test/v1",
    address: str = MERCHANT_ADDRESS,
    active: bool = True,
    headers: dict[str, str] | None = None,
) -> Seed:
    api_key = generate_api_key()
    merchant = Merchant(
        kaspa_address=address,
        display_name="Acme",
        email=None,
        api_key_hash=hash_api_key(api_key),
    )
    async with maker() as session:
        session.add(merchant)
        await session.flush()
        endpoint = Endpoint(
            merchant_id=merchant.id,
            slug=slug,
            name="Weather API",
            upstream_url=upstream_url,
            upstream_headers_encrypted=encrypt_headers(
                headers if headers is not None else {"Authorization": UPSTREAM_SECRET}
            ),
            price_sompi=PRICE_SOMPI,
            allowed_methods=methods or ["GET", "POST"],
            is_active=active,
        )
        session.add(endpoint)
        await session.commit()
        return Seed(merchant_id=merchant.id, endpoint_id=endpoint.id, slug=slug, api_key=api_key)


@pytest_asyncio.fixture(loop_scope="session")
async def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> Seed:
    return await seed_endpoint(sessionmaker)


def make_signature_header(tx_id: str | None = None) -> str:
    return PaymentSignature(
        txId=tx_id or uuid.uuid4().hex + uuid.uuid4().hex,
        rawTx="{}",
        payerAddress=PAYER_ADDRESS,
    ).to_header()
