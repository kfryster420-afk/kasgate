"""API-key handling, upstream-secret encryption, and the upstream SSRF guard."""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpcore
import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from kaspa import PublicKey, verify_message
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.deps import get_db_session, get_redis
from app.models import Merchant
from app.schemas import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    AuthChallengeRequest,
)

logger = logging.getLogger("kasgate.security")

API_KEY_PREFIX = "kg_"
API_KEY_MIN_LENGTH = len(API_KEY_PREFIX) + 16
API_KEY_MAX_LENGTH = 128

HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
PAYMENT_HEADERS: frozenset[str] = frozenset(
    {
        HEADER_PAYMENT_REQUIRED.lower(),
        HEADER_PAYMENT_SIGNATURE.lower(),
        HEADER_PAYMENT_RESPONSE.lower(),
    }
)
FORBIDDEN_UPSTREAM_HEADER_NAMES: frozenset[str] = (
    HOP_BY_HOP_HEADERS | PAYMENT_HEADERS | frozenset({"host", "content-length", "accept-encoding"})
)
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
HEADER_VALUE_MAX_LENGTH = 4096

_bearer_scheme = HTTPBearer(auto_error=False, description="KasGate merchant API key (kg_...)")


# ---------------------------------------------------------------- API keys


def generate_api_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_merchant(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> Merchant:
    if credentials is None:
        raise _unauthorized("missing bearer token")
    token = credentials.credentials.strip()
    if (
        not token.startswith(API_KEY_PREFIX)
        or not API_KEY_MIN_LENGTH <= len(token) <= API_KEY_MAX_LENGTH
        or not token.isascii()
    ):
        raise _unauthorized("invalid API key")
    digest = hash_api_key(token)
    merchant = (await session.execute(select(Merchant).where(Merchant.api_key_hash == digest))).scalar_one_or_none()
    if merchant is None or not hmac.compare_digest(merchant.api_key_hash, digest):
        raise _unauthorized("invalid API key")
    if not merchant.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="merchant account is disabled")
    return merchant


# ---------------------------------------------------------------- upstream header encryption


def get_fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode("ascii"))


def validate_upstream_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Reject header names/values that could smuggle requests or override gateway-controlled headers."""
    cleaned: dict[str, str] = {}
    seen: set[str] = set()
    for name, value in headers.items():
        if not HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid upstream header name: {name!r}")
        lowered = name.lower()
        if lowered in FORBIDDEN_UPSTREAM_HEADER_NAMES:
            raise ValueError(f"upstream header {name!r} is managed by the gateway and cannot be set")
        if lowered in seen:
            raise ValueError(f"duplicate upstream header: {name!r}")
        if len(value) > HEADER_VALUE_MAX_LENGTH or any(
            ord(ch) < 0x20 and ch != "\t" or ord(ch) == 0x7F for ch in value
        ):
            raise ValueError(f"invalid value for upstream header {name!r}")
        seen.add(lowered)
        cleaned[name] = value
    return cleaned


def encrypt_headers(headers: Mapping[str, str]) -> bytes | None:
    if not headers:
        return None
    payload = json.dumps(dict(headers), separators=(",", ":"), sort_keys=True).encode("utf-8")
    return get_fernet().encrypt(payload)


def decrypt_headers(token: bytes | None) -> dict[str, str]:
    if not token:
        return {}
    try:
        raw = get_fernet().decrypt(bytes(token))
    except InvalidToken as exc:
        raise ValueError("upstream headers cannot be decrypted with the configured ENCRYPTION_KEY") from exc
    data = json.loads(raw)
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError("decrypted upstream headers are malformed")
    return {str(k): str(v) for k, v in data.items()}


# ---------------------------------------------------------------- SSRF guard


class UpstreamBlockedError(Exception):
    """Raised when an upstream host resolves to a non-public address."""


def is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


async def resolve_public_address(host: str, port: int) -> str:
    """Resolve `host` and return one address, failing if ANY resolved address is non-public."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UpstreamBlockedError(f"cannot resolve upstream host {host!r}") from exc
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        if is_forbidden_ip(ipaddress.ip_address(address)):
            raise UpstreamBlockedError(f"upstream host {host!r} resolves to a non-public address")
        addresses.append(address)
    if not addresses:
        raise UpstreamBlockedError(f"upstream host {host!r} has no addresses")
    return addresses[0]


class _GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connects to the validated IP itself, so DNS rebinding cannot swap in a private address."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            address = await resolve_public_address(host, port)
        except UpstreamBlockedError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        return await self._inner.connect_tcp(
            address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not permitted for upstream requests")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose TCP connections only ever go to public IP addresses."""

    def __init__(self, limits: httpx.Limits | None = None) -> None:
        effective = limits or httpx.Limits(max_connections=200, max_keepalive_connections=50)
        super().__init__(limits=effective, retries=0, http2=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=effective.max_connections,
            max_keepalive_connections=effective.max_keepalive_connections,
            keepalive_expiry=effective.keepalive_expiry,
            http1=True,
            http2=False,
            network_backend=_GuardedNetworkBackend(),
        )


def build_upstream_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=GuardedTransport(),
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
        trust_env=False,
    )


# ---------------------------------------------------------------- wallet-signature login

AUTH_CHALLENGE_TTL_SECONDS = 300
AUTH_CHALLENGE_KEY_PREFIX = "kasgate:auth:"
_AUTH_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_LOGIN_HEADER = "KasGate login"
_KASPA_NETWORK_NAME: dict[str, str] = {"kaspa:testnet-10": "testnet", "kaspa:mainnet": "mainnet"}


@dataclass(frozen=True)
class LoginChallenge:
    address: str
    nonce: str
    expires_at: datetime


