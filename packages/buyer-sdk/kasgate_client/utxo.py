"""UTXO sourcing with a local ledger.

`RpcClient.get_utxos_by_addresses` reports the confirmed UTXO set only. After a payment is accepted its inputs are
still listed until the next block and its change output is not listed yet, so back-to-back payments would double
spend. The ledger hides outpoints we already spent (or that may have been spent when a request failed ambiguously)
and adds our own unconfirmed change outputs, each for a bounded time.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import kaspa

from .constants import NETWORK_ID
from .exceptions import RpcError
from .transaction import BuiltPayment, Outpoint, entry_outpoint

UtxoProvider = Callable[[str], Awaitable[list[dict[str, Any]]]]


class UtxoLedger:
    def __init__(
        self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._spent: dict[Outpoint, float] = {}
        self._change: dict[Outpoint, tuple[dict[str, Any], float]] = {}

    def _prune(self) -> None:
        now = self._clock()
        self._spent = {k: exp for k, exp in self._spent.items() if exp > now}
        self._change = {k: v for k, v in self._change.items() if v[1] > now}

    def available(self, confirmed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._prune()
        seen: set[Outpoint] = set()
        usable: list[dict[str, Any]] = []
        for raw in confirmed:
            outpoint = entry_outpoint(raw)
            seen.add(outpoint)
            if outpoint not in self._spent:
                usable.append(raw)
        for outpoint, (entry, _) in self._change.items():
            if outpoint not in seen and outpoint not in self._spent:
                usable.append(entry)
        return usable

    def mark_spent(self, built: BuiltPayment) -> None:
        """Payment accepted (or possibly accepted): hide its inputs and expose its change."""
        expiry = self._clock() + self._ttl
        for outpoint in built.spent_outpoints:
            self._spent[outpoint] = expiry
            self._change.pop(outpoint, None)
        if built.change_entry is not None:
            self._change[(built.tx_id, 2)] = (built.change_entry, expiry)

    def quarantine(self, built: BuiltPayment) -> None:
        """Outcome unknown: hide the inputs but do not trust the change output."""
        expiry = self._clock() + self._ttl
        for outpoint in built.spent_outpoints:
            self._spent[outpoint] = expiry
            self._change.pop(outpoint, None)


class RpcUtxoProvider:
    """Fetches confirmed UTXOs through a lazily connected wRPC client bound to the running event loop."""

    def __init__(
        self, network: str, rpc_url: str | None, connect_timeout_ms: int = 10_000
    ) -> None:
        self._network = network
        self._rpc_url = rpc_url
        self._timeout = connect_timeout_ms
        self._rpc: kaspa.RpcClient | None = None

    async def _client(self) -> kaspa.RpcClient:
        if self._rpc is None:
            network_id = kaspa.NetworkId(NETWORK_ID[self._network])
            if self._rpc_url:
                rpc = kaspa.RpcClient(url=self._rpc_url, network_id=network_id)
            else:
                rpc = kaspa.RpcClient(resolver=kaspa.Resolver(), network_id=network_id)
            try:
                await rpc.connect(
                    timeout_duration=self._timeout,
                    retry_interval=500,
                    strategy="fallback",
                )
            except Exception as exc:
                raise RpcError(f"cannot connect to a Kaspa node: {exc}") from exc
            self._rpc = rpc
        return self._rpc

    async def __call__(self, address: str) -> list[dict[str, Any]]:
        rpc = await self._client()
        try:
            response = await rpc.get_utxos_by_addresses({"addresses": [address]})
        except Exception as exc:
            raise RpcError(f"get_utxos_by_addresses failed: {exc}") from exc
        return list(response["entries"])

    async def aclose(self) -> None:
        if self._rpc is not None:
            rpc, self._rpc = self._rpc, None
            try:
                await rpc.disconnect()
            except Exception:
                pass
