"""FastAPI dependency providers for shared application resources."""

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, Protocol

import httpx
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import SessionLocal
from app.schemas import PaymentRequired, PaymentSignature


class VerifiedPaymentLike(Protocol):
    @property
    def tx_id(self) -> str: ...

    @property
    def payer_address(self) -> str: ...

    @property
    def total_sompi(self) -> int: ...

    @property
    def merchant_sompi(self) -> int: ...

    @property
    def fee_sompi(self) -> int: ...


class PaymentVerifier(Protocol):
    """Structural type of `app.kaspa_engine.KaspaEngine` as used by the proxy.

    `verify` is pure; `resolve_inputs` and `settle` talk to the node. `tx` is the engine's own parsed transaction,
    handed back to it untouched.
    """

    def verify(self, signature: PaymentSignature, required: PaymentRequired) -> tuple[VerifiedPaymentLike, Any]: ...

    async def resolve_inputs(self, signature: PaymentSignature, tx: Any) -> object: ...

    async def settle(self, tx: Any, verified: Any, required: PaymentRequired) -> VerifiedPaymentLike: ...


class RateProvider(Protocol):
    """Structural type of `app.oracle.KasUsdOracle` as used by the proxy."""

    async def get_rate(self) -> Decimal: ...


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return SessionLocal


async def get_db_session(
    maker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> AsyncIterator[AsyncSession]:
    async with maker() as session:
        yield session


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


def get_http_client(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.http_client
    return client


def get_engine(request: Request) -> PaymentVerifier:
    engine: PaymentVerifier = request.app.state.engine
    return engine


def get_oracle(request: Request) -> RateProvider:
    oracle: RateProvider = request.app.state.oracle
    return oracle
