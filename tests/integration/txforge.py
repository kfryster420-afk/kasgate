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
from kasgate_client import PaymentSignature

NETWORK_IDS = {"kaspa:testnet-10": "testnet-10", "kaspa:mainnet": "mainnet"}


@dataclass(frozen=True)
class ForgedPayment:
    tx_id: str
    raw_tx: str
    payer_address: str
    tx: kaspa.Transaction
    spent_outpoints: tuple[tuple[str, int], ...] = ()

    def signature(self, **overrides: str) -> PaymentSignature:
        fields = {"txId": self.tx_id, "rawTx": self.raw_tx, "payerAddress": self.payer_address}
        fields.update(overrides)
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
) -> ForgedPayment:
    """Sign a transaction paying exactly `outputs` (in that order) from the key's UTXOs; change goes last.

    The largest UTXO that covers the outputs is used alone, so a rejected forgery never touches more funds than
    it has to (and none at all unless the gateway wrongly submits it).
    """
    key = kaspa.PrivateKey(private_key_hex)
    payer = key.to_address("mainnet" if network == "kaspa:mainnet" else "testnet")
    total = sum(amount for _, amount in outputs)
    candidates = sorted(utxos, key=_amount, reverse=True)
    chosen = next((u for u in candidates if _amount(u) >= total + 5_000_000), None)
    if chosen is None:
        raise RuntimeError(f"no single UTXO of {payer.to_string()} covers {total} sompi plus fees")
    generator = kaspa.Generator(
        entries=[kaspa.UtxoEntryReference.from_dict(chosen)],
        change_address=payer,
        network_id=kaspa.NetworkId(NETWORK_IDS[network]),
        outputs=[kaspa.PaymentOutput(kaspa.Address(address), amount) for address, amount in outputs],
        priority_fee=priority_fee,
    )
    pending = next(generator)
    pending.sign([key], True)
    tx = pending.transaction
    spent = tuple((str(i.previous_outpoint.transaction_id), int(i.previous_outpoint.index)) for i in tx.inputs)
    return ForgedPayment(
        tx_id=tx.id, raw_tx=json.dumps(tx.to_dict()), payer_address=payer.to_string(), tx=tx, spent_outpoints=spent
    )


def tamper_raw_tx(forged: ForgedPayment, mutate) -> str:
    """Return json.dumps of the forged tx dict after `mutate(dict)`; the signature and id are left untouched."""
    data = json.loads(forged.raw_tx)
    mutate(data)
    return json.dumps(data)


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
