"""CONTRACT v2.13 prepaid refunds from the buyer side, against a fake gateway built on the gateway's own models."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import kaspa
import pytest
from app import schemas as gw
from conftest import PAYER, PAYER_KEY

import kasgate_client as sdk
from kasgate_client import (
    InsufficientPrepaidBalanceError,
    InvalidBalanceSignatureError,
    InvalidChallengeError,
    KasGateClient,
    PaymentReceiptError,
    PaymentRejectedError,
    PrepaidRefund,
)

MERCHANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
NONCE = "0123456789abcdef" * 2
SPENDABLE = 40_000_000


def xonly(key_hex: str) -> str:
    return kaspa.PrivateKey(key_hex).to_public_key().to_x_only_public_key().to_string()


def signed_read_ok(request: httpx.Request, purpose: str) -> bool:
    """The buyer-signed read scheme (fix/earlier-features a96a16a `buyer_read_signed`): both headers, unix-seconds
    timestamp within ±300 s, a signature over the read text for this domain, payer, slug and `purpose`."""
    stamp = request.headers.get(sdk.HEADER_BALANCE_TIMESTAMP)
    signature = request.headers.get(sdk.HEADER_BALANCE_SIGNATURE)
    if (
        stamp is None
        or signature is None
        or not stamp.isdigit()
        or abs(int(stamp) - int(datetime.now(UTC).timestamp())) > 300
    ):
        return False
    message = sdk.build_prepaid_signed_read_message(
        domain=request.url.netloc.decode("ascii").lower(),
        address=request.url.params["payer_address"],
        slug=request.url.params["slug"],
        timestamp=int(stamp),
        purpose=purpose,
    )
    return bool(
        kaspa.verify_message(message, signature, kaspa.PublicKey(xonly(PAYER_KEY)))
    )


def challenge_read_is_signed(request: httpx.Request, body: dict[str, Any]) -> bool:
    """What the gateway checks on POST /refunds/challenge when no amount is sent: a balance-read signature."""
    stamp = request.headers.get(gw.HEADER_BALANCE_TIMESTAMP)
    signature = request.headers.get(gw.HEADER_BALANCE_SIGNATURE)
    if stamp is None or signature is None:
        return False
    message = gw.build_prepaid_balance_read_message(
        domain=request.url.netloc.decode("ascii").lower(),
        address=body["payer_address"],
        slug=body["slug"],
        timestamp=int(stamp),
    )
    return bool(kaspa.verify_message(message, signature, kaspa.PrivateKey(PAYER_KEY).to_public_key()))


class RefundGateway:
    """Fake /v1/prepaid/refunds*: validates the SDK's bodies with the gateway's strict models and builds the
    challenge text with the gateway's builder, so a drifting SDK fails here."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides: dict[str, Any] = {}
        self.message_override: str | None = None
        self.challenge_error: tuple[int, str] | None = None
        self.amount_override: int | None = None
        self.refund_id = uuid.uuid4()
        self.last: dict[str, Any] | None = None
        self.refunds: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/prepaid/refunds/challenge":
            return self.challenge(json.loads(request.content), request)
        if path == "/v1/prepaid/refunds/status" and request.method == "POST":
            body = gw.PrepaidRefundSigned.model_validate(json.loads(request.content))
            assert self.last is not None and self.last["action"] == "status"
            assert kaspa.verify_message(
                self.last["message"], body.signature, kaspa.PublicKey(xonly(PAYER_KEY))
            )
            return httpx.Response(
                200, json=[{**row, "reason": "promo credit"} for row in self.refunds]
            )
        if path == "/v1/prepaid/refunds/status":
            if not signed_read_ok(
                request, sdk.PREPAID_REFUNDS_READ_PURPOSE
            ):  # refunds purpose; unsigned or cross-purpose is 401
                return httpx.Response(
                    401,
                    json={
                        "error": "invalid_balance_signature",
                        "message": "sign the read",
                    },
                )
            return httpx.Response(200, json=self.refunds)
        if path == "/v1/prepaid/refunds" or path.endswith("/cancel"):
            return self.signed(request)
        return httpx.Response(404, json={"detail": "not found"})

    def challenge(self, body: dict[str, Any], request: httpx.Request) -> httpx.Response:
        parsed = gw.PrepaidRefundChallengeRequest.model_validate(body)
        if parsed.action == "request" and parsed.amount_sompi is None and not challenge_read_is_signed(request, body):
            return httpx.Response(422, json={"error": "refund_amount_required", "message": "sign or send an amount"})
        if self.challenge_error is not None:
            status, code = self.challenge_error
            return httpx.Response(status, json={"error": code, "message": "no"})
        amount = parsed.amount_sompi if parsed.amount_sompi is not None else SPENDABLE
        if parsed.action == "cancel":
            amount = 25_000_000
        if parsed.action == "status":
            amount = 0
        expires = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=60)
        fields: dict[str, Any] = {
            "action": parsed.action,
            "address": parsed.payer_address,
            "merchant_id": MERCHANT_ID,
            "amount_sompi": amount,
            "refund_id": parsed.refund_id,
            "nonce": NONCE,
            "expires_at": expires,
        }
        message = gw.build_prepaid_refund_message(domain="gateway.test", **fields)
        payload = {
            "nonce": NONCE,
            "message": self.message_override or message,
            "action": parsed.action,
            "slug": parsed.slug,
            "merchant_id": str(MERCHANT_ID),
            "payer_address": parsed.payer_address,
            "amount_sompi": str(amount),
            "refund_id": None if parsed.refund_id is None else str(parsed.refund_id),
            "expires_at": expires.isoformat(),
        } | self.overrides
        self.last = {"message": message, "amount": amount, "action": parsed.action}
        return httpx.Response(201, json=payload)

    def signed(self, request: httpx.Request) -> httpx.Response:
        body = gw.PrepaidRefundSigned.model_validate(json.loads(request.content))
        assert self.last is not None
        assert kaspa.verify_message(
            self.last["message"], body.signature, kaspa.PublicKey(xonly(PAYER_KEY))
        )
        now = datetime.now(UTC)
        cancelled = self.last["action"] == "cancel"
        refund = {
            "id": str(self.refund_id),
            "merchant_id": str(MERCHANT_ID),
            "payer_address": PAYER,
            "amount_sompi": str(self.amount_override or self.last["amount"]),
            "status": "cancelled" if cancelled else "requested",
            "tx_id": None,
            "reason": None,
            "last_error": None,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "payout_submitted_at": None,
            "paid_at": None,
            "resolved_at": now.isoformat() if cancelled else None,
            "payout_address": PAYER,
            "payout_payload_hex": gw.refund_payout_payload(self.refund_id).hex(),
            "payout_payload": gw.refund_payout_payload(self.refund_id).decode(),
            "claimed_at": None,
            "late_paid_from": None,
            "shortfall_sompi": "0",
        }
        self.refunds.insert(0, refund)
        return httpx.Response(200 if cancelled else 201, json=refund)


