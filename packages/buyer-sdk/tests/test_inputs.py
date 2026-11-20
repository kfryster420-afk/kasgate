"""CONTRACT v2.3 input selection: change is 0 or >= 30_000_000 sompi, never donated to the miner, <= 16 input addresses."""

from __future__ import annotations

import random

import kaspa
import pytest
from conftest import (
    MERCHANT,
    MIN_PRICE,
    NONCE,
    OTHER_KEY,
    PAYER,
    PAYER_KEY,
    TREASURY,
    FakeGateway,
    address_of,
    challenge,
    make_utxo,
)

import kasgate_client as sdk
from kasgate_client import transaction as tx_module
from kasgate_client.transaction import (
    build_payment_transaction,
    distinct_input_addresses,
)

KAS = 100_000_000
MERCHANT_SOMPI, FEE_SOMPI = 40_000_000, 20_000_000  # the minimum price 60_000_000
MIN_CHANGE = sdk.DEFAULT_MIN_CHANGE_SOMPI


def build(amounts: list[int], **kwargs):
    return build_payment_transaction(
        private_key=kaspa.PrivateKey(PAYER_KEY),
        network="kaspa:testnet-10",
        utxo_entries=[make_utxo(i, amount) for i, amount in enumerate(amounts)],
        pay_to=MERCHANT,
        fee_to=TREASURY,
        merchant_sompi=MERCHANT_SOMPI,
        fee_sompi=FEE_SOMPI,
        max_network_fee_sompi=5_000_000,
        payload=sdk.payment_payload(NONCE),
        **kwargs,
    )


def test_constants_follow_the_contract() -> None:
    assert MIN_CHANGE == 30_000_000
    assert sdk.MAX_INPUT_ADDRESSES == 16


@pytest.mark.parametrize(
    "amount", [90_300_000, 91_000_000, 100_000_000, 10 * KAS, 500 * KAS]
)
def test_single_utxo_with_large_enough_change_is_used_alone(amount: int) -> None:
    built = build([amount])
    assert built.change_sompi >= MIN_CHANGE and len(built.spent_outpoints) == 1
    assert (
        amount
        == MERCHANT_SOMPI + FEE_SOMPI + built.network_fee_sompi + built.change_sompi
    )


@pytest.mark.parametrize(
    "amount", [60_250_000, 60_300_000, 62_000_000, 80_000_000, 89_000_000, 90_000_000]
)
def test_single_utxo_that_would_leave_small_change_raises_dust_change(
    amount: int,
) -> None:
    with pytest.raises(sdk.DustChangeError) as caught:
        build([amount])
    assert isinstance(caught.value, sdk.InsufficientFundsError)


def test_small_change_is_never_emitted_for_any_balance() -> None:
    """Sweep the balance of a one-UTXO wallet across the whole dust window and beyond."""
    for amount in range(60_000_000, 100_000_001, 250_000):
        try:
            built = build([amount])
        except sdk.InsufficientFundsError:
            continue  # DustChangeError or plain insufficient funds: typed, nothing signed
        assert built.change_sompi == 0 or built.change_sompi >= MIN_CHANGE, amount
        assert built.network_fee_sompi <= 5_000_000, (
            amount
        )  # change is never silently turned into miner fee


def test_a_second_utxo_is_added_when_the_first_leaves_small_change() -> None:
    """62M alone leaves ~2M change (dust/storage mass); with 40M the change is ~41.6M."""
    built = build([62_000_000, 40_000_000])
    assert len(built.spent_outpoints) == 2
    assert built.change_sompi >= MIN_CHANGE


def test_utxo_order_does_not_matter() -> None:
    a = build([85_000_000, 10_000_000])
    b = build([10_000_000, 85_000_000])
    assert (
        a.change_sompi == b.change_sompi >= MIN_CHANGE and len(a.spent_outpoints) == 2
    )


def test_storage_mass_error_is_treated_as_add_an_input_and_retry(monkeypatch) -> None:
    errors: list[str] = []
    real_plan = tx_module._plan

    def spy(*args, **kwargs):
        try:
            return real_plan(*args, **kwargs)
        except Exception as exc:
            errors.append(str(exc))
            raise

    monkeypatch.setattr(tx_module, "_plan", spy)
    built = build(
        [66_000_000, 40_000_000]
    )  # 66M passes the quick funds check but leaves ~5.7M change
    assert any("Storage mass exceeds maximum" in e for e in errors), (
        "the first attempt must hit the mass limit"
    )
    assert len(built.spent_outpoints) == 2 and built.change_sompi >= MIN_CHANGE


def test_exhausted_wallet_raises_typed_error_instead_of_donating_change_to_the_miner() -> (
    None
):
    with pytest.raises(sdk.DustChangeError, match="Refusing to donate"):
        build([62_000_000])
    with pytest.raises(sdk.DustChangeError):
        build(
            [30_000_000, 30_000_000, 30_000_000]
        )  # 90M: change 29.6M after the miner fee


def test_insufficient_funds_is_still_insufficient_funds() -> None:
    with pytest.raises(sdk.InsufficientFundsError) as caught:
        build([10 * 1_000_000])
    assert not isinstance(caught.value, sdk.DustChangeError)


def test_random_wallets_never_produce_small_change() -> None:
    rng = random.Random(23)
    outcomes = {"ok": 0, "typed": 0}
    for _ in range(150):
        amounts = [
            rng.randint(5_000_000, 200_000_000) for _ in range(rng.randint(1, 5))
        ]
        try:
            built = build(amounts)
        except (sdk.InsufficientFundsError, sdk.TransactionBuildError):
            outcomes["typed"] += 1
            continue
        outcomes["ok"] += 1
        assert built.change_sompi == 0 or built.change_sompi >= MIN_CHANGE, amounts
    assert outcomes["ok"] > 20 and outcomes["typed"] > 0


