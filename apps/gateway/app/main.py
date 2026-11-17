"""Application entrypoint: lifespan wiring, CORS, health check, and routers."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api_routes import router as api_router
from app.config import get_settings
from app.db import engine as db_engine
from app.deps import get_sessionmaker
from app.kaspa_engine import engine as kaspa_engine
from app.oracle import KasUsdOracle
from app.proxy import router as proxy_router
from app.schemas import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    HealthStatus,
)
from app.security import build_upstream_client

logger = logging.getLogger("kasgate.main")

APP_VERSION = "1.0.0"

__all__ = ["app", "create_app", "kaspa_engine"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    http_client = build_upstream_client()
    app.state.redis = redis
    app.state.http_client = http_client
    app.state.oracle = KasUsdOracle(settings, redis)
    app.state.engine = kaspa_engine
    await kaspa_engine.start()
    try:
        yield
    finally:
        await kaspa_engine.stop()
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
        expose_headers=[HEADER_PAYMENT_REQUIRED, HEADER_PAYMENT_SIGNATURE, HEADER_PAYMENT_RESPONSE],
        max_age=600,
    )
    app.include_router(api_router)
    app.include_router(proxy_router)

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
