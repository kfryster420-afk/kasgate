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
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpcore
import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from kaspa import PublicKey, verify_message
from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.deps import get_db_session, get_redis
from app.models import Merchant
from app.schemas import (
    AUTH_PURPOSE,
    HEADER_K402_PAYMENT,
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    HEADER_PREPAID_DEBIT,
    HEADER_PREPAID_RECEIPT,
    HEADER_TRIAL_CALL,
    HEADER_TRIAL_LIMIT,
    HEADER_TRIAL_OFFER,
    HEADER_TRIAL_RECEIPT,
    HEADER_TRIAL_REMAINING,
    NONCE_RE,
    REGISTER_PURPOSE,
    build_auth_challenge,
)
from app.schemas import (
    NAT64_WELL_KNOWN as NAT64_WELL_KNOWN,
)
from app.schemas import (
    is_forbidden_ip as is_forbidden_ip,
)

logger = logging.getLogger("kasgate.security")

API_KEY_PREFIX = "kg_"
SESSION_TOKEN_PREFIX = "kgs_"  # noqa: S105 - a token-type prefix, not a credential
API_KEY_MIN_LENGTH = len(API_KEY_PREFIX) + 16
API_KEY_MAX_LENGTH = 128
SESSION_KEY_PREFIX = "kasgate:session:"
SESSION_INDEX_PREFIX = "kasgate:sessions:"
SESSION_REVOKE_ATTEMPTS = 5

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
        HEADER_K402_PAYMENT.lower(),
        HEADER_PREPAID_DEBIT.lower(),
        HEADER_PREPAID_RECEIPT.lower(),
        HEADER_TRIAL_OFFER.lower(),
        HEADER_TRIAL_CALL.lower(),
        HEADER_TRIAL_RECEIPT.lower(),
        HEADER_TRIAL_LIMIT.lower(),
        HEADER_TRIAL_REMAINING.lower(),
    }
)
FORBIDDEN_UPSTREAM_HEADER_NAMES: frozenset[str] = (
    HOP_BY_HOP_HEADERS | PAYMENT_HEADERS | frozenset({"host", "content-length", "accept-encoding"})
)
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
HEADER_VALUE_MAX_LENGTH = 4096

_bearer_scheme = HTTPBearer(
    auto_error=False, description="KasGate merchant API key (kg_...) or dashboard session (kgs_...)"
)


# ---------------------------------------------------------------- API keys


def generate_api_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_session_token() -> str:
    return f"{SESSION_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def session_digest(token: str) -> str:
    return hash_api_key(token)


def session_key(token: str) -> str:
    return f"{SESSION_KEY_PREFIX}{session_digest(token)}"


def session_index_key(merchant_id: uuid.UUID) -> str:
    """`kasgate:sessions:{merchant_id}`: the set of that merchant's live session digests (for logout-all)."""
    return f"{SESSION_INDEX_PREFIX}{merchant_id}"


def _text(value: bytes | str) -> str:
    return value.decode("ascii") if isinstance(value, bytes) else value


async def prune_session_index(redis: Redis, merchant_id: uuid.UUID) -> list[str]:
    """Drop members whose `kasgate:session:{digest}` key has expired or been deleted; return the live digests."""
    index = session_index_key(merchant_id)
    members = sorted(_text(member) for member in await cast(Awaitable[set[Any]], redis.smembers(index)))
    if not members:
        return []
    async with redis.pipeline(transaction=False) as pipe:
        for digest in members:
            pipe.exists(f"{SESSION_KEY_PREFIX}{digest}")
        flags = await pipe.execute()
    live = [digest for digest, present in zip(members, flags, strict=True) if present]
    stale = [digest for digest, present in zip(members, flags, strict=True) if not present]
    if stale:
        await cast(Awaitable[int], redis.srem(index, *stale))
    return live