@pytest.fixture
def rgw() -> RefundGateway:
    return RefundGateway()


def paths(gateway: RefundGateway) -> list[str]:
    return [r.url.path for r in gateway.requests]


async def test_request_all_spendable_credit(rgw, make_client) -> None:
    client: KasGateClient = make_client(rgw)
    refund = await client.prepaid_refund_request("http://gateway.test", "weather")
    assert isinstance(refund, PrepaidRefund)
    assert (refund.status, int(refund.amount_sompi), refund.payout_address) == (
        "requested",
        SPENDABLE,
        PAYER,
    )
    assert paths(rgw) == ["/v1/prepaid/refunds/challenge", "/v1/prepaid/refunds"]
    body = json.loads(rgw.requests[0].content)
    assert body == {"slug": "weather", "payer_address": PAYER, "action": "request"}
    # no amount: the challenge request carries a balance-read signature (the gateway derives the default only then)
    assert gw.HEADER_BALANCE_SIGNATURE in rgw.requests[0].headers
    assert challenge_read_is_signed(rgw.requests[0], body)


async def test_an_explicit_amount_sends_no_balance_signature(rgw, make_client) -> None:
    client: KasGateClient = make_client(rgw)
    await client.prepaid_refund_request("http://gateway.test", "weather", 25_000_000)
    assert gw.HEADER_BALANCE_SIGNATURE not in rgw.requests[0].headers