async def test_client_raises_dust_change_before_anything_goes_on_the_wire(
    make_client,
) -> None:
    gateway = FakeGateway(challenge(MIN_PRICE))
    client = make_client(gateway, utxos=[make_utxo(0, 62_000_000)])
    with pytest.raises(sdk.DustChangeError):
        await client.get("/p/demo")
    assert gateway.signatures == []


async def test_client_pays_the_minimum_price_with_a_big_enough_second_utxo(
    make_client,
) -> None:
    gateway = FakeGateway(challenge(MIN_PRICE))
    client = make_client(
        gateway, utxos=[make_utxo(0, 62_000_000), make_utxo(1, 40_000_000)]
    )
    response = await client.get("/p/demo")
    assert response.status_code == 200
    tx = gateway.signatures[0]["tx"]
    assert len(tx.inputs) == 2 and tx.outputs[2].value >= MIN_CHANGE


# ---------------------------------------------------------------- the exact Generator call (confirmed by 02)


def test_generator_is_called_exactly_as_the_contract_specifies(monkeypatch) -> None:
    calls: list[dict] = []
    real = kaspa.Generator

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(kaspa, "Generator", spy)
    build([500 * KAS])
    kwargs = calls[0]
    assert set(kwargs) == {
        "entries",
        "change_address",
        "network_id",
        "outputs",
        "payload",
        "priority_fee",
    }
    assert kwargs["priority_fee"] == 0
    assert (
        kwargs["payload"]
        == sdk.payment_payload(NONCE)
        == b"kasgate:v1:" + NONCE.encode()
    )
    assert str(kwargs["network_id"]) == str(kaspa.NetworkId("testnet-10"))
    assert kwargs["change_address"].to_string() == PAYER
    assert len(kwargs["outputs"]) == 2 and len(kwargs["entries"]) == 1


def test_payload_helper_matches_the_gateway_byte_for_byte() -> None:
    from app.schemas import payment_payload as gateway_payload

    for nonce in (NONCE, "00" * 16, "ff" * 16):
        assert sdk.payment_payload(nonce) == gateway_payload(nonce)


# ---------------------------------------------------------------- <= 16 distinct input addresses


def other_utxos(count: int) -> list[dict]:
    keys = [f"{i + 1:064x}" for i in range(count)]
    return [
        make_utxo(100 + i, 5 * KAS, address=address_of(k)) for i, k in enumerate(keys)
    ]


def test_distinct_input_addresses_counts_script_owners() -> None:
    entries = [make_utxo(0, KAS), make_utxo(1, KAS), *other_utxos(16)]
    assert distinct_input_addresses(entries, "kaspa:testnet-10") == {
        PAYER,
        *(e["address"] for e in other_utxos(16)),
    }
    assert len(distinct_input_addresses(entries, "kaspa:testnet-10")) == 17


@pytest.mark.parametrize("foreign", [1, 15, 16, 17, 30])
def test_builder_refuses_utxos_that_are_not_the_signers(foreign: int) -> None:
    """Only the payer's key is available, so any foreign-address input is an error, and 17+ addresses are over the
    gateway's cap regardless."""
    with pytest.raises(sdk.TransactionBuildError, match="distinct input addresses"):
        build_payment_transaction(
            private_key=kaspa.PrivateKey(PAYER_KEY),
            network="kaspa:testnet-10",
            utxo_entries=[make_utxo(0, 500 * KAS), *other_utxos(foreign)],
            pay_to=MERCHANT,
            fee_to=TREASURY,
            merchant_sompi=MERCHANT_SOMPI,
            fee_sompi=FEE_SOMPI,
            max_network_fee_sompi=5_000_000,
            payload=sdk.payment_payload(NONCE),
        )


async def test_client_never_builds_from_foreign_utxos_even_if_the_provider_returns_them(
    make_client,
) -> None:
    gateway = FakeGateway(challenge(MIN_PRICE))
    client = make_client(gateway, utxos=[make_utxo(0, 500 * KAS), *other_utxos(17)])
    with pytest.raises(sdk.TransactionBuildError):
        await client.get("/p/demo")
    assert gateway.signatures == []


def test_normal_payment_uses_exactly_one_input_address() -> None:
    built = build([500 * KAS])
    assert built.payer_address == PAYER == address_of(PAYER_KEY)
    assert OTHER_KEY != PAYER_KEY


def test_known_limitation_two_equal_utxos_that_each_nearly_cover_the_payment_raise_a_typed_error() -> None:
    """kaspa.Generator stops after the first input when it alone covers the outputs, and then fails on the tiny change.
    With two equal ~60.3M UTXOs there is no smaller input to put first, so the SDK cannot force a two-input plan
    without either a non-zero priority fee (donating up to the fee cap, and leaving the contract's `priority_fee=0`
    call) or hand-computing the network fee for `create_transaction`. Both were rejected: the SDK fails closed instead.
    Wallets with any other mix, or one extra UTXO, are unaffected (see the tests above)."""
    with pytest.raises(sdk.TransactionBuildError):
        build([60_300_000, 60_300_000])
    built = build([60_300_000, 60_300_000, 40_000_000])  # one more input and the plan is found
    assert built.change_sompi >= MIN_CHANGE
