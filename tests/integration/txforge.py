"""Low-level transaction forge for red-team tests.

Deliberately independent of kasgate_client's builder and its safety checks: it signs ANY output list the caller
asks for (wrong order, short fee, wrong treasury, missing outputs) so the gateway's verifier is the only defence.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import kaspa
from kasgate_client import PaymentSignature, payment_payload

MIN_CHANGE_SOMPI = 30_000_000  # CONTRACT v2.3: change is 0 or >= 0.3 KAS, else the tx is non-standard (storage mass)
NETWORK_IDS = {"kaspa:testnet-10": "testnet-10", "kaspa:mainnet": "mainnet"}


@dataclass(frozen=True)
class ForgedPayment:
    tx_id: str
    raw_tx: str
    payer_address: str
    tx: kaspa.Transaction
    spent_outpoints: tuple[tuple[str, int], ...] = ()
    nonce: str | None = None
    payload: bytes = b""

    def signature(self, **overrides: str) -> PaymentSignature:
        """PAYMENT-SIGNATURE body; `nonce` defaults to the one the tx payload was forged for."""
        fields = {"txId": self.tx_id, "rawTx": self.raw_tx, "payerAddress": self.payer_address, "nonce": self.nonce}
        fields.update(overrides)
        if fields["nonce"] is None:
            raise ValueError("forge_payment(..., nonce=...) or header(nonce=...) is required (CONTRACT v2)")
        return PaymentSignature(**fields)

    def header(self, **overrides: str) -> str:
        return self.signature(**overrides).to_header()


async def fetch_utxos(address: str, network: str, rpc_url: str | None) -> list[dict[str, Any]]:
    network_id = kaspa.NetworkId(NETWORK_IDS[network])
    rpc = (
        kaspa.RpcClient(url=rpc_url, network_id=network_id)
        if rpc_url
        else kaspa.RpcClient(resolver=kaspa.Resolver(), network_id=network_id)
    )
    await rpc.connect(timeout_duration=15_000, retry_interval=500, strategy="fallback")
    try:
        response = await rpc.get_utxos_by_addresses({"addresses": [address]})
        return list(response["entries"])
    finally:
        await rpc.disconnect()


def fetch_utxos_sync(address: str, network: str, rpc_url: str | None) -> list[dict[str, Any]]:
    """Blocking UTXO fetch that also works when called from inside a running event loop (async tests)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(fetch_utxos(address, network, rpc_url))
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, fetch_utxos(address, network, rpc_url)).result()


def _amount(entry: dict[str, Any]) -> int:
    return int(entry.get("utxoEntry", entry)["amount"])


def forge_payment(
    *,
    private_key_hex: str,
    network: str,
    utxos: Sequence[dict[str, Any]],
    outputs: Sequence[tuple[str, int]],
    priority_fee: int = 0,
    nonce: str | None = None,
    payload: bytes | None = None,
) -> ForgedPayment:
    """Sign a transaction paying exactly `outputs` (in that order) from the key's UTXOs; change goes last.

    CONTRACT v2: `nonce` is the challenge nonce echoed in PAYMENT-SIGNATURE and, unless `payload` overrides it, the
    transaction payload becomes `kasgate:v1:<nonce>`. Pass `payload=b""` to forge a tx without any payload, or a
    different byte string to forge a mismatched one.

    The largest UTXO that covers the outputs is used alone, so a rejected forgery never touches more funds than
    it has to (and none at all unless the gateway wrongly submits it).
    """
    key = kaspa.PrivateKey(private_key_hex)
    payer = key.to_address("mainnet" if network == "kaspa:mainnet" else "testnet")
    total = sum(amount for _, amount in outputs)
    candidates = sorted(utxos, key=_amount, reverse=True)
    chosen = next((u for u in candidates if _amount(u) >= total + 5_000_000 + MIN_CHANGE_SOMPI), None)
    if chosen is None:
        raise RuntimeError(f"no single UTXO of {payer.to_string()} covers {total} sompi plus fees and {MIN_CHANGE_SOMPI} change")
    generator = kaspa.Generator(
        entries=[kaspa.UtxoEntryReference.from_dict(chosen)],
        change_address=payer,
        network_id=kaspa.NetworkId(NETWORK_IDS[network]),
        outputs=[kaspa.PaymentOutput(kaspa.Address(address), amount) for address, amount in outputs],
        priority_fee=priority_fee,
        payload=payload if payload is not None else (payment_payload(nonce) if nonce else b""),
    )
    pending = next(generator)
    pending.sign([key], True)
    tx = pending.transaction
    spent = tuple((str(i.previous_outpoint.transaction_id), int(i.previous_outpoint.index)) for i in tx.inputs)
    return ForgedPayment(
        tx_id=tx.id,
        raw_tx=json.dumps(tx.to_dict()),
        payer_address=payer.to_string(),
        tx=tx,
        spent_outpoints=spent,
        nonce=nonce,
        payload=bytes.fromhex(tx.payload),
    )


