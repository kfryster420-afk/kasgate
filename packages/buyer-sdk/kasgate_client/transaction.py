"""Single-transaction payment builder on top of kaspa==2.1.0.

API facts verified by introspection against kaspa 2.1.0:
  * `create_transaction()` has no change address: every sompi not assigned to an output becomes miner fee, so it is
    unsafe for payments and is NOT used.
  * `Generator` keeps the caller's output order and appends the change output last, so outputs[0] = merchant,
    outputs[1] = treasury, outputs[2] = change (when present). The builder still re-verifies the layout.
  * `Generator` may emit several chained "batch" transactions for fragmented wallets; the gateway accepts exactly one
    transaction, so more than one pending transaction is an error.
  * The transaction id does not cover signature scripts, so `tx.id` is identical before and after signing.
  * CONTRACT v2 nonce binding: `Generator(..., payload=<bytes>)` sets the transaction payload. `bytes` are used
    verbatim; a `str` is interpreted as HEX (so never pass the text form). The payload is covered by the tx id, is
    exposed as `Transaction.payload` (lowercase hex str, '' when unset) and `Transaction.to_dict()['payload']`, and
    costs ~200 sompi of mass per byte. The builder passes `kasgate:v1:<nonce>` as bytes and re-reads it from the
    signed transaction before returning.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import kaspa

from .constants import DEFAULT_MIN_OUTPUT_SOMPI, NETWORK_ID
from .exceptions import (
    InsufficientFundsError,
    NetworkFeeTooHighError,
    TransactionBuildError,
)

Outpoint = tuple[str, int]


@dataclass(frozen=True)
class BuiltPayment:
    tx_id: str
    raw_tx: str
    payer_address: str
    merchant_sompi: int
    fee_sompi: int
    network_fee_sompi: int
    change_sompi: int
    spent_outpoints: tuple[Outpoint, ...]
    change_entry: dict[str, Any] | None


def entry_amount(raw: dict[str, Any]) -> int:
    inner = raw.get("utxoEntry", raw)
    return int(inner["amount"])


def entry_outpoint(raw: dict[str, Any]) -> Outpoint:
    out = raw["outpoint"]
    return str(out["transactionId"]), int(out["index"])


def _script_of(address: str) -> str:
    return kaspa.pay_to_address_script(kaspa.Address(address)).script


def _generate_with_growing_prefix(
    ordered: Sequence[dict[str, Any]],
    payer: kaspa.Address,
    network: str,
    pay_to_addr: kaspa.Address,
    fee_to_addr: kaspa.Address,
    merchant_sompi: int,
    fee_sompi: int,
    max_network_fee_sompi: int,
    payer_str: str,
    payload: bytes,
) -> list[kaspa.PendingTransaction]:
    needed = merchant_sompi + fee_sompi
    running = 0
    count = 0
    for count, raw in enumerate(ordered, start=1):
        running += entry_amount(raw)
        if running >= needed + max_network_fee_sompi:
            break
    last_error: Exception | None = None
    while count <= len(ordered):
        try:
            references = [
                kaspa.UtxoEntryReference.from_dict(e) for e in ordered[:count]
            ]
            generator = kaspa.Generator(
                entries=references,
                change_address=payer,
                network_id=kaspa.NetworkId(NETWORK_ID[network]),
                outputs=[
                    kaspa.PaymentOutput(pay_to_addr, merchant_sompi),
                    kaspa.PaymentOutput(fee_to_addr, fee_sompi),
                ],
                priority_fee=0,
                payload=payload,
            )
            pending: list[kaspa.PendingTransaction] = []
            while (item := next(generator, None)) is not None:
                pending.append(item)
            if len(pending) == 1:
                return pending
            last_error = TransactionBuildError(
                f"payment needs {len(pending)} chained transactions"
            )
        except Exception as exc:
            if "Insufficient funds" in str(exc) and count == len(ordered):
                raise InsufficientFundsError(
                    f"{payer_str} cannot fund {needed} sompi plus network fee"
                ) from exc
            last_error = exc
        count += 1
    raise TransactionBuildError(
        f"payment needs several chained transactions or exceeds storage mass (fragmented UTXOs); "
        f"consolidate the wallet first ({last_error})"
    )


def build_payment_transaction(
    *,
    private_key: kaspa.PrivateKey,
    network: str,
    utxo_entries: Sequence[dict[str, Any]],
    pay_to: str,
    fee_to: str,
    merchant_sompi: int,
    fee_sompi: int,
    max_network_fee_sompi: int,
    payload: bytes,
    min_output_sompi: int = DEFAULT_MIN_OUTPUT_SOMPI,
) -> BuiltPayment:
    """Build and sign ONE transaction paying `merchant_sompi` to pay_to and `fee_sompi` to fee_to.

    `utxo_entries` are RPC-style (nested `utxoEntry`) or flat dicts accepted by `UtxoEntryReference.from_dict`.
    """
    network_type = "mainnet" if network == "kaspa:mainnet" else "testnet"
    if not isinstance(payload, bytes) or not payload:
        raise TransactionBuildError(
            "payload must be the non-empty challenge payload as bytes"
        )
    if merchant_sompi < min_output_sompi or fee_sompi < min_output_sompi:
        raise TransactionBuildError(
            f"refusing to build outputs {merchant_sompi}/{fee_sompi} below the {min_output_sompi} sompi floor"
        )
    payer = private_key.to_address(network_type)
    payer_str = payer.to_string()
    try:
        pay_to_addr = kaspa.Address(pay_to)
        fee_to_addr = kaspa.Address(fee_to)
    except Exception as exc:
        raise TransactionBuildError(
            f"recipient address rejected by the Kaspa SDK: {exc}"
        ) from exc

    ordered = sorted(utxo_entries, key=entry_amount, reverse=True)
    if not ordered:
        raise InsufficientFundsError(f"no spendable UTXOs for {payer_str}")
    needed = merchant_sompi + fee_sompi
    if sum(entry_amount(e) for e in ordered) < needed:
        raise InsufficientFundsError(
            f"balance of {payer_str} is below the required {needed} sompi"
        )

    # Generator does not minimise inputs: handed a large wallet it happily chains many small UTXOs, which bloats
    # rawTx past real-world header limits. Feed it the smallest prefix of the largest UTXOs that can pay, and widen
    # the prefix only if the generator itself reports a storage-mass problem.
    pending = _generate_with_growing_prefix(
        ordered,
        payer,
        network,
        pay_to_addr,
        fee_to_addr,
        merchant_sompi,
        fee_sompi,
        max_network_fee_sompi,
        payer_str,
        payload,
    )

    if len(pending) != 1:
        raise TransactionBuildError(
            f"payment needs {len(pending)} chained transactions (fragmented UTXOs); consolidate the wallet first"
        )
    item = pending[0]
    if item.transaction_type != "final":
        raise TransactionBuildError(
            "generator produced a batch transaction instead of a final payment"
        )
    if item.fee_amount > max_network_fee_sompi:
        raise NetworkFeeTooHighError(item.fee_amount, max_network_fee_sompi)

    try:
        item.sign([private_key], True)
    except Exception as exc:
        raise TransactionBuildError(f"signing failed: {exc}") from exc

    tx = item.transaction
    outputs = tx.outputs
    expected = [(merchant_sompi, _script_of(pay_to)), (fee_sompi, _script_of(fee_to))]
    if len(outputs) not in (2, 3):
        raise TransactionBuildError(f"unexpected output count {len(outputs)}")
    for index, (value, script) in enumerate(expected):
        if (
            outputs[index].value != value
            or outputs[index].script_public_key.script != script
        ):
            raise TransactionBuildError(
                f"output {index} does not match the required payment"
            )
    change_value = 0
    if len(outputs) == 3:
        change_value = outputs[2].value
        if outputs[2].script_public_key.script != _script_of(payer_str):
            raise TransactionBuildError(
                "change output does not return to the payer address"
            )

    if bytes.fromhex(tx.payload) != payload:
        raise TransactionBuildError(
            "signed transaction does not carry the challenge payload"
        )
    if any(o.value < min_output_sompi for o in outputs[:2]):
        raise TransactionBuildError(
            "a payment output is below the minimum output floor"
        )

    recomputed = str(kaspa.Transaction.from_dict(tx.to_dict()).finalize())
    if recomputed != tx.id:
        raise TransactionBuildError("transaction id does not match its recomputed hash")

    spent = tuple(
        (str(i.previous_outpoint.transaction_id), int(i.previous_outpoint.index))
        for i in tx.inputs
    )
    change_entry = None
    if change_value:
        change_entry = {
            "address": payer_str,
            "outpoint": {"transactionId": tx.id, "index": 2},
            "amount": change_value,
            "scriptPublicKey": {
                "version": 0,
                "script": outputs[2].script_public_key.script,
            },
            "blockDaaScore": 0,
            "isCoinbase": False,
            "covenantId": None,
        }
    return BuiltPayment(
        tx_id=tx.id,
        raw_tx=json.dumps(tx.to_dict()),
        payer_address=payer_str,
        merchant_sompi=merchant_sompi,
        fee_sompi=fee_sompi,
        network_fee_sompi=item.fee_amount,
        change_sompi=change_value,
        spent_outpoints=spent,
        change_entry=change_entry,
    )