async def test_refunds_reads_never_reuse_a_timestamp(rgw, make_client) -> None:
    """The gateway accepts each refunds-status signature once, so two reads in the same second differ."""
    client: KasGateClient = make_client(rgw)
    await client.prepaid_refunds("http://gateway.test", "weather")
    await client.prepaid_refunds("http://gateway.test", "weather")
    stamps = [r.headers[gw.HEADER_BALANCE_TIMESTAMP] for r in rgw.requests]
    sigs = [r.headers[gw.HEADER_BALANCE_SIGNATURE] for r in rgw.requests]
    assert len(set(stamps)) == 2 and len(set(sigs)) == 2


async def test_request_a_partial_amount_and_list_and_cancel(rgw, make_client) -> None:
    client: KasGateClient = make_client(rgw)
    refund = await client.prepaid_refund_request(
        "http://gateway.test", "weather", 25_000_000
    )
    assert int(refund.amount_sompi) == 25_000_000
    assert json.loads(rgw.requests[0].content)["amount_sompi"] == 25_000_000
    cancelled = await client.prepaid_refund_cancel(
        "http://gateway.test", "weather", refund.id
    )
    assert cancelled.status == "cancelled"
    assert rgw.requests[-1].url.path == f"/v1/prepaid/refunds/{refund.id}/cancel"
    listed = await client.prepaid_refunds("http://gateway.test", "weather")
    assert [r.status for r in listed] == ["cancelled", "requested"]
    assert rgw.requests[-1].url.params["payer_address"] == PAYER


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        (
            {
                "payer_address": "kaspatest:qz0000000000000000000000000000000000000000000000000000000000x"
            },
            "payer",
        ),
        ({"action": "cancel"}, "action"),
        ({"slug": "other"}, "endpoint"),
        ({"amount_sompi": "30000000"}, "30000000"),
        (
            {"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
            "expired",
        ),
    ],
)
async def test_mismatched_challenges_are_never_signed(
    rgw, make_client, override, needle
) -> None:
    client: KasGateClient = make_client(rgw)
    rgw.overrides = override
    with pytest.raises(InvalidChallengeError, match=needle):
        await client.prepaid_refund_request(
            "http://gateway.test", "weather", 25_000_000
        )
    assert paths(rgw) == ["/v1/prepaid/refunds/challenge"]


async def test_tampered_text_or_a_mismatched_amount_is_never_signed(
    rgw, make_client
) -> None:
    client: KasGateClient = make_client(rgw)
    rgw.message_override = "KasGate\nsomething else"
    with pytest.raises(InvalidChallengeError, match="does not match"):
        await client.prepaid_refund_request("http://gateway.test", "weather")
    rgw.message_override = None
    # amount_sompi disagrees with the signed text (the unsigned challenge no longer reports spendable)
    rgw.overrides = {"amount_sompi": str(SPENDABLE + 1)}
    with pytest.raises(InvalidChallengeError, match="does not match"):
        await client.prepaid_refund_request("http://gateway.test", "weather")
    assert "/v1/prepaid/refunds" not in paths(rgw)


async def test_gateway_errors_and_bad_results(rgw, make_client) -> None:
    client: KasGateClient = make_client(rgw)
    rgw.challenge_error = (409, "insufficient_balance")
    with pytest.raises(InsufficientPrepaidBalanceError):
        await client.prepaid_refund_request("http://gateway.test", "weather")
    rgw.challenge_error = (422, "refund_too_small")
    with pytest.raises(PaymentRejectedError) as info:
        await client.prepaid_refund_request("http://gateway.test", "weather")
    assert info.value.code == "refund_too_small"
    rgw.challenge_error = None
    rgw.amount_override = 1
    with pytest.raises(PaymentReceiptError):
        await client.prepaid_refund_request("http://gateway.test", "weather")
    with pytest.raises(sdk.ConfigurationError):
        await client.prepaid_refund_request("http://gateway.test", "weather", 0)


def test_message_builder_matches_the_gateway() -> None:
    expires = datetime(2026, 1, 1, tzinfo=UTC)
    refund_id = uuid.uuid4()
    for action, rid in (("request", None), ("cancel", refund_id)):
        args: dict[str, Any] = {
            "domain": "gateway.test",
            "action": action,
            "address": PAYER,
            "merchant_id": MERCHANT_ID,
            "amount_sompi": 5,
            "refund_id": rid,
            "nonce": NONCE,
            "expires_at": expires,
        }
        assert sdk.build_prepaid_refund_message(
            **args
        ) == gw.build_prepaid_refund_message(**args)


