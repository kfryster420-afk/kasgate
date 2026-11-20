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

from .constants import (
    DEFAULT_MIN_CHANGE_SOMPI,
    DEFAULT_MIN_OUTPUT_SOMPI,
    MAX_INPUT_ADDRESSES,
    NETWORK_ID,
)
from .exceptions import (
    DustChangeError,
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


def _change_of(item: kaspa.PendingTransaction) -> int:
    outputs = item.transaction.outputs
    return int(outputs[2].value) if len(outputs) > 2 else 0


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
    min_change_sompi: int,
) -> list[kaspa.PendingTransaction]:
    """Feed `kaspa.Generator` the smallest prefix of the largest UTXOs that yields ONE transaction whose change is
    either 0 or >= `min_change_sompi` (CONTRACT v2.3). A storage-mass error, a chained (multi-tx) plan or small change
    all mean "add another input and retry"; small change is never emitted and never donated to the miner."""
    needed = merchant_sompi + fee_sompi
    balance = sum(entry_amount(e) for e in ordered)
    running = 0
    count = 0
    for count, raw in enumerate(ordered, start=1):
        running += entry_amount(raw)
        if running >= needed + max_network_fee_sompi:
            break
    last_error: Exception | None = None
    while count <= len(ordered):
        chosen = list(ordered[:count])  # the `count` largest UTXOs
        # kaspa.Generator consumes entries in the order given and stops as soon as they cover the outputs, so a
        # first UTXO that alone leaves tiny change makes it fail instead of adding the next one. Offering the same set
        # smallest-first forces it to spend the whole set when the small ones cannot pay on their own.
        for candidate in (sorted(chosen, key=entry_amount), chosen):
            try:
                pending = _plan(
                    candidate,
                    payer,
                    network,
                    pay_to_addr,
                    fee_to_addr,
                    merchant_sompi,
                    fee_sompi,
                    payload,
                )
            except Exception as exc:
                if "Insufficient funds" in str(exc) and count == len(ordered):
                    raise InsufficientFundsError(
                        f"{payer_str} cannot fund {needed} sompi plus network fee"
                    ) from exc
                last_error = exc  # includes "Storage mass exceeds maximum": add an input and retry
                continue
            if len(pending) != 1:
                last_error = TransactionBuildError(
                    f"payment needs {len(pending)} chained transactions"
                )
                continue
            change = _change_of(pending[0])
            if 0 < change < min_change_sompi:
                last_error = DustChangeError(
                    f"change of {change} sompi is below {min_change_sompi}; adding inputs"
                )
                continue
            return pending
        count += 1
    if balance - needed - max_network_fee_sompi < min_change_sompi:
        raise DustChangeError(
            f"{payer_str} holds {balance} sompi: paying {needed} sompi would leave change below {min_change_sompi} "
            "sompi (CONTRACT v2.3 forbids small change; add funds). Refusing to donate it to the miner fee"
        ) from last_error
    raise TransactionBuildError(
        f"payment needs several chained transactions or exceeds storage mass (fragmented UTXOs); "
        f"consolidate the wallet first ({last_error})"
    )


def _plan(
    entries: Sequence[dict[str, Any]],
    payer: kaspa.Address,
    network: str,
    pay_to_addr: kaspa.Address,
    fee_to_addr: kaspa.Address,
    merchant_sompi: int,
    fee_sompi: int,
    payload: bytes,
) -> list[kaspa.PendingTransaction]:
    """The exact `kaspa.Generator` call of the contract: priority_fee is required and payload binds the nonce."""
    generator = kaspa.Generator(
        entries=[kaspa.UtxoEntryReference.from_dict(e) for e in entries],
        change_address=payer,
        network_id=kaspa.NetworkId(NETWORK_ID[network]),
        outputs=[
            kaspa.PaymentOutput(pay_to_addr, merchant_sompi),
            kaspa.PaymentOutput(fee_to_addr, fee_sompi),
        ],
        payload=payload,
        priority_fee=0,
    )
    pending: list[kaspa.PendingTransaction] = []
    while (item := next(generator, None)) is not None:
        pending.append(item)
    return pending


def distinct_input_addresses(
    entries: Sequence[dict[str, Any]], network: str
) -> set[str]:
    """Addresses that own the given UTXO entries, derived from their locking scripts."""
    network_type = "mainnet" if network == "kaspa:mainnet" else "testnet"
    owners: set[str] = set()
    for raw in entries:
        inner = raw.get("utxoEntry", raw)
        script = inner["scriptPublicKey"]
        script_hex = script["script"] if isinstance(script, dict) else str(script)
        version = int(script["version"]) if isinstance(script, dict) else 0
        try:
            spk = kaspa.ScriptPublicKey(version, script_hex)
            owner = kaspa.address_from_script_public_key(spk, network_type)
        except Exception as exc:
            raise TransactionBuildError(
                f"UTXO locked by a script with no standard address: {exc}"
            ) from exc
        owners.add(owner.to_string())
    return owners


def _spent_entries(
    tx: kaspa.Transaction, ordered: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_outpoint = {entry_outpoint(e): e for e in ordered}
    spent = []
    for item in tx.inputs:
        key = (
            str(item.previous_outpoint.transaction_id),
            int(item.previous_outpoint.index),
        )
        if key not in by_outpoint:
            raise TransactionBuildError(
                "transaction spends an outpoint that was not offered to the builder"
            )
        spent.append(by_outpoint[key])
    return spent


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
    min_change_sompi: int = DEFAULT_MIN_CHANGE_SOMPI,
) -> BuiltPayment:
    """Build and sign ONE transaction paying `merchant_sompi` to pay_to and `fee_sompi` to fee_to.

    `utxo_entries` are RPC-style (nested `utxoEntry`) or flat dicts accepted by `UtxoEntryReference.from_dict`.
    """
    network_type = "mainnet" if network == "kaspa:mainnet" else "testnet"
    if not isinstance(payload, bytes) or not payload:
        raise TransactionBuildError(
            "payload must be the non-empty challenge payload as bytes"
        )
    if merchant_sompi < 2 * min_output_sompi or fee_sompi < min_output_sompi:
        raise TransactionBuildError(
            f"refusing to build outputs {merchant_sompi}/{fee_sompi}: merchant must be >= {2 * min_output_sompi} "
            f"and fee >= {min_output_sompi} sompi"
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
    owners = distinct_input_addresses(ordered, network)
    if owners != {payer_str}:
        raise TransactionBuildError(
            f"{len(owners)} distinct input addresses supplied; only UTXOs of {payer_str} can be signed "
            f"(the gateway rejects payments from more than {MAX_INPUT_ADDRESSES} addresses)"
        )
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
        min_change_sompi,
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
