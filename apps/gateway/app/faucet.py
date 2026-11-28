"""Testnet faucet: ``POST /v1/faucet/claim`` sends ``FAUCET_AMOUNT_SOMPI`` TKAS from a dedicated hot wallet.

Disabled (503 ``faucet_unavailable``) unless ``FAUCET_PRIVATE_KEY`` is set; enabling it on any network but testnet-10
refuses to start (``Settings`` validation). Limits are enforced in Redis, atomically, BEFORE the send and released or
rolled back when the send fails:

* one claim per address per 24 h:  ``SET faucet:addr:<address> NX EX 86400``
* one claim per client IP per 24 h: ``SET faucet:ip:<ip> NX EX 86400`` (``security.client_ip``: trusted proxies only)
* a global daily cap in sompi:      ``INCRBY faucet:day:<UTC date> <amount>`` against ``FAUCET_DAILY_CAP_SOMPI``
"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Protocol

import kaspa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from kaspa import Address, PrivateKey
from pydantic import field_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings
from app.deps import get_redis
from app.kaspa_engine import engine as kaspa_engine
from app.kaspa_types import validate_kaspa_address
from app.payments import PaymentVerificationError
from app.proxy import _error
from app.schemas import StrictModel
from app.security import client_ip

logger = logging.getLogger("kasgate.faucet")

router = APIRouter(prefix="/v1/faucet", tags=["faucet"])

FAUCET_NETWORK = "kaspa:testnet-10"
CLAIM_WINDOW_SECONDS = 86_400
DAY_KEY_TTL_SECONDS = 172_800
PENDING_CHANGE_SECONDS = 120.0
ADDRESS_KEY_PREFIX = "faucet:addr:"
IP_KEY_PREFIX = "faucet:ip:"
DAY_KEY_PREFIX = "faucet:day:"


class FaucetClaimRequest(StrictModel):
    address: str

    @field_validator("address")
    @classmethod
    def _testnet_address(cls, value: str) -> str:
        return validate_kaspa_address(value, FAUCET_NETWORK)


class FaucetClaimResponse(StrictModel):
    tx_id: str
    amount_sompi: int
    address: str


class FaucetSendError(Exception):
    """The faucet could not build or broadcast the transaction (nothing was sent)."""


class FaucetSender(Protocol):
    async def send(self, address: str, amount_sompi: int) -> str: ...


def _entry_outpoint(entry: dict[str, Any]) -> tuple[str, int]:
    out = entry["outpoint"]
    return str(out["transactionId"]), int(out["index"])


def _entry_amount(entry: dict[str, Any]) -> int:
    return int(entry.get("utxoEntry", entry)["amount"])


def _with_covenant(entry: dict[str, Any]) -> dict[str, Any]:
    """A copy whose UTXO entry carries ``covenantId`` (required by ``UtxoEntryReference.from_dict``)."""
    inner = entry.get("utxoEntry")
    if isinstance(inner, dict):
        return {**entry, "utxoEntry": {"covenantId": None, **inner}}
    return {"covenantId": None, **entry}


class KaspaFaucetSender:
    """Builds, signs (kaspa Generator + PendingTransaction.sign, as the buyer SDK does) and broadcasts one payment.

    Sends are serialised. Outpoints spent and change created by this process are remembered for a short while so back
    to back claims do not reuse a UTXO the node has not yet dropped, or miss change still in the mempool.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._spent: dict[tuple[str, int], float] = {}
        self._change: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}

    def _usable_entries(self, node_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = time.monotonic()
        self._spent = {k: t for k, t in self._spent.items() if now - t < PENDING_CHANGE_SECONDS}
        self._change = {k: v for k, v in self._change.items() if now - v[0] < PENDING_CHANGE_SECONDS}
        seen = {_entry_outpoint(e) for e in node_entries}
        entries = [_with_covenant(e) for e in node_entries if _entry_outpoint(e) not in self._spent]
        entries.extend(e for k, (_, e) in self._change.items() if k not in seen and k not in self._spent)
        return sorted(entries, key=_entry_amount, reverse=True)

    async def send(self, address: str, amount_sompi: int) -> str:
        settings = self._settings or get_settings()
        secret = settings.faucet_private_key
        if secret is None:
            raise FaucetSendError("faucet is not configured")
        key = PrivateKey(secret.get_secret_value())
        faucet_address = key.to_address("testnet")
        faucet_str = faucet_address.to_string()
        async with self._lock:
            try:
                node_entries = await kaspa_engine.utxos_for_address(faucet_str)
            except PaymentVerificationError as exc:
                raise FaucetSendError(f"node UTXO lookup failed: {exc.message}") from exc
            entries = self._usable_entries(node_entries)
            if sum(_entry_amount(e) for e in entries) < amount_sompi:
                raise FaucetSendError("faucet wallet balance is too low")
            try:
                payment_outputs: Any = [kaspa.PaymentOutput(Address(address), amount_sompi)]
                generator = kaspa.Generator(
                    entries=[kaspa.UtxoEntryReference.from_dict(e) for e in entries],
                    change_address=faucet_address,
                    network_id=kaspa.NetworkId("testnet-10"),
                    outputs=payment_outputs,
                    priority_fee=0,
                )
                pending = []
                while (item := next(generator, None)) is not None:
                    pending.append(item)
                if len(pending) != 1 or pending[0].transaction_type != "final":
                    raise FaucetSendError("faucet wallet is fragmented; consolidate its UTXOs")
                if pending[0].fee_amount > settings.faucet_max_network_fee_sompi:
                    raise FaucetSendError("network fee above the faucet maximum")
                pending[0].sign([key], True)
            except FaucetSendError:
                raise
            except Exception as exc:
                raise FaucetSendError(f"could not build the faucet transaction: {exc}") from exc
            tx = pending[0].transaction
            tx_id = str(tx.id)
            try:
                await kaspa_engine.broadcast(tx, tx_id)
            except PaymentVerificationError as exc:
                raise FaucetSendError(f"broadcast failed: {exc.message}") from exc
            now = time.monotonic()
            for item_input in tx.inputs:
                self._spent[
                    (str(item_input.previous_outpoint.transaction_id), int(item_input.previous_outpoint.index))
                ] = now
            outputs = tx.outputs
            if len(outputs) > 1:
                self._change[(tx_id, 1)] = (
                    now,
                    {
                        "address": faucet_str,
                        "outpoint": {"transactionId": tx_id, "index": 1},
                        "amount": outputs[1].value,
                        "scriptPublicKey": {"version": 0, "script": outputs[1].script_public_key.script},
                        "blockDaaScore": 0,
                        "isCoinbase": False,
                        "covenantId": None,
                    },
                )
            return tx_id


_sender = KaspaFaucetSender()


def get_faucet_sender() -> FaucetSender:
    return _sender


def _seconds_until_utc_midnight(now: datetime) -> int:
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


async def _release(redis: Redis, keys: list[str], day_key: str | None, amount: int) -> None:
    try:
        if keys:
            await redis.delete(*keys)
        if day_key is not None:
            await redis.decrby(day_key, amount)
    except RedisError:
        logger.error("faucet reservation rollback failed for %s", keys, exc_info=True)


@router.post("/claim", response_model=FaucetClaimResponse, tags=["faucet"])
async def claim(
    body: FaucetClaimRequest,
    request: Request,
    redis: Annotated[Redis, Depends(get_redis)],
    sender: Annotated[FaucetSender, Depends(get_faucet_sender)],
) -> FaucetClaimResponse | JSONResponse:
    settings = get_settings()
    if not settings.faucet_enabled:
        return _error(503, "faucet_unavailable", "the faucet is not enabled on this gateway")
    amount = settings.faucet_amount_sompi
    ip = client_ip(request)
    now = datetime.now(UTC)
    held: list[str] = []
    day_key = f"{DAY_KEY_PREFIX}{now:%Y-%m-%d}"
    day_reserved = False
    try:
        for key, what in ((f"{IP_KEY_PREFIX}{ip}", "client IP"), (f"{ADDRESS_KEY_PREFIX}{body.address}", "address")):
            if not await redis.set(key, "1", nx=True, ex=CLAIM_WINDOW_SECONDS):
                ttl = int(await redis.ttl(key))
                await _release(redis, held, None, 0)
                return _error(
                    429,
                    "rate_limited",
                    f"this {what} already claimed from the faucet in the last 24 hours",
                    headers={"Retry-After": str(ttl if ttl > 0 else CLAIM_WINDOW_SECONDS)},
                )
            held.append(key)
        total = int(await redis.incrby(day_key, amount))
        day_reserved = True
        await redis.expire(day_key, DAY_KEY_TTL_SECONDS)
        if total > settings.faucet_daily_cap_sompi:
            await _release(redis, held, day_key, amount)
            return _error(
                503,
                "faucet_unavailable",
                "faucet_exhausted: the daily faucet limit is reached, try again after 00:00 UTC",
                headers={"Retry-After": str(_seconds_until_utc_midnight(now))},
            )
    except RedisError:
        logger.error("faucet limiter unavailable", exc_info=True)
        await _release(redis, held, day_key if day_reserved else None, amount)
        return _error(503, "faucet_unavailable", "the faucet is temporarily unavailable")
    try:
        tx_id = await sender.send(body.address, amount)
    except Exception:
        logger.error("faucet send to %s failed; releasing its reservations", body.address, exc_info=True)
        await _release(redis, held, day_key, amount)
        return _error(503, "faucet_unavailable", "the faucet could not send right now, try again shortly")
    logger.info("faucet claim tx_id=%s address=%s amount_sompi=%d ip=%s", tx_id, body.address, amount, ip)
    return FaucetClaimResponse(tx_id=tx_id, amount_sompi=amount, address=body.address)