async def test_balance_reads_the_v2_13_fields(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "merchant_id": str(MERCHANT_ID),
                "payer_address": PAYER,
                "balance_sompi": "40000000",
                "deposited_sompi": "50000000",
                "debited_sompi": "5000000",
                "refunded_sompi": "5000000",
                "expired_sompi": "0",
                "refund_hold_sompi": "20000000",
                "spendable_sompi": "20000000",
                "expires_at": "2027-01-01T00:00:00+00:00",
                "updated_at": None,
            },
        )

    client: KasGateClient = make_client(handler)
    balance = await client.prepaid_balance("http://gateway.test", "weather")
    assert (balance.refund_hold_sompi, balance.spendable_sompi) == (
        "20000000",
        "20000000",
    )
    assert balance.expires_at is not None and balance.expires_at.year == 2027


async def test_signed_status_returns_reasons_and_checks_the_challenge(
    rgw, make_client
) -> None:
    client: KasGateClient = make_client(rgw)
    await client.prepaid_refund_request("http://gateway.test", "weather")
    public = await client.prepaid_refunds("http://gateway.test", "weather")
    assert public[0].reason is None
    mine = await client.prepaid_refunds("http://gateway.test", "weather", signed=True)
    assert mine[0].reason == "promo credit" and mine[0].shortfall_sompi == "0"
    assert (
        rgw.last is not None
        and "Action: status" in rgw.last["message"]
        and "Amount: 0" in rgw.last["message"]
    )
    rgw.overrides = {
        "amount_sompi": "1"
    }  # a status challenge that names an amount is never signed
    with pytest.raises(InvalidChallengeError):
        await client.prepaid_refunds("http://gateway.test", "weather", signed=True)


def test_paying_and_late_payout_fields_decode() -> None:
    now = datetime.now(UTC).isoformat()
    refund = PrepaidRefund.model_validate(
        {
            "id": str(uuid.uuid4()),
            "merchant_id": str(MERCHANT_ID),
            "payer_address": PAYER,
            "amount_sompi": "25000000",
            "status": "paying",
            "created_at": now,
            "updated_at": now,
            "claimed_at": now,
            "first_claimed_at": now,
            "claim_expires_at": now,
            "hold_until": None,
            "payout_address": PAYER,
            "payout_payload_hex": "00",
            "late_paid_from": None,
            "shortfall_sompi": "0",
        }
    )
    assert refund.status == "paying" and refund.claimed_at is not None
    assert (
        refund.claim_expires_at is not None
        and refund.first_claimed_at is not None
        and refund.hold_until is None
    )


# ---------------------------------------------------------------- buyer-signed reads (balance, refunds/status)


