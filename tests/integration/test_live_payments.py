"""Live payment red-team against a running gateway (CONTRACT v2.x: nonce-bound payments).

Two tiers:

* **No funds needed.** Everything the gateway refuses BEFORE it asks the Kaspa node: tampered outputs, the nonce and
  payload binding, `payment_expired`, the 16-address cap. These transactions spend made-up UTXOs, so they can never
  settle, and they run against any gateway that is up (even one without a node).
* **Funded** (`KASGATE_TEST_PAYER_KEY` + a gateway with `kaspa_rpc=true`). Real settlements at the minimum price
  (0.6 KAS plus miner fees each), paid to a throw-away merchant address, and the cases that need the node's own view
  of the inputs (embedded-UTXO tampering, unsigned inputs, outage handling, redelivery).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from kasgate_client import (
    KasGateClient,
    PaymentRequired,
    PaymentResponse,
    PriceCapExceededError,
    receipt_of,
)

from conftest import RPC_URL, address_for_key, create_endpoint, fresh_quote, merchant_fee_outputs
from txforge import ForgedPayment, forge_exact, make_payer_proof, synthetic_utxo

pytestmark = pytest.mark.live
funded = pytest.mark.funded

ATTACKER_ADDRESS = "kaspatest:qr0lr4ml9fn3chekrqmjdkergxl93l4wrk3dankcgvjq776s9wn9jhtkdksae"
MAX_INPUT_ADDRESSES = 16
FAILING_UPSTREAM_URL = os.environ.get("KASGATE_TEST_FAILING_UPSTREAM_URL", "https://httpbin.org/status/503")


def challenge_of(response: httpx.Response) -> PaymentRequired:
    return PaymentRequired.from_header(response.headers["PAYMENT-REQUIRED"])


def _trace(header: str, response: httpx.Response, elapsed: float, method: str, url: str) -> None:
    """Append one JSON line per presented payment to $KASGATE_TEST_TXLOG (txId, status, latency) when it is set."""
    path = os.environ.get("KASGATE_TEST_TXLOG")
    if not path:
        return
    from kasgate_client import PaymentSignature

    try:
        signature = PaymentSignature.from_header(header)
        tx_id, nonce = signature.txId, signature.nonce
    except ValueError:
        tx_id = nonce = None
    error = None
    try:
        error = response.json().get("error")
    except ValueError:
        pass
    with open(path, "a") as handle:
        handle.write(
            json.dumps(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "test": os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0],
                    "method": method,
                    "url": url,
                    "txId": tx_id,
                    "nonce": nonce,
                    "status": response.status_code,
                    "error": error,
                    "seconds": round(elapsed, 3),
                }
            )
            + "\n"
        )


async def send(url: str, header: str, *, method: str = "GET", client: httpx.AsyncClient | None = None) -> httpx.Response:
    started = time.monotonic()
    if client is not None:
        response = await client.request(method, url, headers={"PAYMENT-SIGNATURE": header})
    else:
        async with httpx.AsyncClient(timeout=60) as own:
            response = await own.request(method, url, headers={"PAYMENT-SIGNATURE": header})
    _trace(header, response, time.monotonic() - started, method, url)
    return response


def assert_rejected(response: httpx.Response, *codes: str) -> None:
    assert response.status_code == 402, (response.status_code, response.text)
    assert response.json()["error"] in codes, response.text
    assert "PAYMENT-REQUIRED" in response.headers


# ================================================================ tier 1: no funds, no node needed
# Each forged transaction below spends a made-up UTXO: the gateway must refuse it before the node is asked.


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


def test_v2_1_minimum_split_of_20m_20m_is_refused_at_the_v2_3_minimum_price(proxy_url, quote, forge) -> None:
    """CONTRACT v2.3: the merchant output must be >= 2*floor. The old 0.4 KAS split no longer pays a 0.6 KAS price."""
    floor = quote.fee_sompi
    forged = forge([(quote.payTo, floor), (quote.platformFee.feeTo, floor)])
    assert_rejected(asyncio.run(send(proxy_url, forged.header())), "underpaid", "invalid_fee_split")


def test_merchant_output_paid_to_a_third_party_is_invalid_fee_split(proxy_url, quote, forge) -> None:
    forged = forge([(ATTACKER_ADDRESS, quote.merchant_sompi), (quote.platformFee.feeTo, quote.fee_sompi)])
    assert_rejected(asyncio.run(send(proxy_url, forged.header())), "invalid_fee_split")


def test_forged_tx_id_is_rejected_with_402(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    response = asyncio.run(send(proxy_url, forged.header(txId="ab" * 32)))
    assert_rejected(response, "invalid_payment_header")


def test_mainnet_prefixed_payer_on_testnet_is_402(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    mainnet_payer = "kaspa:" + forged.payer_address.split(":", 1)[1]
    response = asyncio.run(send(proxy_url, forged.header(payerAddress=mainnet_payer)))
    assert_rejected(response, "wrong_network", "invalid_payment_header")


def test_rejected_signature_does_not_poison_the_replay_lock(proxy_url, quote, forge) -> None:
    """Presenting a tx id with a bad body must not stop the same id from being presented again (lock released)."""
    forged = forge(merchant_fee_outputs(quote))
    bad = forged.header(rawTx="{}")
    first = asyncio.run(send(proxy_url, bad))
    second = asyncio.run(send(proxy_url, bad))
    assert first.status_code == second.status_code == 402, (first.text, second.text)


# ---------------------------------------------------------------- nonce binding (CONTRACT v2)


def test_unknown_nonce_is_payment_expired_with_a_fresh_challenge(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote), nonce="0" * 32)
    response = asyncio.run(send(proxy_url, forged.header()))
    assert_rejected(response, "payment_expired")
    fresh = challenge_of(response)
    assert fresh.nonce not in {"0" * 32, quote.nonce}
    assert fresh.total_sompi == quote.total_sompi and fresh.expiresAt > datetime.now(UTC)


def test_header_nonce_that_differs_from_the_signed_payload_is_rejected(proxy_url, quote, forge) -> None:
    other = fresh_quote(proxy_url.rsplit("/p/", 1)[0], proxy_url.rsplit("/", 1)[1])
    forged = forge(merchant_fee_outputs(quote), nonce=other.nonce)  # payload says OTHER...
    response = asyncio.run(send(proxy_url, forged.header(nonce=quote.nonce)))  # ...header claims the challenge nonce
    assert_rejected(response, "invalid_payment_header")


def test_transaction_without_the_nonce_payload_is_rejected(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote), payload=b"")
    assert_rejected(asyncio.run(send(proxy_url, forged.header(nonce=quote.nonce))), "invalid_payment_header")


@pytest.mark.parametrize("payload", [b"kasgate:v1:" + b"f" * 32, b"x402", b"kasgate:v2:", b"KASGATE:V1:"])
def test_transaction_with_a_foreign_payload_is_rejected(proxy_url, quote, forge, payload: bytes) -> None:
    forged = forge(merchant_fee_outputs(quote), payload=payload)
    assert_rejected(asyncio.run(send(proxy_url, forged.header(nonce=quote.nonce))), "invalid_payment_header")


def test_nonce_issued_for_another_endpoint_cannot_pay_this_one(gateway_url, auth, proxy_url, min_price, forge, quote) -> None:
    other = create_endpoint(gateway_url, auth, min_price)
    other_quote = fresh_quote(gateway_url, other["slug"])
    forged = forge(merchant_fee_outputs(other_quote), nonce=other_quote.nonce)
    response = asyncio.run(send(proxy_url, forged.header()))
    assert_rejected(response, "invalid_payment_header")
    assert "different endpoint" in response.text


def test_a_failed_payment_leaves_the_nonce_usable(proxy_url, quote, forge) -> None:
    """A verification failure releases the locks (and keeps the challenge): the SAME nonce can be presented again."""
    bad = forge([(quote.payTo, quote.merchant_sompi - 1), (quote.platformFee.feeTo, quote.fee_sompi)])
    first = asyncio.run(send(proxy_url, bad.header()))
    again = forge([(quote.payTo, quote.merchant_sompi - 1), (quote.platformFee.feeTo, quote.fee_sompi)])
    second = asyncio.run(send(proxy_url, again.header()))
    assert_rejected(first, "underpaid")
    assert_rejected(second, "underpaid")  # not payment_expired and not 409: the nonce survived the failed attempt


@pytest.mark.skipif(os.environ.get("KASGATE_TEST_SLOW") != "1", reason="set KASGATE_TEST_SLOW=1: waits for a challenge to expire")
def test_expired_challenge_is_payment_expired(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    time.sleep(max((quote.expiresAt - datetime.now(UTC)).total_seconds(), 0) + 3)
    response = asyncio.run(send(proxy_url, forged.header()))
    assert_rejected(response, "payment_expired")
    assert challenge_of(response).nonce != quote.nonce


# ---------------------------------------------------------------- CONTRACT v2.6: payerProof closes front-running (GAP-1)

FRONT_RUNNER_KEY = "e7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"


def _front_runner_header(forged: ForgedPayment, **overrides: str) -> str:
    """The victim's tx copied from the mempool, re-presented with the front-runner's own payerProof."""
    overrides.setdefault("payerProof", make_payer_proof(FRONT_RUNNER_KEY, forged.nonce, overrides.get("txId", forged.tx_id)))
    return forged.header(**overrides)


def _header_without_proof(forged: ForgedPayment) -> str:
    body = json.loads(forged.signature().model_dump_json())
    body.pop("payerProof")
    return base64.b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()


def _reached_the_node_stage(response: httpx.Response) -> bool:
    """True when the gateway got past every pure check (the proof included); synthetic UTXOs then fail at the node."""
    return response.status_code in {402, 409, 503} and "payerProof" not in response.text


def test_front_runner_with_their_own_proof_is_refused_and_the_victim_is_unaffected(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    thief = asyncio.run(send(proxy_url + "/thief/path", _front_runner_header(forged), method="POST"))
    assert_rejected(thief, "invalid_payment_header")
    assert "payerProof" in thief.text, thief.text
    # nothing was locked or consumed by the refused attempt: the victim's identical tx gets past the proof stage
    victim = asyncio.run(send(proxy_url, forged.header()))
    assert _reached_the_node_stage(victim), victim.text


def test_missing_payer_proof_is_402(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    response = asyncio.run(send(proxy_url, _header_without_proof(forged)))
    assert_rejected(response, "invalid_payment_header")


def test_proof_for_the_wrong_nonce_is_402(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    proof = make_payer_proof(forged.payer_key, "f" * 32, forged.tx_id)
    response = asyncio.run(send(proxy_url, forged.header(payerProof=proof)))
    assert_rejected(response, "invalid_payment_header")
    assert "payerProof" in response.text


def test_proof_for_the_wrong_tx_id_is_402(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    proof = make_payer_proof(forged.payer_key, forged.nonce, "ab" * 32)
    response = asyncio.run(send(proxy_url, forged.header(payerProof=proof)))
    assert_rejected(response, "invalid_payment_header")
    assert "payerProof" in response.text


def test_proof_from_an_earlier_payment_cannot_be_replayed_on_a_new_one(proxy_url, quote, forge) -> None:
    first = forge(merchant_fee_outputs(quote))
    other_quote = fresh_quote(proxy_url.rsplit("/p/", 1)[0], proxy_url.rsplit("/", 1)[1])
    second = forge(merchant_fee_outputs(other_quote), nonce=other_quote.nonce)
    response = asyncio.run(send(proxy_url, second.header(payerProof=first.signature().payerProof)))
    assert_rejected(response, "invalid_payment_header")
    assert "payerProof" in response.text


@pytest.mark.parametrize("bad", ["", "ab" * 63, "zz" * 64, "00" * 64])
def test_malformed_or_zero_proofs_are_402(proxy_url, quote, forge, bad: str) -> None:
    forged = forge(merchant_fee_outputs(quote))
    body = json.loads(forged.signature().model_dump_json())
    body["payerProof"] = bad
    header = base64.b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()
    assert_rejected(asyncio.run(send(proxy_url, header)), "invalid_payment_header")


def test_front_runner_who_claims_the_tx_as_their_own_address_is_refused(proxy_url, quote, forge) -> None:
    forged = forge(merchant_fee_outputs(quote))
    attacker = address_for_key(FRONT_RUNNER_KEY)
    proof = make_payer_proof(FRONT_RUNNER_KEY, forged.nonce, forged.tx_id)
    response = asyncio.run(send(proxy_url, forged.header(payerAddress=attacker, payerProof=proof)))
    assert_rejected(response, "invalid_payment_header")


# ---------------------------------------------------------------- CONTRACT v2.3: at most 16 payer addresses


def _multi_address_payment(quote: PaymentRequired, count: int) -> ForgedPayment:
    keys = [f"{i + 1:064x}" for i in range(count)]
    addresses = [address_for_key(k) for k in keys]
    funds = [synthetic_utxo(address, index=i, amount=2 * 100_000_000) for i, address in enumerate(addresses)]
    change = sum(f["amount"] for f in funds) - quote.total_sompi - 1_000_000
    return forge_exact(
        private_keys=keys,
        network=quote.network,
        utxos=funds,
        outputs=[(quote.payTo, quote.merchant_sompi), (quote.platformFee.feeTo, quote.fee_sompi), (addresses[0], change)],
        payer_address=addresses[0],
        nonce=quote.nonce,
    )


def test_payment_drawing_on_seventeen_addresses_is_rejected_before_the_node(proxy_url, quote) -> None:
    forged = _multi_address_payment(quote, MAX_INPUT_ADDRESSES + 1)
    assert len(forged.tx.inputs) == MAX_INPUT_ADDRESSES + 1
    response = asyncio.run(send(proxy_url, forged.header()))
    assert_rejected(response, "invalid_payment_header")
    assert str(MAX_INPUT_ADDRESSES) in response.text, response.text


# ================================================================ tier 2: funded (real settlements)


@pytest.fixture(scope="module")
def settled(gateway_url, endpoint, proxy_url, real_forge_for) -> dict[str, Any]:
    quote = fresh_quote(gateway_url, endpoint["slug"])
    forged = real_forge_for(merchant_fee_outputs(quote), nonce=quote.nonce)
    response = asyncio.run(send(proxy_url, forged.header()))
    if response.status_code == 200:
        real_forge_for.commit(forged)
    return {"forged": forged, "response": response, "header": forged.header(), "quote": quote}


@funded
def test_happy_path_returns_200_with_a_matching_settlement_receipt(settled, network) -> None:
    response: httpx.Response = settled["response"]
    forged: ForgedPayment = settled["forged"]
    assert response.status_code == 200, response.text
    receipt = PaymentResponse.from_header(response.headers["PAYMENT-RESPONSE"])
    assert receipt.settled is True
    assert receipt.txId == forged.tx_id
    assert receipt.network == network
    assert "PAYMENT-SIGNATURE" not in response.headers and "PAYMENT-REQUIRED" not in response.headers


@funded
def test_reusing_a_settled_tx_id_on_another_request_is_409(settled, proxy_url) -> None:
    assert settled["response"].status_code == 200
    for method, suffix in (("GET", "/other/path"), ("POST", "")):
        response = asyncio.run(send(proxy_url + suffix, settled["header"], method=method))
        assert response.status_code == 409, (method, response.status_code, response.text)
        assert response.json()["error"] == "payment_replayed"


@funded
def test_a_settled_nonce_cannot_pay_a_second_transaction(settled, proxy_url, real_forge_for) -> None:
    """The nonce is single-use: a different tx that echoes it is 409, and nothing is broadcast."""
    assert settled["response"].status_code == 200
    quote: PaymentRequired = settled["quote"]
    second = real_forge_for(merchant_fee_outputs(quote), nonce=quote.nonce)
    response = asyncio.run(send(proxy_url, second.header()))
    assert response.status_code == 409 and response.json()["error"] == "payment_replayed", response.text


@funded
def test_settlement_is_visible_to_the_merchant(settled, gateway_url, auth) -> None:
    assert settled["response"].status_code == 200
    rows = httpx.get(f"{gateway_url}/v1/settlements", headers=auth, timeout=15).json()
    match = [row for row in rows if row["tx_id"] == settled["forged"].tx_id]
    assert len(match) == 1 and match[0]["status"] == "settled"
    assert match[0]["amount_sompi"] == match[0]["merchant_amount_sompi"] + match[0]["fee_amount_sompi"]
    assert match[0]["fee_amount_sompi"] >= 20_000_000 and match[0]["merchant_amount_sompi"] >= 40_000_000


@funded
async def test_concurrent_replay_of_one_signature_yields_exactly_one_200(proxy_url, quote, real_forge) -> None:
    forged = real_forge(merchant_fee_outputs(quote))
    header = forged.header()
    limits = httpx.Limits(max_connections=20)
    async with httpx.AsyncClient(timeout=90, limits=limits) as client:
        responses = await asyncio.gather(*(send(proxy_url, header, client=client) for _ in range(10)))
    statuses = Counter(r.status_code for r in responses)
    if statuses[200]:
        real_forge.commit(forged)
    assert statuses[200] == 1, statuses
    assert statuses[409] == 9, statuses
    for response in responses:
        if response.status_code == 409:
            assert response.json()["error"] == "payment_replayed"
        else:
            assert PaymentResponse.from_header(response.headers["PAYMENT-RESPONSE"]).txId == forged.tx_id


@funded
async def test_sdk_client_pays_end_to_end_and_enforces_the_cap(proxy_url, quote, payer_key, network, pool) -> None:
    pool.available(quote.total_sompi + 35_000_000)  # wait until the previous payments' change is spendable
    async with KasGateClient(payer_key, network=network, rpc_url=RPC_URL) as client:
        with pytest.raises(PriceCapExceededError):
            await client.get(proxy_url, max_price_kas="0.5")
        response = await client.get(proxy_url, max_price_kas="50")
    assert response.status_code == 200, response.text
    receipt = receipt_of(response)
    assert receipt is not None and receipt.amount_sompi == quote.total_sompi
    assert PaymentResponse.from_header(response.headers["PAYMENT-RESPONSE"]).txId == receipt.tx_id


# ---------------------------------------------------------------- tampering that only the node can catch


@funded
def test_payer_address_that_owns_no_input_is_rejected(proxy_url, quote, real_forge) -> None:
    forged = real_forge(merchant_fee_outputs(quote))
    response = asyncio.run(send(proxy_url, forged.header(payerAddress=ATTACKER_ADDRESS)))
    assert_rejected(response, "invalid_payment_header")


@funded
def test_tampered_embedded_utxo_amount_never_settles(proxy_url, quote, real_forge) -> None:
    """inputs[].utxo is attacker-supplied; the node must still reject a tx whose sighash covered other values."""
    from txforge import tamper_raw_tx

    forged = real_forge(merchant_fee_outputs(quote))

    def inflate(data: dict) -> None:
        for entry in data["inputs"]:
            entry["utxo"]["amount"] = entry["utxo"]["amount"] * 1000

    raw = tamper_raw_tx(forged, inflate)
    response = asyncio.run(send(proxy_url, forged.header(rawTx=raw)))
    assert response.status_code != 200, response.text
    assert response.status_code in {402, 409}, (response.status_code, response.text)


@funded
def test_unsigned_transaction_never_settles(proxy_url, quote, real_forge) -> None:
    from txforge import tamper_raw_tx

    forged = real_forge(merchant_fee_outputs(quote))

    def strip(data: dict) -> None:
        for entry in data["inputs"]:
            entry["signatureScript"] = ""

    response = asyncio.run(send(proxy_url, forged.header(rawTx=tamper_raw_tx(forged, strip))))
    assert response.status_code == 402, (response.status_code, response.text)
    assert response.json()["error"] in {"tx_rejected", "invalid_payment_header"}


@funded
def test_small_change_at_the_minimum_price_is_a_non_standard_transaction(gateway_url, endpoint, proxy_url, pool, payer_key, network) -> None:
    """A hand-rolled client that leaves < ~0.27 KAS change at the minimum price is refused (tx_rejected), never broadcast."""
    quote = fresh_quote(gateway_url, endpoint["slug"])
    utxos = pool.available(quote.total_sompi + 5_000_000)
    biggest = max(utxos, key=lambda u: int(u.get("utxoEntry", u)["amount"]))
    amount = int(biggest.get("utxoEntry", biggest)["amount"])
    payer = address_for_key(payer_key)
    small_change = 5_000_000
    spend_fee = amount - quote.total_sompi - small_change
    if spend_fee > 50_000_000:
        pytest.skip("largest UTXO is too big to burn into the miner fee for this probe; use a payer with a ~1 KAS UTXO")
    forged = forge_exact(
        private_keys=[payer_key],
        network=network,
        utxos=[biggest],
        outputs=[(quote.payTo, quote.merchant_sompi), (quote.platformFee.feeTo, quote.fee_sompi), (payer, small_change)],
        payer_address=payer,
        nonce=quote.nonce,
    )
    assert_rejected(asyncio.run(send(proxy_url, forged.header())), "tx_rejected")


@pytest.mark.skipif(os.environ.get("KASGATE_TEST_RPC_DOWN") != "1", reason="set KASGATE_TEST_RPC_DOWN=1 only when the gateway runs with an unreachable Kaspa node")
def test_node_outage_is_503_and_the_same_signature_can_be_retried(proxy_url, quote, forge) -> None:
    """Synthetic UTXOs are enough: with the node down the gateway answers 503 before it can find them unknown."""
    forged = forge(merchant_fee_outputs(quote))
    header = forged.header()
    first = asyncio.run(send(proxy_url, header))
    second = asyncio.run(send(proxy_url, header))
    for response in (first, second):
        assert response.status_code == 503, (response.status_code, response.text)
        assert response.json()["error"] == "rpc_unavailable"
    assert second.status_code != 409  # replay lock was released after the first failure


@funded
def test_upstream_failure_after_payment_can_be_redelivered_with_the_same_signature(gateway_url, auth, min_price, real_forge_for) -> None:
    """CONTRACT v2: a paid call whose upstream failed (5xx) may be re-presented with the SAME tx, nonce, payer, method
    and path. It must not be 409 (and must never charge twice)."""
    failing = _create_failing_endpoint(gateway_url, auth, min_price)
    url = f"{gateway_url}/p/{failing['slug']}"
    quote = fresh_quote(gateway_url, failing["slug"])
    forged = real_forge_for(merchant_fee_outputs(quote), nonce=quote.nonce)
    first = asyncio.run(send(url, forged.header()))
    if first.status_code in {200, 502, 503, 504}:
        real_forge_for.commit(forged)
    assert first.status_code != 402, first.text
    second = asyncio.run(send(url, forged.header()))
    assert second.status_code != 409 or second.json().get("error") != "payment_replayed", second.text
    assert second.status_code != 402, second.text


def _create_failing_endpoint(gateway_url: str, auth: dict[str, str], price: int) -> dict[str, Any]:
    import secrets

    response = httpx.post(
        f"{gateway_url}/v1/endpoints",
        headers=auth,
        json={
            "name": f"qa-failing-{secrets.token_hex(3)}",
            "upstream_url": FAILING_UPSTREAM_URL,
            "price_sompi": price,
            "allowed_methods": ["GET"],
            "is_public_listed": False,
        },
        timeout=10,
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------- documented gaps (assert the secure behaviour)


@funded
def test_front_runner_cannot_claim_the_victims_settled_payment(proxy_url, quote, real_forge) -> None:
    """GAP-1 (closed by CONTRACT v2.6) with real funds: a third party presents the victim's signed tx first, with their
    own key's payerProof, from another path and method. They get 402; the victim then still gets the resource."""
    forged = real_forge(merchant_fee_outputs(quote))
    thief = asyncio.run(send(proxy_url + "/thief/path", _front_runner_header(forged), method="POST"))
    assert_rejected(thief, "invalid_payment_header")
    victim = asyncio.run(send(proxy_url, forged.header()))
    if victim.status_code == 200:
        real_forge.commit(forged)
    assert victim.status_code == 200, victim.text
    assert PaymentResponse.from_header(victim.headers["PAYMENT-RESPONSE"]).txId == forged.tx_id
