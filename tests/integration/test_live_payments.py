"""Live payment red-team. Needs KASGATE_TEST_PAYER_KEY (funded kaspa:testnet-10 key).

Three real payments are made (happy path, replay race, SDK end-to-end), each of one minimum price (~13.4 KAS) plus
miner fees, paid to a throw-away merchant address. Every tampered transaction is rejected before it reaches the node.
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from typing import Any

import httpx
import pytest
from conftest import RPC_URL, merchant_fee_outputs
from txforge import ForgedPayment

from kasgate_client import (
    KasGateClient,
    PaymentRequired,
    PaymentResponse,
    PriceCapExceededError,
    receipt_of,
)

pytestmark = [pytest.mark.live, pytest.mark.funded]

ATTACKER_ADDRESS = "kaspatest:qr0lr4ml9fn3chekrqmjdkergxl93l4wrk3dankcgvjq776s9wn9jhtkdksae"


def challenge_of(response: httpx.Response) -> PaymentRequired:
    return PaymentRequired.from_header(response.headers["PAYMENT-REQUIRED"])


async def send(url: str, header: str, *, method: str = "GET", client: httpx.AsyncClient | None = None) -> httpx.Response:
    if client is not None:
        return await client.request(method, url, headers={"PAYMENT-SIGNATURE": header})
    async with httpx.AsyncClient(timeout=60) as own:
        return await own.request(method, url, headers={"PAYMENT-SIGNATURE": header})


def assert_rejected(response: httpx.Response, *codes: str) -> None:
    assert response.status_code == 402, (response.status_code, response.text)
    assert response.json()["error"] in codes, response.text
    assert "PAYMENT-REQUIRED" in response.headers


# ---------------------------------------------------------------- one settled payment shared by several tests


@pytest.fixture(scope="module")
def settled(proxy_url, quote, forge) -> dict[str, Any]:
    forged = forge(merchant_fee_outputs(quote))
    response = asyncio.run(send(proxy_url, forged.header()))
    if response.status_code == 200:
        forge.commit(forged)
    return {"forged": forged, "response": response, "header": forged.header()}


def test_happy_path_returns_200_with_a_matching_settlement_receipt(settled, network) -> None:
    response: httpx.Response = settled["response"]
    forged: ForgedPayment = settled["forged"]
    assert response.status_code == 200, response.text
    receipt = PaymentResponse.from_header(response.headers["PAYMENT-RESPONSE"])
    assert receipt.settled is True
    assert receipt.txId == forged.tx_id
    assert receipt.network == network
    assert "PAYMENT-SIGNATURE" not in response.headers and "PAYMENT-REQUIRED" not in response.headers


def test_reusing_a_settled_tx_id_on_another_request_is_409(settled, proxy_url) -> None:
    assert settled["response"].status_code == 200
    for method, suffix in (("GET", "/other/path"), ("POST", "")):
        response = asyncio.run(send(proxy_url + suffix, settled["header"], method=method))
        assert response.status_code == 409, (method, response.status_code, response.text)
        assert response.json()["error"] == "payment_replayed"


def test_settlement_is_visible_to_the_merchant(settled, gateway_url, auth) -> None:
    assert settled["response"].status_code == 200
    rows = httpx.get(f"{gateway_url}/v1/settlements", headers=auth, timeout=15).json()
    match = [row for row in rows if row["tx_id"] == settled["forged"].tx_id]
    assert len(match) == 1 and match[0]["status"] == "settled"
    assert match[0]["amount_sompi"] == match[0]["merchant_amount_sompi"] + match[0]["fee_amount_sompi"]


async def test_concurrent_replay_of_one_signature_yields_exactly_one_200(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    header = forged.header()
    limits = httpx.Limits(max_connections=20)
    async with httpx.AsyncClient(timeout=90, limits=limits) as client:
        responses = await asyncio.gather(*(send(proxy_url, header, client=client) for _ in range(10)))
    statuses = Counter(r.status_code for r in responses)
    if statuses[200]:
        forge.commit(forged)
    assert statuses[200] == 1, statuses
    assert statuses[409] == 9, statuses
    for response in responses:
        if response.status_code == 409:
            assert response.json()["error"] == "payment_replayed"
        else:
            assert PaymentResponse.from_header(response.headers["PAYMENT-RESPONSE"]).txId == forged.tx_id


async def test_sdk_client_pays_end_to_end_and_enforces_the_cap(proxy_url, quote, payer_key, network, pool) -> None:
    pool.available(quote.total_sompi + 5_000_000)  # wait until the previous payments' change is spendable
    async with KasGateClient(payer_key, network=network, rpc_url=RPC_URL) as client:
        with pytest.raises(PriceCapExceededError):
            await client.get(proxy_url, max_price_kas="0.5")
        response = await client.get(proxy_url, max_price_kas="50")
    assert response.status_code == 200, response.text
    receipt = receipt_of(response)
    assert receipt is not None and receipt.amount_sompi == quote.total_sompi
    assert PaymentResponse.from_header(response.headers["PAYMENT-RESPONSE"]).txId == receipt.tx_id


# ---------------------------------------------------------------- tampering: nothing below reaches the node


def test_fee_output_one_sompi_short_is_invalid_fee_split(proxy_url, quote, forge) -> None:
    forged = forge([(quote.payTo, quote.merchant_sompi), (quote.platformFee.feeTo, quote.fee_sompi - 1)])
    assert_rejected(asyncio.run(send(proxy_url, forged.header())), "invalid_fee_split")


def test_fee_output_omitted_is_invalid_fee_split(proxy_url, quote, forge) -> None:
    forged = forge([(quote.payTo, quote.total_sompi)])
    assert_rejected(asyncio.run(send(proxy_url, forged.header())), "invalid_fee_split")


def test_swapped_output_order_is_invalid_fee_split(proxy_url, quote, forge) -> None:
    forged = forge([(quote.platformFee.feeTo, quote.fee_sompi), (quote.payTo, quote.merchant_sompi)])
    assert_rejected(asyncio.run(send(proxy_url, forged.header())), "invalid_fee_split")


def test_wrong_treasury_address_is_invalid_fee_split(proxy_url, quote, forge) -> None:
    forged = forge([(quote.payTo, quote.merchant_sompi), (ATTACKER_ADDRESS, quote.fee_sompi)])
    assert_rejected(asyncio.run(send(proxy_url, forged.header())), "invalid_fee_split")


def test_overpaying_the_fee_output_is_invalid_fee_split(proxy_url, quote, forge) -> None:
    forged = forge([(quote.payTo, quote.merchant_sompi), (quote.platformFee.feeTo, quote.fee_sompi + 1)])
    assert_rejected(asyncio.run(send(proxy_url, forged.header())), "invalid_fee_split")


def test_merchant_output_one_sompi_short_is_underpaid(proxy_url, quote, forge) -> None:
    forged = forge([(quote.payTo, quote.merchant_sompi - 1), (quote.platformFee.feeTo, quote.fee_sompi)])
    assert_rejected(asyncio.run(send(proxy_url, forged.header())), "underpaid")


def test_merchant_output_paid_to_a_third_party_is_invalid_fee_split(proxy_url, quote, forge) -> None:
    forged = forge([(ATTACKER_ADDRESS, quote.merchant_sompi), (quote.platformFee.feeTo, quote.fee_sompi)])
    assert_rejected(asyncio.run(send(proxy_url, forged.header())), "invalid_fee_split")


def test_forged_tx_id_is_rejected_with_402(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    response = asyncio.run(send(proxy_url, forged.header(txId="ab" * 32)))
    assert_rejected(response, "invalid_payment_header")


def test_payer_address_that_owns_no_input_is_rejected(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    response = asyncio.run(send(proxy_url, forged.header(payerAddress=ATTACKER_ADDRESS)))
    assert_rejected(response, "invalid_payment_header")


def test_mainnet_prefixed_payer_on_testnet_is_402(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    mainnet_payer = "kaspa:" + forged.payer_address.split(":", 1)[1]
    response = asyncio.run(send(proxy_url, forged.header(payerAddress=mainnet_payer)))
    assert_rejected(response, "wrong_network", "invalid_payment_header")


def test_tampered_embedded_utxo_amount_never_settles(proxy_url, quote, forge, settled) -> None:
    """inputs[].utxo is attacker-supplied; the node must still reject a tx whose sighash covered other values."""
    from txforge import tamper_raw_tx

    forged = forge(merchant_fee_outputs(quote))

    def inflate(data: dict) -> None:
        for entry in data["inputs"]:
            entry["utxo"]["amount"] = entry["utxo"]["amount"] * 1000

    raw = tamper_raw_tx(forged, inflate)
    response = asyncio.run(send(proxy_url, forged.header(rawTx=raw)))
    assert response.status_code != 200, response.text
    assert response.status_code in {402, 409}, (response.status_code, response.text)


def test_unsigned_transaction_never_settles(proxy_url, quote, forge) -> None:
    from txforge import tamper_raw_tx

    forged = forge(merchant_fee_outputs(quote))

    def strip(data: dict) -> None:
        for entry in data["inputs"]:
            entry["signatureScript"] = ""

    response = asyncio.run(send(proxy_url, forged.header(rawTx=tamper_raw_tx(forged, strip))))
    assert response.status_code == 402, (response.status_code, response.text)
    assert response.json()["error"] in {"tx_rejected", "invalid_payment_header"}


def test_rejected_signature_does_not_poison_the_replay_lock(proxy_url, quote, forge) -> None:
    """Presenting a tx id with a bad body must not stop the same id from being presented again (lock released)."""
    forged = forge(merchant_fee_outputs(quote))
    bad = forged.header(rawTx="{}")
    first = asyncio.run(send(proxy_url, bad))
    second = asyncio.run(send(proxy_url, bad))
    assert first.status_code == second.status_code == 402, (first.text, second.text)


@pytest.mark.skipif(os.environ.get("KASGATE_TEST_RPC_DOWN") != "1", reason="set KASGATE_TEST_RPC_DOWN=1 only when the gateway runs with an unreachable Kaspa node")
def test_node_outage_is_503_and_the_same_signature_can_be_retried(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    header = forged.header()
    first = asyncio.run(send(proxy_url, header))
    second = asyncio.run(send(proxy_url, header))
    for response in (first, second):
        assert response.status_code == 503, (response.status_code, response.text)
        assert response.json()["error"] == "rpc_unavailable"
    assert second.status_code != 409  # replay lock was released after the first failure


# ---------------------------------------------------------------- documented gaps (assert the secure behaviour)


@pytest.mark.xfail(strict=True, reason="GAP-1: PAYMENT-SIGNATURE is a bearer token; nothing binds the tx to the request, so a mempool observer can front-run it")
def test_payment_is_bound_to_the_request_that_was_challenged(proxy_url, quote, forge, gateway_url, endpoint) -> None:
    forged = forge(merchant_fee_outputs(quote))
    header = forged.header()
    thief = asyncio.run(send(proxy_url + "/thief/path", header, method="POST"))
    victim = asyncio.run(send(proxy_url, header))
    if thief.status_code == 200:
        forge.commit(forged)
    assert not (thief.status_code == 200 and victim.status_code == 409), (
        "a third party presenting the victim's signed tx first got the resource; the victim paid and got 409"
    )
