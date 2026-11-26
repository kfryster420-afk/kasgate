"""FastAPI dependency providers for shared application resources."""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import SessionLocal
from app.exact_v2 import ExactV2Verification
from app.payments import PayoutSighting
from app.schemas import K402UtxoOffer, PaymentRequired, PaymentSignature
from app.x402_v2 import ExactV2PaymentPayload, ExactV2Requirements


class VerifiedPaymentLike(Protocol):
    @property
    def tx_id(self) -> str: ...

    @property
    def payer_address(self) -> str | None: ...

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

    async def verify_k402(self, tx_id: str, offer: K402UtxoOffer) -> VerifiedPaymentLike: ...
    # CONTRACT v2.10, community kaspa-x402 `exact` / `kaspa-exact-v2` standard-native
    def verify_exact_v2(
        self,
        payment: ExactV2PaymentPayload,
        requirement: ExactV2Requirements,
        *,
        request_hash: str,
        now: datetime | None = None,
        check_expiry: bool = True,
    ) -> ExactV2Verification: ...

    async def resolve_exact_v2_inputs(self, verification: ExactV2Verification) -> None: ...

    async def settle_exact_v2(self, verification: ExactV2Verification) -> VerifiedPaymentLike: ...

    # CONTRACT v2.13 prepaid refund payouts (merchant -> buyer, no platform fee)
    async def verify_payout(
        self, tx_id: str, to_address: str, amount_sompi: int, refund_id: uuid.UUID, *, payload_checked: bool = False
    ) -> None: ...

    def payout_tx_id(self, transaction: str, payload: bytes, to_address: str, amount_sompi: int) -> str: ...

    async def virtual_daa_score(self) -> int: ...

    async def find_refund_payouts(
        self, to_address: str, amount_sompi: int, refund_id: uuid.UUID, *, min_daa_score: int = 0
    ) -> list[PayoutSighting]: ...

    async def submit_payout(
        self, transaction: str, payload: bytes, to_address: str, amount_sompi: int
    ) -> tuple[str, bool]: ...


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