def new_login_challenge(address: str, now: datetime | None = None) -> tuple[LoginChallenge, str]:
    moment = now or datetime.now(UTC)
    expires_at = moment.astimezone(UTC).replace(microsecond=0) + timedelta(seconds=AUTH_CHALLENGE_TTL_SECONDS)
    challenge = LoginChallenge(address=address, nonce=secrets.token_hex(16), expires_at=expires_at)
    return challenge, format_login_challenge(challenge)


def format_login_challenge(challenge: LoginChallenge) -> str:
    return (
        f"{_LOGIN_HEADER}\nAddress: {challenge.address}\nNonce: {challenge.nonce}\n"
        f"Expires: {challenge.expires_at.isoformat()}"
    )


def parse_login_challenge(message: str) -> LoginChallenge:
    """Parse a challenge produced by `format_login_challenge`; raises ValueError if it is not byte-identical."""
    lines = message.split("\n")
    if len(lines) != 4 or lines[0] != _LOGIN_HEADER:
        raise ValueError("malformed login challenge")
    prefixes = ("Address: ", "Nonce: ", "Expires: ")
    if not all(line.startswith(prefix) for line, prefix in zip(lines[1:], prefixes, strict=True)):
        raise ValueError("malformed login challenge")
    address, nonce, expires = (line[len(prefix) :] for line, prefix in zip(lines[1:], prefixes, strict=True))
    if not _AUTH_NONCE_RE.fullmatch(nonce):
        raise ValueError("malformed login challenge nonce")
    expires_at = datetime.fromisoformat(expires)
    if expires_at.tzinfo is None:
        raise ValueError("login challenge expiry has no timezone")
    parsed = LoginChallenge(address=address, nonce=nonce, expires_at=expires_at.astimezone(UTC))
    if format_login_challenge(parsed) != message:
        raise ValueError("login challenge is not in canonical form")
    return parsed


def challenge_key(address: str, nonce: str) -> str:
    return f"{AUTH_CHALLENGE_KEY_PREFIX}{address}:{nonce}"


def _check_wallet_signature(message: str, signature_hex: str, public_key_hex: str, expected_address: str) -> bool:
    try:
        public_key = PublicKey(public_key_hex)
        network = _KASPA_NETWORK_NAME[get_settings().kaspa_network]
        derived = str(public_key.to_address(network).to_string()).lower()
        if not hmac.compare_digest(derived, expected_address.lower()):
            return False
        return bool(verify_message(message, signature_hex, public_key))
    except Exception:
        return False


async def verify_wallet_signature(message: str, signature_hex: str, public_key_hex: str, expected_address: str) -> bool:
    """True only if `signature_hex` signs `message` AND the public key derives exactly `expected_address`."""
    return await asyncio.to_thread(_check_wallet_signature, message, signature_hex, public_key_hex, expected_address)


# ---------------------------------------------------------------- rate limiting (fixed window, Redis)

RATE_LIMIT_KEY_PREFIX = "kasgate:rl:"


@dataclass(frozen=True)
class RateLimit:
    """A fixed-window rule. `limit_setting` names the `Settings` field holding the per-window maximum."""

    route: str
    limit_setting: str
    window_seconds: int


RATE_LIMIT_CREATE_MERCHANT = RateLimit("merchants", "rl_merchant_create_per_hour", 3600)
RATE_LIMIT_AUTH_CHALLENGE = RateLimit("auth_challenge", "rl_auth_challenge_per_minute", 60)
RATE_LIMIT_AUTH_VERIFY = RateLimit("auth_verify", "rl_auth_verify_per_minute", 60)
RATE_LIMIT_UNPAID_PROXY = RateLimit("proxy_unpaid", "rl_unpaid_proxy_per_minute", 60)


def client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


async def enforce_rate_limit(redis: Redis, rule: RateLimit, subject: str) -> None:
    """Count one hit for `subject` in the current window; raise 429 with Retry-After when over the limit.

    INCR, EXPIRE NX and TTL run in one MULTI/EXEC, so the window is armed atomically on the first hit and a
    key can never be left without an expiry. Limits and switches come from `Settings`: with
    `rate_limit_enabled` false Redis is never touched; when Redis is unreachable the request gets 503
    (`rate_limit_fail_closed`, default) or is let through (fail open).
    """
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return
    limit = int(getattr(settings, rule.limit_setting))
    key = f"{RATE_LIMIT_KEY_PREFIX}{rule.route}:{subject}"
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, rule.window_seconds, nx=True)
            pipe.ttl(key)
            count, _, ttl = await pipe.execute()
    except RedisError as exc:
        if settings.rate_limit_fail_closed:
            logger.error("rate limiter unavailable for %s; rejecting request", rule.route, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="service temporarily unavailable",
                headers={"Retry-After": "5"},
            ) from exc
        logger.warning("rate limiter unavailable for %s; allowing request", rule.route, exc_info=True)
        return
    if int(count) > limit:
        retry_after = int(ttl) if int(ttl) > 0 else rule.window_seconds
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded; retry later",
            headers={"Retry-After": str(retry_after)},
        )


def limit_by_ip(rule: RateLimit) -> Callable[[Request, Redis], Awaitable[None]]:
    """Dependency factory: `Depends(limit_by_ip(RATE_LIMIT_...))` limits each client IP."""

    async def dependency(request: Request, redis: Redis = Depends(get_redis)) -> None:
        await enforce_rate_limit(redis, rule, f"ip:{client_ip(request)}")

    return dependency


async def limit_challenge_by_address(payload: AuthChallengeRequest, redis: Redis = Depends(get_redis)) -> None:
    """Per-address limit for `/v1/auth/challenge` (the per-IP limit is a separate dependency)."""
    await enforce_rate_limit(redis, RATE_LIMIT_AUTH_CHALLENGE, f"addr:{payload.kaspa_address}")
