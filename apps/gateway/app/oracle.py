"""Live KAS/USD price oracle with Redis caching and a last-known-good fallback."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
import redis.asyncio as aioredis

from app.config import SOMPI_PER_KAS, Settings

logger = logging.getLogger("kasgate.oracle")

RATE_CACHE_KEY: Final[str] = "kasgate:oracle:kas_usd"
RATE_LAST_KEY: Final[str] = "kasgate:oracle:kas_usd:last"
RATE_CACHE_TTL_SECONDS: Final[int] = 60
HTTP_TIMEOUT_SECONDS: Final[float] = 3.0
RATE_QUANTUM: Final[Decimal] = Decimal("0.00000001")

_NESTED_KEYS: Final[tuple[str, ...]] = (
    "kaspa",
    "KAS",
    "kas",
    "data",
    "result",
    "market_data",
    "current_price",
    "quote",
    "USD",
)
_VALUE_KEYS: Final[tuple[str, ...]] = (
    "usd",
    "USD",
    "price",
    "last",
    "rate",
    "kas_usd",
    "value",
    "lastPrice",
    "last_price",
)


class OracleUnavailableError(Exception):
    """No live price could be fetched and no previously stored price exists."""


def usd_value(total_sompi: int, rate: Decimal) -> Decimal:
    """USD value of ``total_sompi`` at ``rate`` USD per KAS, quantised to 8 decimal places."""
    return (Decimal(total_sompi) / Decimal(SOMPI_PER_KAS) * rate).quantize(RATE_QUANTUM)


def _to_rate(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        rate = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not rate.is_finite() or rate <= 0:
        return None
    quantised = rate.quantize(RATE_QUANTUM)
    return quantised if quantised > 0 else None


def extract_rate(payload: Any, _depth: int = 0) -> Decimal | None:
    """Find a positive USD-per-KAS number in the common price API response shapes.

    Handles ``{"kaspa": {"usd": x}}`` (CoinGecko), ``{"price": "x"}`` (exchanges),
    ``{"data": {"price": x}}``, ``{"result": [...]}``, bare numbers and single-item lists.
    """
    if _depth > 5:
        return None
    direct = _to_rate(payload)
    if direct is not None:
        return direct
    if isinstance(payload, list):
        for item in payload:
            found = extract_rate(item, _depth + 1)
            if found is not None:
                return found
        return None
    if not isinstance(payload, dict):
        return None
    for key in _NESTED_KEYS:
        if key in payload and isinstance(payload[key], (dict, list)):
            found = extract_rate(payload[key], _depth + 1)
            if found is not None:
                return found
    for key in _VALUE_KEYS:
        if key in payload:
            found = extract_rate(payload[key], _depth + 1)
            if found is not None:
                return found
    return None


def _decode(value: bytes | str | None) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8") if isinstance(value, bytes) else value


class KasUsdOracle:
    def __init__(
        self,
        settings: Settings,
        redis: aioredis.Redis,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._http_client = http_client
        self._lock = asyncio.Lock()

    async def get_rate(self) -> Decimal:
        """USD per 1 KAS (8 dp): fresh cache, else live fetch, else last stored rate, else error."""
        cached = await self._read(RATE_CACHE_KEY)
        if cached is not None:
            return cached
        async with self._lock:
            cached = await self._read(RATE_CACHE_KEY)
            if cached is not None:
                return cached
            try:
                rate = await self._fetch()
            except OracleUnavailableError as exc:
                fallback = await self._read(RATE_LAST_KEY)
                if fallback is None:
                    raise OracleUnavailableError(f"{exc}; no previously stored KAS/USD rate is available") from exc
                logger.warning("using last known KAS/USD rate %s: %s", fallback, exc)
                return fallback
            await self._store(rate)
            return rate

    async def _read(self, key: str) -> Decimal | None:
        try:
            raw = _decode(await self._redis.get(key))
        except Exception as exc:
            logger.warning("Redis read of %s failed: %s", key, exc)
            return None
        return None if raw is None else _to_rate(raw)

    async def _store(self, rate: Decimal) -> None:
        text = format(rate, "f")
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.set(RATE_CACHE_KEY, text, ex=RATE_CACHE_TTL_SECONDS)
                pipe.set(RATE_LAST_KEY, text)
                await pipe.execute()
        except Exception as exc:
            logger.warning("Redis write of KAS/USD rate failed: %s", exc)

    async def _fetch(self) -> Decimal:
        url = self._settings.kas_usd_price_url
        try:
            if self._http_client is not None:
                response = await self._http_client.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            else:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
                    response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise OracleUnavailableError("KAS/USD price request timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise OracleUnavailableError(f"KAS/USD price source returned HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise OracleUnavailableError(f"KAS/USD price request failed: {exc}") from exc
        rate = extract_rate(payload)
        if rate is None:
            raise OracleUnavailableError("KAS/USD price response contained no positive USD rate")
        return rate
