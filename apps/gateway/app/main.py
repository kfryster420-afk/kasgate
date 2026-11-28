"""Application entrypoint: lifespan wiring, CORS, health check, and routers."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api_routes import router as api_router
from app.api_routes import well_known_router
from app.config import get_settings
from app.db import engine as db_engine
from app.debit_sweeper import DebitSweeper
from app.deps import get_sessionmaker
from app.failed_delivery import router as failed_delivery_router
from app.failed_delivery import sweep_pass as failed_delivery_sweep_pass
from app.faucet import router as faucet_router
from app.kaspa_engine import engine as kaspa_engine
from app.ledger import LedgerWorker
from app.oracle import KasUsdOracle
from app.prepaid import router as prepaid_router
from app.proxy import router as proxy_router
from app.refunds import router as refunds_router
from app.schema_guard import check_database_schema
from app.schemas import (
    HEADER_FAILED_DELIVERY_CREDIT,
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    HEADER_PREPAID_RECEIPT,
    HEADER_TRIAL_LIMIT,
    HEADER_TRIAL_OFFER,
    HEADER_TRIAL_RECEIPT,
    HEADER_TRIAL_REMAINING,
    HealthStatus,
)
from app.security import RateLimited, build_upstream_client, build_webhook_client
from app.trials import router as trials_router
from app.webhooks import WebhookDispatcher
from app.webhooks import router as webhooks_router

logger = logging.getLogger("kasgate.main")

APP_VERSION = "1.0.0"

__all__ = ["app", "create_app", "kaspa_engine"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await check_database_schema(db_engine)
    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    http_client = build_upstream_client()
    app.state.redis = redis
    app.state.http_client = http_client
    app.state.oracle = KasUsdOracle(settings, redis, http_client=http_client)
    app.state.engine = kaspa_engine
    webhook_client = build_webhook_client(settings.webhook_timeout_seconds)
    app.state.webhook_client = webhook_client
    dispatcher = WebhookDispatcher(get_sessionmaker(), webhook_client)
    app.state.webhook_dispatcher = dispatcher
    if settings.webhook_dispatcher_enabled:
        dispatcher.start()
    ledger_worker = LedgerWorker(get_sessionmaker(), kaspa_engine, extra_passes=[failed_delivery_sweep_pass])
    app.state.ledger_worker = ledger_worker
    if settings.ledger_worker_enabled:
        ledger_worker.start()
    debit_sweeper = DebitSweeper(get_sessionmaker(), redis)
    app.state.debit_sweeper = debit_sweeper
    if settings.prepaid_debit_sweeper_enabled:
        debit_sweeper.start()
    await kaspa_engine.start()
    try:
        yield
    finally:
        await dispatcher.stop()
        await ledger_worker.stop()
        await debit_sweeper.stop()
        await kaspa_engine.stop()
        await webhook_client.aclose()
        await http_client.aclose()
        await redis.aclose()
        await db_engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="KasGate",
        version=APP_VERSION,
        description="HTTP 402 payment gateway: pay per request in KAS, settle on the Kaspa network.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            HEADER_PAYMENT_REQUIRED,
            HEADER_PAYMENT_SIGNATURE,
            HEADER_PAYMENT_RESPONSE,
            HEADER_PREPAID_RECEIPT,
            HEADER_TRIAL_OFFER,
            HEADER_TRIAL_RECEIPT,
            HEADER_TRIAL_LIMIT,
            HEADER_TRIAL_REMAINING,
            HEADER_FAILED_DELIVERY_CREDIT,
        ],
        max_age=600,
    )
    app.include_router(api_router)
    app.include_router(well_known_router)
    app.include_router(refunds_router)
    app.include_router(failed_delivery_router)
    app.include_router(prepaid_router)
    app.include_router(trials_router)
    app.include_router(faucet_router)
    app.include_router(webhooks_router)
    app.include_router(proxy_router)

    @app.exception_handler(RateLimited)
    async def rate_limited_handler(_request: Request, exc: RateLimited) -> JSONResponse:
        return exc.response()

    @app.get("/healthz", response_model=HealthStatus, tags=["system"])
    async def healthz(
        maker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    ) -> HealthStatus:
        database_ok = False
        redis_ok = False
        try:
            async with maker() as session:
                await session.execute(text("SELECT 1"))
            database_ok = True
        except Exception:
            logger.warning("health check: database unreachable", exc_info=True)
        try:
            redis_ok = bool(await app.state.redis.ping())
        except Exception:
            logger.warning("health check: redis unreachable", exc_info=True)
        rpc_ok = kaspa_engine.is_connected()
        return HealthStatus(
            status="ok" if database_ok and redis_ok and rpc_ok else "degraded",
            network=settings.kaspa_network,
            database=database_ok,
            redis=redis_ok,
            kaspa_rpc=rpc_ok,
        )

    return app


app = create_app()