async def create_session(redis: Redis, merchant_id: uuid.UUID) -> tuple[str, datetime]:
    """Issue a `kgs_` session and index it; return (token, expiry).

    One MULTI/EXEC stores `kasgate:session:{sha256(token)}` -> merchant id (EX `session_ttl_seconds`), adds the digest
    to `kasgate:sessions:{merchant_id}` and re-arms that set's expiry to the same TTL. The new session always has the
    longest remaining life, so the set never expires before any session it lists. Stale members are pruned on the way.
    """
    ttl = get_settings().session_ttl_seconds
    token = generate_session_token()
    index = session_index_key(merchant_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(session_key(token), str(merchant_id), ex=ttl)
        pipe.sadd(index, session_digest(token))
        pipe.expire(index, ttl)
        await pipe.execute()
    try:
        await prune_session_index(redis, merchant_id)
    except RedisError:
        logger.warning("could not prune the session index of %s", merchant_id, exc_info=True)
    return token, datetime.now(UTC) + timedelta(seconds=ttl)


async def revoke_session(redis: Redis, merchant_id: uuid.UUID, token: str) -> None:
    """Log out one session: delete `kasgate:session:{digest}` and remove the digest from the merchant's set."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(session_key(token))
        pipe.srem(session_index_key(merchant_id), session_digest(token))
        await pipe.execute()


async def revoke_all_sessions(redis: Redis, merchant_id: uuid.UUID) -> int:
    """Delete every session of `merchant_id` and the index itself; return how many session keys existed.

    The index is WATCHed while its members are read, so a session issued concurrently makes the MULTI/EXEC retry
    instead of being left behind unlisted. Sessions that exist but are not in the set cannot occur: `create_session`
    writes both in one transaction.
    """
    index = session_index_key(merchant_id)
    async with redis.pipeline(transaction=True) as pipe:
        for _ in range(SESSION_REVOKE_ATTEMPTS):
            try:
                await pipe.watch(index)
                members = [_text(member) for member in await cast(Awaitable[set[Any]], pipe.smembers(index))]
                pipe.multi()  # type: ignore[no-untyped-call]
                for digest in members:
                    pipe.delete(f"{SESSION_KEY_PREFIX}{digest}")
                pipe.delete(index)
                results = await pipe.execute()
            except WatchError:
                continue
            return sum(int(count) for count in results[:-1])
    raise RedisError("could not revoke sessions: the session index kept changing")


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _merchant_for_session(token: str, session: AsyncSession, redis: Redis) -> Merchant | None:
    try:
        raw = await redis.get(session_key(token))
    except RedisError as exc:
        logger.error("session store unavailable", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service temporarily unavailable",
            headers={"Retry-After": "5"},
        ) from exc
    if raw is None:
        return None
    try:
        merchant_id = uuid.UUID(str(raw))
    except ValueError:
        return None
    return (await session.execute(select(Merchant).where(Merchant.id == merchant_id))).scalar_one_or_none()


def get_bearer_token(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme)) -> str:
    """The raw `Authorization: Bearer` token (401 when absent); used by routes that act on the credential itself."""
    if credentials is None:
        raise _unauthorized("missing bearer token")
    return credentials.credentials.strip()


async def get_current_merchant(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> Merchant:
    """Resolve `Authorization: Bearer kg_<api key>` (hash lookup) or `kgs_<session>` (Redis session) to a merchant."""
    if credentials is None:
        raise _unauthorized("missing bearer token")
    token = credentials.credentials.strip()
    if not API_KEY_MIN_LENGTH <= len(token) <= API_KEY_MAX_LENGTH or not token.isascii():
        raise _unauthorized("invalid API key")
    merchant: Merchant | None
    if token.startswith(SESSION_TOKEN_PREFIX):
        merchant = await _merchant_for_session(token, session, redis)
        if merchant is None:
            raise _unauthorized("invalid or expired session")
    elif token.startswith(API_KEY_PREFIX):
        digest = hash_api_key(token)
        merchant = (await session.execute(select(Merchant).where(Merchant.api_key_hash == digest))).scalar_one_or_none()
        if merchant is None or not hmac.compare_digest(merchant.api_key_hash, digest):
            raise _unauthorized("invalid API key")
    else:
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


async def _getaddrinfo(host: str, port: int) -> list[tuple[Any, Any, Any, Any, tuple[Any, ...]]]:
    """DNS lookup seam: the one place the upstream guard talks to the resolver (replaced in tests)."""
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def resolve_public_address(host: str, port: int) -> str:
    """Resolve `host` at request time and return one address; fail if ANY resolved address is non-public.

    Names such as `*.nip.io` or `localtest.me` resolve to private or loopback addresses and are refused here, as are
    literal IPs outside `is_global` (for example 100.64.0.0/10). The caller connects to the returned IP itself.
    """
    try:
        infos = await _getaddrinfo(host.strip("[]"), port)
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


WEBHOOK_MAX_CONNECTIONS = 50


def build_webhook_client(timeout_seconds: float) -> httpx.AsyncClient:
    """Outbound client for merchant webhooks: the same SSRF guard as upstream calls (every resolved address must be
    public and the socket goes to the validated IP), no redirects, no proxy settings from the environment."""
    return httpx.AsyncClient(
        transport=GuardedTransport(
            limits=httpx.Limits(max_connections=WEBHOOK_MAX_CONNECTIONS, max_keepalive_connections=10)
        ),
        timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0)),
        follow_redirects=False,
        trust_env=False,
    )


def build_upstream_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=GuardedTransport(),
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
        trust_env=False,
    )


# ---------------------------------------------------------------- wallet-signature login / registration

AUTH_CHALLENGE_TTL_SECONDS = 300
AUTH_KEY_PREFIX = "kasgate:auth:"
AuthPurpose = Literal["login", "register"]
_PURPOSE_TEXT: dict[AuthPurpose, str] = {"login": AUTH_PURPOSE, "register": REGISTER_PURPOSE}
_KASPA_NETWORK_NAME: dict[str, str] = {"kaspa:testnet-10": "testnet", "kaspa:mainnet": "mainnet"}
_NONCE_PATTERN = re.compile(NONCE_RE)


@dataclass(frozen=True)
class AuthChallengeData:
    purpose: AuthPurpose
    address: str
    nonce: str
    expires_at: datetime
    message: str


def auth_domain() -> str:
    """`urlsplit(PUBLIC_BASE_URL).netloc` lowercased: the domain every signed challenge is bound to."""
    return urlsplit(get_settings().public_base_url).netloc.lower()


def _build_auth_message(purpose: AuthPurpose, address: str, nonce: str, expires_at: datetime) -> str:
    return build_auth_challenge(_PURPOSE_TEXT[purpose], auth_domain(), address, nonce, expires_at)


def new_auth_challenge(purpose: AuthPurpose, address: str, now: datetime | None = None) -> AuthChallengeData:
    moment = now or datetime.now(UTC)
    expires_at = moment.astimezone(UTC).replace(microsecond=0) + timedelta(seconds=AUTH_CHALLENGE_TTL_SECONDS)
    nonce = secrets.token_hex(16)
    return AuthChallengeData(
        purpose=purpose,
        address=address,
        nonce=nonce,
        expires_at=expires_at,
        message=_build_auth_message(purpose, address, nonce, expires_at),
    )


def parse_auth_challenge(message: str) -> AuthChallengeData:
    """Parse a challenge produced by `new_auth_challenge`; raises ValueError unless it is byte-identical to one."""
    lines = message.split("\n")
    prefixes = ("Domain: ", "Purpose: ", "Address: ", "Nonce: ", "Expires: ")
    if len(lines) != 6 or lines[0] != "KasGate":
        raise ValueError("malformed auth challenge")
    if not all(line.startswith(prefix) for line, prefix in zip(lines[1:], prefixes, strict=True)):
        raise ValueError("malformed auth challenge")
    _, purpose_text, address, nonce, expires = (
        line[len(prefix) :] for line, prefix in zip(lines[1:], prefixes, strict=True)
    )
    purposes = [name for name, text in _PURPOSE_TEXT.items() if text == purpose_text]
    if not purposes or not _NONCE_PATTERN.fullmatch(nonce):
        raise ValueError("malformed auth challenge purpose or nonce")
    expires_at = datetime.fromisoformat(expires)
    if expires_at.tzinfo is None:
        raise ValueError("auth challenge expiry has no timezone")
    parsed = AuthChallengeData(
        purpose=purposes[0], address=address, nonce=nonce, expires_at=expires_at.astimezone(UTC), message=message
    )
    if _build_auth_message(parsed.purpose, address, nonce, parsed.expires_at) != message:
        raise ValueError("auth challenge is not in canonical form for this domain")
    return parsed


def auth_challenge_key(purpose: AuthPurpose, address: str, nonce: str) -> str:
    return f"{AUTH_KEY_PREFIX}{purpose}:{address}:{nonce}"


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


async def consume_wallet_proof(
    redis: Redis,
    *,
    purpose: AuthPurpose,
    address: str,
    challenge: str,
    public_key: str,
    signature: str,
) -> bool:
    """True only for a live, unused challenge of `purpose` for `address` that the wallet signed.

    The challenge is single use: it is consumed (GETDEL) whenever it parses, and the signature check always runs, so
    the work done and the outcome are the same however the proof fails. Raises 503 when Redis is unavailable.
    """
    parsed: AuthChallengeData | None
    try:
        parsed = parse_auth_challenge(challenge)
    except ValueError:
        parsed = None
    challenge_ok = False
    if parsed is not None:
        try:
            stored = await redis.getdel(auth_challenge_key(parsed.purpose, parsed.address, parsed.nonce))
        except RedisError as exc:
            logger.exception("could not consume auth challenge")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="login is temporarily unavailable",
                headers={"Retry-After": "5"},
            ) from exc
        challenge_ok = (
            stored is not None
            and hmac.compare_digest(str(stored), challenge)
            and parsed.purpose == purpose
            and parsed.address == address
            and parsed.expires_at > datetime.now(UTC)
        )
    signature_ok = await verify_wallet_signature(challenge, signature, public_key, address)
    return challenge_ok and signature_ok


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
# CONTRACT v2.12: free trial calls actually served, per client IP (anti-abuse on top of the per-endpoint daily cap).
RATE_LIMIT_TRIAL_CALLS = RateLimit("trial_calls", "rl_trial_calls_per_ip_per_hour", 3600)


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