def _builders_from_a96a16a() -> dict[str, Any] | None:
    """The two read-message builders from fix/earlier-features a96a16a, without merging that branch."""
    import importlib.util
    import subprocess
    import sys
    import tempfile

    root = Path(__file__).resolve().parents[3]
    try:
        text = subprocess.check_output(
            ["git", "show", "a96a16a:apps/gateway/app/schemas.py"],
            cwd=root,
            stderr=subprocess.DEVNULL,
        ).decode()
    except (OSError, subprocess.CalledProcessError):
        return None

    def take(marker: str, stop: str) -> str | None:
        begin = text.find(marker)
        if begin < 0:
            return None
        finish = text.find(stop, begin + len(marker))
        if finish < 0:
            return None
        return text[begin:finish]

    balance_eq = take("PREPAID_BALANCE_READ_PURPOSE =", "\n")
    refunds_eq = take("PREPAID_REFUNDS_READ_PURPOSE =", "\n")
    balance_fn = take(
        "def build_prepaid_balance_read_message",
        "def build_prepaid_refunds_read_message",
    )
    refunds_fn = take(
        "def build_prepaid_refunds_read_message", "def build_prepaid_debit_message"
    )
    if None in (balance_eq, refunds_eq, balance_fn, refunds_fn):
        return None
    # Load a96a16a's builders as a throwaway module (trusted source: that commit's schemas.py).
    path = Path(tempfile.gettempdir()) / "kg_a96a16a_read_builders.py"
    path.write_text("\n".join((balance_eq, refunds_eq, balance_fn, refunds_fn)))  # noqa: FLY002
    spec = importlib.util.spec_from_file_location("kg_a96a16a_read_builders", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return {
        "build_prepaid_balance_read_message": module.build_prepaid_balance_read_message,
        "build_prepaid_refunds_read_message": module.build_prepaid_refunds_read_message,
        "PREPAID_REFUNDS_READ_PURPOSE": module.PREPAID_REFUNDS_READ_PURPOSE,
    }


def test_the_read_texts_are_byte_identical_to_the_a96a16a_scheme() -> None:
    """Both purposes, spelled out, and compared to a96a16a's gateway builders when that commit is reachable."""
    args = {
        "domain": "gw.example",
        "address": PAYER,
        "slug": "weather",
        "timestamp": 1_700_000_000,
    }
    balance = (
        "KasGate\nDomain: gw.example\nPurpose: kasgate:v1:prepaid-balance-read\n"
        f"Address: {PAYER}\nSlug: weather\nTimestamp: 1700000000"
    )
    refunds = (
        "KasGate\nDomain: gw.example\nPurpose: kasgate:v1:refunds-status-read\n"
        f"Address: {PAYER}\nSlug: weather\nTimestamp: 1700000000"
    )
    assert sdk.build_prepaid_balance_read_message(**args) == balance
    assert sdk.build_prepaid_refunds_read_message(**args) == refunds
    assert (
        sdk.build_prepaid_signed_read_message(
            **args, purpose=sdk.PREPAID_REFUNDS_READ_PURPOSE
        )
        == refunds
    )
    assert sdk.PREPAID_REFUNDS_READ_PURPOSE == "kasgate:v1:refunds-status-read"
    assert (sdk.HEADER_BALANCE_TIMESTAMP, sdk.HEADER_BALANCE_SIGNATURE) == (
        "X-KasGate-Balance-Timestamp",
        "X-KasGate-Balance-Signature",
    )
    # The worktree's gateway is #3, which may not have these builders; load them from a96a16a directly.
    remote = _builders_from_a96a16a()
    if remote is not None:
        assert sdk.build_prepaid_balance_read_message(**args) == remote[
            "build_prepaid_balance_read_message"
        ](**args)
        assert sdk.build_prepaid_refunds_read_message(**args) == remote[
            "build_prepaid_refunds_read_message"
        ](**args)
        assert (
            remote["PREPAID_REFUNDS_READ_PURPOSE"] == sdk.PREPAID_REFUNDS_READ_PURPOSE
        )
    elif hasattr(gw, "build_prepaid_refunds_read_message"):
        assert sdk.build_prepaid_balance_read_message(
            **args
        ) == gw.build_prepaid_balance_read_message(**args)
        assert sdk.build_prepaid_refunds_read_message(
            **args
        ) == gw.build_prepaid_refunds_read_message(**args)


async def test_the_refund_list_is_a_signed_read_and_an_unsigned_one_is_refused(
    rgw, make_client
) -> None:
    client: KasGateClient = make_client(rgw)
    await client.prepaid_refund_request("http://gateway.test", "weather")
    listed = await client.prepaid_refunds("http://gateway.test", "weather")
    assert len(listed) == 1
    get = rgw.requests[-1]
    assert get.method == "GET" and signed_read_ok(get, sdk.PREPAID_REFUNDS_READ_PURPOSE)
    assert not signed_read_ok(
        get, sdk.PREPAID_BALANCE_READ_PURPOSE
    )  # a balance-read signature would not verify
    assert PAYER_KEY not in str(get.headers) and PAYER_KEY not in str(get.url)
    with httpx.Client(
        transport=httpx.MockTransport(rgw), base_url="http://gateway.test"
    ) as raw:
        unsigned = raw.get(
            "/v1/prepaid/refunds/status",
            params={"slug": "weather", "payer_address": PAYER},
        )
    assert unsigned.status_code == 401


async def test_a_401_on_the_refund_list_is_a_typed_rejection(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": "invalid_balance_signature", "message": "no"}
        )

    client: KasGateClient = make_client(handler)
    with pytest.raises(InvalidBalanceSignatureError) as caught:
        await client.prepaid_refunds("http://gateway.test", "weather")
    assert isinstance(caught.value, PaymentRejectedError)
    assert (caught.value.status_code, caught.value.code) == (
        401,
        "invalid_balance_signature",
    )


async def test_the_balance_is_a_signed_read_by_default(make_client) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        signed = signed_read_ok(request, sdk.PREPAID_BALANCE_READ_PURPOSE)
        body: dict[str, Any] = {
            "spendable_sompi": "20000000",
            "expires_at": "2027-01-01T00:00:00+00:00",
        }
        if signed:
            body |= {
                "merchant_id": str(MERCHANT_ID),
                "payer_address": PAYER,
                "balance_sompi": "40000000",
                "deposited_sompi": "50000000",
                "debited_sompi": "5000000",
                "refunded_sompi": "5000000",
                "expired_sompi": "0",
                "refund_hold_sompi": "20000000",
                "updated_at": None,
            }
        return httpx.Response(200, json=body)

    client: KasGateClient = make_client(handler)
    mine = await client.prepaid_balance("http://gateway.test", "weather")
    assert (
        signed_read_ok(seen[0], sdk.PREPAID_BALANCE_READ_PURPOSE)
        and mine.balance_sompi == "40000000"
    )
    assert mine.spendable_sompi == "20000000"
    assert not signed_read_ok(seen[0], sdk.PREPAID_REFUNDS_READ_PURPOSE)
    public = await client.prepaid_balance(
        "http://gateway.test", "weather", signed=False
    )
    assert sdk.HEADER_BALANCE_SIGNATURE not in seen[1].headers
    assert (
        public.spendable_sompi,
        public.balance_sompi,
        public.updated_at,
        public.deposited_sompi,
    ) == (
        "20000000",
        None,
        None,
        None,
    )
    assert public.expires_at is not None and public.expires_at.year == 2027


async def test_an_unsigned_balance_with_only_spendable_and_expiry_decodes(
    make_client,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"spendable_sompi": "0", "expires_at": None})

    balance = await make_client(handler).prepaid_balance(
        "http://gateway.test", "weather", signed=False
    )
    assert (
        balance.spendable_sompi == "0"
        and balance.balance_sompi is None
        and balance.merchant_id is None
    )


