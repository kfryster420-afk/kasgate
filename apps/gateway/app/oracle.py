"""Multi-source KAS/USD price oracle with Redis caching, last-good fallback, and a staleness cutoff."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
USER_AGENT: Final[str] = "KasGate-Oracle/1.0"

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
    """No live price could be fetched, and no last-good rate is still within the staleness window."""


@dataclass(frozen=True)
class QuotedRate:
    rate: Decimal
    quoted_at: datetime


@dataclass(frozen=True)
class OracleSource:
    name: str
    url: str
    parse: Callable[[Any], Decimal | None]


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


def parse_coingecko(payload: Any) -> Decimal | None:
    if isinstance(payload, dict):
        kaspa = payload.get("kaspa")
        if isinstance(kaspa, dict):
            found = _to_rate(kaspa.get("usd"))
            if found is not None:
                return found
    return extract_rate(payload)


def parse_coinpaprika(payload: Any) -> Decimal | None:
    if isinstance(payload, dict):
        quotes = payload.get("quotes")
        if isinstance(quotes, dict):
            usd = quotes.get("USD") if "USD" in quotes else quotes.get("usd")
            if isinstance(usd, dict):
                found = _to_rate(usd.get("price"))
                if found is not None:
                    return found
    return extract_rate(payload)


def parse_kraken(payload: Any) -> Decimal | None:
    if not isinstance(payload, dict):
        return extract_rate(payload)
    errors = payload.get("error")
    if isinstance(errors, list) and errors:
        return None
    result = payload.get("result")
    if isinstance(result, dict) and result:
        first = next(iter(result.values()))
        if isinstance(first, dict):
            close = first.get("c")
            if isinstance(close, list) and close:
                found = _to_rate(close[0])
                if found is not None:
                    return found
    return extract_rate(payload)


def parse_mexc(payload: Any) -> Decimal | None:
    if isinstance(payload, dict):
        found = _to_rate(payload.get("price"))
        if found is not None:
            return found
    return extract_rate(payload)


def median_rate(rates: Sequence[Decimal]) -> Decimal:
    if not rates:
        raise ValueError("median of an empty rate list")
    ordered = sorted(rates)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return ((ordered[mid - 1] + ordered[mid]) / Decimal(2)).quantize(RATE_QUANTUM)


def reject_outliers(rates: Sequence[Decimal], max_deviation: Decimal) -> list[Decimal]:
    """Drop rates whose relative distance from the median exceeds ``max_deviation`` (e.g. 0.5 = 50%)."""
    if len(rates) < 2:
        return list(rates)
    mid = median_rate(rates)
    if mid <= 0:
        return []
    return [rate for rate in rates if abs(rate - mid) / mid <= max_deviation]


def configured_sources(settings: Settings) -> list[OracleSource]:
    """The enabled sources, in a stable order. A source is disabled by setting its URL to empty."""
    candidates = (
        ("coingecko", settings.oracle_source_coingecko, parse_coingecko),
        ("coinpaprika", settings.oracle_source_coinpaprika, parse_coinpaprika),
        ("kraken", settings.oracle_source_kraken, parse_kraken),
        ("mexc", settings.oracle_source_mexc, parse_mexc),
    )
    return [OracleSource(name, url.strip(), parse) for name, url, parse in candidates if url.strip()]


def _decode(value: bytes | str | None) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _encode_quoted(quoted: QuotedRate) -> str:
    return json.dumps({"rate": format(quoted.rate, "f"), "quoted_at": quoted.quoted_at.isoformat()}, sort_keys=True)


def _parse_quoted(raw: str) -> QuotedRate | None:
    try:
        data = json.loads(raw)
    except ValueError:
        # Legacy bare-rate last-good values have no timestamp; fail closed rather than guess their age.
        return None
    if not isinstance(data, dict):
        return None
    rate = _to_rate(data.get("rate"))
    quoted_raw = data.get("quoted_at")
    if rate is None or not isinstance(quoted_raw, str):
        return None
    try:
        quoted_at = datetime.fromisoformat(quoted_raw)
    except ValueError:
        return None
    if quoted_at.tzinfo is None:
        quoted_at = quoted_at.replace(tzinfo=UTC)
    return QuotedRate(rate=rate, quoted_at=quoted_at.astimezone(UTC))


class KasUsdOracle:
    def __init__(
        self,
        settings: Settings,
        redis: aioredis.Redis,
        *,
        http_client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._http_client = http_client
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()

    async def get_rate(self) -> Decimal:
        """USD per 1 KAS (8 dp): fresh cache, else live multi-source median, else last-good within max staleness."""
        return (await self.get_quoted_rate()).rate

    async def get_quoted_rate(self) -> QuotedRate:
        cached = await self._read(RATE_CACHE_KEY)
        if cached is not None:
            return cached
        async with self._lock:
            cached = await self._read(RATE_CACHE_KEY)
            if cached is not None:
                return cached
            try:
                quoted = await self._fetch_quoted()
            except OracleUnavailableError as exc:
                fallback = await self._usable_last_good()
                if fallback is None:
                    raise OracleUnavailableError(f"{exc}; no last-good KAS/USD rate is still fresh enough") from exc
                logger.warning(
                    "using last known KAS/USD rate %s quoted at %s: %s", fallback.rate, fallback.quoted_at, exc
                )
                await self._store_cache(fallback)
                return fallback
            await self._store(quoted)
            return quoted

    async def _usable_last_good(self) -> QuotedRate | None:
        last = await self._read(RATE_LAST_KEY)
        if last is None:
            return None
        age = self._now() - last.quoted_at
        if age > timedelta(seconds=self._settings.oracle_max_staleness_seconds):
            logger.warning(
                "last-good KAS/USD rate %s quoted at %s is older than %ss",
                last.rate,
                last.quoted_at,
                self._settings.oracle_max_staleness_seconds,
            )
            return None
        if age < timedelta(0):
            return None
        return last

    async def _read(self, key: str) -> QuotedRate | None:
        try:
            raw = _decode(await self._redis.get(key))
        except Exception as exc:
            logger.warning("Redis read of %s failed: %s", key, exc)
            return None
        return None if raw is None else _parse_quoted(raw)

    async def _store(self, quoted: QuotedRate) -> None:
        text = _encode_quoted(quoted)
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.set(RATE_CACHE_KEY, text, ex=self._settings.oracle_cache_ttl_seconds)
                pipe.set(RATE_LAST_KEY, text)
                await pipe.execute()
        except Exception as exc:
            logger.warning("Redis write of KAS/USD rate failed: %s", exc)

    async def _store_cache(self, quoted: QuotedRate) -> None:
        """Re-arm the short cache for a last-good fallback without refreshing its quoted_at."""
        remaining = int(
            (
                timedelta(seconds=self._settings.oracle_max_staleness_seconds) - (self._now() - quoted.quoted_at)
            ).total_seconds()
        )
        ttl = max(1, min(self._settings.oracle_cache_ttl_seconds, remaining))
        try:
            await self._redis.set(RATE_CACHE_KEY, _encode_quoted(quoted), ex=ttl)
        except Exception as exc:
            logger.warning("Redis write of KAS/USD cache failed: %s", exc)

    async def _fetch_quoted(self) -> QuotedRate:
        sources = configured_sources(self._settings)
        min_sources = self._settings.oracle_min_sources
        if len(sources) < min_sources:
            raise OracleUnavailableError(
                f"only {len(sources)} KAS/USD source(s) are configured; {min_sources} are required"
            )
        results = await asyncio.gather(*(self._fetch_one(source) for source in sources), return_exceptions=True)
        rates: list[Decimal] = []
        for source, result in zip(sources, results, strict=True):
            if isinstance(result, Decimal):
                rates.append(result)
            else:
                logger.warning("KAS/USD source %s failed: %s", source.name, result)
        if len(rates) < min_sources:
            raise OracleUnavailableError(
                f"only {len(rates)} of {len(sources)} KAS/USD sources returned a usable rate; "
                f"{min_sources} are required"
            )
        kept = reject_outliers(rates, self._settings.oracle_outlier_max_deviation)
        if len(kept) < min_sources:
            raise OracleUnavailableError(
                f"only {len(kept)} KAS/USD source(s) remained after outlier rejection; {min_sources} are required"
            )
        return QuotedRate(rate=median_rate(kept), quoted_at=self._now().astimezone(UTC))

    async def _fetch_one(self, source: OracleSource) -> Decimal:
        timeout = self._settings.oracle_timeout_seconds
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        try:
            if self._http_client is not None:
                response = await self._http_client.get(source.url, timeout=timeout, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
                    response = await client.get(source.url)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise OracleUnavailableError(f"{source.name} timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise OracleUnavailableError(f"{source.name} returned HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise OracleUnavailableError(f"{source.name} request failed: {exc}") from exc
        rate = source.parse(payload)
        if rate is None:
            raise OracleUnavailableError(f"{source.name} response contained no positive USD rate")
        return rate