def tamper_raw_tx(forged: ForgedPayment, mutate) -> str:
    """Return json.dumps of the forged tx dict after `mutate(dict)`; the signature and id are left untouched."""
    data = json.loads(forged.raw_tx)
    mutate(data)
    return json.dumps(data)


def synthetic_utxo(address: str, index: int = 0, amount: int = 500 * 100_000_000) -> dict[str, Any]:
    """A UTXO entry that exists nowhere but in the test: enough for every check the gateway makes BEFORE it asks the node."""
    script = kaspa.pay_to_address_script(kaspa.Address(address)).script
    return {
        "address": address,
        "outpoint": {"transactionId": f"{0xCD:02x}" * 30 + f"{index:04x}", "index": 0},
        "amount": amount,
        "scriptPublicKey": {"version": 0, "script": script},
        "blockDaaScore": 1000,
        "isCoinbase": False,
        "covenantId": None,
    }


class SyntheticPool:
    """Stands in for `UtxoPool` when no funded key is configured: one big made-up UTXO, never spent on any chain."""

    def __init__(self, address: str) -> None:
        self.address = address

    def available(self, minimum: int, timeout: float = 0.0) -> list[dict[str, Any]]:
        return [synthetic_utxo(self.address, amount=max(minimum * 10, 500 * 100_000_000))]

    def mark_spent(self, forged: ForgedPayment) -> None:
        return None


class UtxoPool:
    """Confirmed UTXOs of one address minus outpoints this test session already spent.

    Real payments take a few seconds to confirm, so after a settled payment the next forge polls until the node
    shows a UTXO that has not been spent by this session yet.
    """

    def __init__(self, address: str, network: str, rpc_url: str | None) -> None:
        self.address = address
        self.network = network
        self.rpc_url = rpc_url
        self.spent: set[tuple[str, int]] = set()

    def available(self, minimum: int, timeout: float = 60.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while True:
            entries = fetch_utxos_sync(self.address, self.network, self.rpc_url)
            usable = [
                e
                for e in entries
                if (str(e["outpoint"]["transactionId"]), int(e["outpoint"]["index"])) not in self.spent
            ]
            if any(_amount(e) >= minimum for e in usable):
                return usable
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"{self.address} has no unspent UTXO >= {minimum} sompi (fund the payer from a testnet faucet)"
                )
            time.sleep(2)

    def mark_spent(self, forged: ForgedPayment) -> None:
        self.spent.update(forged.spent_outpoints)


def forge_exact(
    *,
    private_keys: Sequence[str],
    network: str,
    utxos: Sequence[dict[str, Any]],
    outputs: Sequence[tuple[str, int]],
    payer_address: str,
    nonce: str | None = None,
    payload: bytes | None = None,
) -> ForgedPayment:
    """Sign a transaction that spends ALL `utxos` and pays EXACTLY `outputs` (change included, in that order).

    Whatever the inputs hold beyond the outputs becomes the miner fee. Unlike `forge_payment` this goes through
    `kaspa.create_transaction`, so the caller controls every output (small change, many inputs) and may sign inputs
    that belong to several keys. `kaspa.Generator` would refuse (storage mass) or reorder such shapes.
    """
    keys = [kaspa.PrivateKey(k) for k in private_keys]
    data = payload if payload is not None else (payment_payload(nonce) if nonce else None)
    tx = kaspa.create_transaction(
        [kaspa.UtxoEntryReference.from_dict(u) for u in utxos],
        [kaspa.PaymentOutput(kaspa.Address(address), amount) for address, amount in outputs],
        0,
        data,
    )
    signed = kaspa.sign_transaction(tx, keys, False)
    spent = tuple((str(i.previous_outpoint.transaction_id), int(i.previous_outpoint.index)) for i in signed.inputs)
    return ForgedPayment(
        tx_id=signed.id,
        raw_tx=json.dumps(signed.to_dict()),
        payer_address=payer_address,
        tx=signed,
        spent_outpoints=spent,
        nonce=nonce,
        payload=bytes.fromhex(signed.payload),
    )