async def test_an_unsigned_balance_with_all_keys_null_decodes(make_client) -> None:
    """a96a16a's unsigned GET /balance returns every field, with only spendable_sompi and expires_at set."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "merchant_id": None,
                "payer_address": None,
                "balance_sompi": None,
                "deposited_sompi": None,
                "debited_sompi": None,
                "updated_at": None,
                "refunded_sompi": None,
                "expired_sompi": None,
                "refund_hold_sompi": None,
                "spendable_sompi": "20000000",
                "expires_at": "2027-01-01T00:00:00+00:00",
                "credited_sompi": None,
            },
        )

    balance = await make_client(handler).prepaid_balance(
        "http://gateway.test", "weather", signed=False
    )
    assert (
        balance.spendable_sompi,
        balance.expires_at is not None and balance.expires_at.year,
    ) == ("20000000", 2027)
    assert (
        balance.balance_sompi,
        balance.updated_at,
        balance.merchant_id,
        balance.payer_address,
    ) == (None, None, None, None)
    assert (balance.deposited_sompi, balance.debited_sompi, balance.refunded_sompi) == (
        None,
        None,
        None,
    )
    assert (
        balance.expired_sompi,
        balance.refund_hold_sompi,
        balance.credited_sompi,
    ) == (None, None, None)


async def test_a_401_on_the_balance_read_is_a_typed_rejection(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": "invalid_balance_signature", "message": "cross-purpose"}
        )

    with pytest.raises(InvalidBalanceSignatureError) as caught:
        await make_client(handler).prepaid_balance("http://gateway.test", "weather")
    assert (caught.value.status_code, caught.value.code) == (
        401,
        "invalid_balance_signature",
    )


async def test_a_balance_for_another_payer_or_malformed_is_refused(make_client) -> None:
    replies: list[Any] = [
        {"payer_address": "kaspatest:qqother", "spendable_sompi": "1"},
        {"spendable_sompi": 5},
        {"spendable_sompi": "-1"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=replies.pop(0))

    client: KasGateClient = make_client(handler)
    for _ in range(3):
        with pytest.raises(PaymentReceiptError):
            await client.prepaid_balance("http://gateway.test", "weather")
