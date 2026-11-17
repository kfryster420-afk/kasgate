"""KasGateClient: an httpx.AsyncClient wrapper that pays x402 `kaspa-exact-v1` challenges automatically."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import kaspa

from .challenge import decode_challenge, validate_challenge
from .constants import (
    DEFAULT_MAX_NETWORK_FEE_SOMPI,
    DEFAULT_MAX_PRICE_SOMPI,
    DEFAULT_MIN_OUTPUT_SOMPI,
    MAX_RETRY_AFTER_SECONDS,
    DEFAULT_MAX_SIGNATURE_HEADER_BYTES,
    DEFAULT_UTXO_QUARANTINE_SECONDS,
    GATEWAY_MAX_HEADER_BYTES,
    GATEWAY_MAX_RAW_TX_CHARS,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    NETWORK_ADDRESS_PREFIX,
    NETWORK_TYPE,
)
from .exceptions import (
    ConfigurationError,
    PaymentReceiptError,
    PaymentRejectedError,
    PaymentReplayedError,
    TransactionBuildError,
    error_details,
)
from .protocol import PaymentRequired, PaymentResponse, PaymentSignature, kas_to_sompi
from .transaction import BuiltPayment, build_payment_transaction
from .utxo import RpcUtxoProvider, UtxoLedger, UtxoProvider

RECEIPT_EXTENSION = "kasgate_receipt"
_PRIVATE_KEY_RE = re.compile(r"^(?:0x)?([0-9a-fA-F]{64})$")


@dataclass(frozen=True)
class PaymentReceipt:
    """What was paid for a successful call. Attached to `response.extensions['kasgate_receipt']`."""

    tx_id: str
    network: str
    payer_address: str
    pay_to: str
    fee_to: str
    amount_sompi: int
    merchant_sompi: int
    fee_sompi: int
    network_fee_sompi: int
    usd_value_at_settlement: Decimal


def receipt_of(response: httpx.Response) -> PaymentReceipt | None:
    return response.extensions.get(RECEIPT_EXTENSION)


class KasGateClient:
    """Drop-in async HTTP client that transparently pays HTTP 402 challenges.

    Safety properties:
      * `max_price_sompi` is a hard circuit breaker checked against the recomputed total before any UTXO is fetched
        or anything is signed. A per-call `max_price_kas` can only tighten it, never raise it.
      * At most one payment is made per call; a 402 after paying raises instead of paying again.
      * Payments are serialised per client so concurrent calls never select the same UTXOs.
      * `max_network_fee_sompi` bounds the miner fee on top of the quoted price.
    """

    def __init__(
        self,
        private_key_hex: str,
        network: str = "kaspa:testnet-10",
        rpc_url: str | None = None,
        max_price_sompi: int = DEFAULT_MAX_PRICE_SOMPI,
        *,
        max_network_fee_sompi: int = DEFAULT_MAX_NETWORK_FEE_SOMPI,
        min_output_sompi: int = DEFAULT_MIN_OUTPUT_SOMPI,
        pinned_fee_to: str | None = None,
        unavailable_retry_delay: float | None = None,
        max_signature_header_bytes: int = DEFAULT_MAX_SIGNATURE_HEADER_BYTES,
        utxo_provider: UtxoProvider | None = None,
        utxo_quarantine_seconds: float = DEFAULT_UTXO_QUARANTINE_SECONDS,
        http_client: httpx.AsyncClient | None = None,
        **httpx_kwargs: Any,
    ) -> None:
        if network not in NETWORK_ADDRESS_PREFIX:
            raise ConfigurationError(f"unsupported network {network!r}")
        if not isinstance(max_price_sompi, int) or isinstance(max_price_sompi, bool) or max_price_sompi <= 0:
            raise ConfigurationError("max_price_sompi must be a positive integer")
        if max_network_fee_sompi < 0 or min_output_sompi < 1:
            raise ConfigurationError("max_network_fee_sompi must be >= 0 and min_output_sompi >= 1")
        if not 1_000 <= max_signature_header_bytes <= GATEWAY_MAX_HEADER_BYTES:
            raise ConfigurationError(f"max_signature_header_bytes must be within 1000..{GATEWAY_MAX_HEADER_BYTES}")
        match = _PRIVATE_KEY_RE.fullmatch(private_key_hex.strip()) if isinstance(private_key_hex, str) else None
        if match is None:
            raise ConfigurationError("private_key_hex must be a 64-character hex string")
        try:
            self._private_key = kaspa.PrivateKey(match.group(1))
        except Exception as exc:
            raise ConfigurationError("private_key_hex is not a valid secp256k1 secret key") from exc

        self.network = network
        self.max_price_sompi = max_price_sompi
        self.max_network_fee_sompi = max_network_fee_sompi
        self.min_output_sompi = min_output_sompi
        self.unavailable_retry_delay = unavailable_retry_delay
        self.max_signature_header_bytes = max_signature_header_bytes
        self.pinned_fee_to = pinned_fee_to.strip().lower() if pinned_fee_to else None
        self.address: str = self._private_key.to_address(NETWORK_TYPE[network]).to_string()

        self._rpc_provider: RpcUtxoProvider | None = None
        if utxo_provider is None:
            self._rpc_provider = RpcUtxoProvider(network, rpc_url)
            utxo_provider = self._rpc_provider
        self._utxo_provider: UtxoProvider = utxo_provider
        self._ledger = UtxoLedger(utxo_quarantine_seconds)
        self._pay_lock = asyncio.Lock()
        self._owns_http = http_client is None
        self._http = http_client if http_client is not None else httpx.AsyncClient(**httpx_kwargs)

    def __repr__(self) -> str:
        return f"KasGateClient(address={self.address!r}, network={self.network!r}, max_price_sompi={self.max_price_sompi})"

    async def __aenter__(self) -> KasGateClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
        if self._rpc_provider is not None:
            await self._rpc_provider.aclose()

    # ------------------------------------------------------------------ public HTTP verbs

    async def request(
        self,
        method: str,
        url: str | httpx.URL,
        *,
        max_price_kas: Decimal | str | int | float | None = None,
        expected_pay_to: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        cap = self._effective_cap(max_price_kas)
        self._require_replayable(kwargs)

        response = await self._http.request(method, url, **kwargs)
        if response.status_code != 402:
            return response
        challenge = decode_challenge(response)
        if challenge is None:
            return response
        await response.aclose()

        merchant, fee = validate_challenge(
            challenge,
            network=self.network,
            cap_sompi=cap,
            min_output_sompi=self.min_output_sompi,
            pinned_fee_to=self.pinned_fee_to,
            expected_pay_to=expected_pay_to,
        )
        async with self._pay_lock:
            return await self._pay_and_retry(method, url, kwargs, challenge, merchant, fee)

    async def get(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    # ------------------------------------------------------------------ internals

    def _effective_cap(self, per_call_kas: Decimal | str | int | float | None) -> int:
        if per_call_kas is None:
            return self.max_price_sompi
        try:
            per_call = kas_to_sompi(per_call_kas)
        except ValueError as exc:
            raise ConfigurationError(f"invalid max_price_kas: {exc}") from exc
        return min(per_call, self.max_price_sompi)

    @staticmethod
    def _require_replayable(kwargs: dict[str, Any]) -> None:
        content = kwargs.get("content")
        if content is not None and not isinstance(content, (bytes, str)):
            raise ConfigurationError("request content must be bytes or str so it can be replayed after payment")
        if kwargs.get("files") is not None:
            raise ConfigurationError("multipart file uploads cannot be replayed after payment")

    def _build(self, challenge: PaymentRequired, entries: list[dict[str, Any]], merchant: int, fee: int) -> BuiltPayment:
        return build_payment_transaction(
            private_key=self._private_key,
            network=self.network,
            utxo_entries=entries,
            pay_to=challenge.payTo,
            fee_to=challenge.platformFee.feeTo,
            merchant_sompi=merchant,
            fee_sompi=fee,
            max_network_fee_sompi=self.max_network_fee_sompi,
        )

    def _signature_header(self, built: BuiltPayment) -> str:
        if len(built.raw_tx) > GATEWAY_MAX_RAW_TX_CHARS:
            raise TransactionBuildError("rawTx exceeds the gateway limit; consolidate the wallet's UTXOs")
        header = PaymentSignature(txId=built.tx_id, rawTx=built.raw_tx, payerAddress=built.payer_address).to_header()
        if len(header) > self.max_signature_header_bytes:
            raise TransactionBuildError(
                f"PAYMENT-SIGNATURE is {len(header)} bytes (limit {self.max_signature_header_bytes}); "
                "consolidate the wallet's UTXOs"
            )
        return header

    async def _pay_and_retry(
        self,
        method: str,
        url: str | httpx.URL,
        kwargs: dict[str, Any],
        challenge: PaymentRequired,
        merchant: int,
        fee: int,
    ) -> httpx.Response:
        confirmed = await self._utxo_provider(self.address)
        entries = self._ledger.available(confirmed)
        built = self._build(challenge, entries, merchant, fee)
        header = self._signature_header(built)

        headers = httpx.Headers(kwargs.get("headers") or {})
        headers[HEADER_PAYMENT_SIGNATURE] = header
        retry_kwargs = {**kwargs, "headers": headers}
        try:
            response = await self._http.request(method, url, **retry_kwargs)
            if self._is_node_unavailable(response):
                # The gateway released its replay lock and did not settle: resending the SAME signature is safe.
                await asyncio.sleep(self._retry_delay(response))
                response = await self._http.request(method, url, **retry_kwargs)
        except BaseException:
            self._ledger.quarantine(built)
            raise
        return self._finish(response, built, challenge)

    @staticmethod
    def _is_node_unavailable(response: httpx.Response) -> bool:
        """503 rpc_unavailable. The current gateway emits code `tx_rejected` plus Retry-After for this case."""
        if response.status_code != 503 or response.headers.get(HEADER_PAYMENT_RESPONSE) is not None:
            return False
        code, _ = error_details(response)
        return code == "rpc_unavailable" or (code == "tx_rejected" and "retry-after" in response.headers)

    def _retry_delay(self, response: httpx.Response) -> float:
        if self.unavailable_retry_delay is not None:
            return self.unavailable_retry_delay
        try:
            return max(0.0, min(float(response.headers.get("retry-after", "1")), MAX_RETRY_AFTER_SECONDS))
        except ValueError:
            return 1.0

    def _finish(self, response: httpx.Response, built: BuiltPayment, challenge: PaymentRequired) -> httpx.Response:
        raw_receipt = response.headers.get(HEADER_PAYMENT_RESPONSE)
        status = response.status_code

        if status == 409:
            # A fresh txId can only be "replayed" if someone else presented our tx first, so assume it was submitted.
            self._ledger.quarantine(built)
            code, message = error_details(response)
            raise PaymentReplayedError(status, code, message, response)
        if status == 503 and raw_receipt is None:
            self._ledger.quarantine(built)
            code, message = error_details(response)
            raise PaymentRejectedError(status, code, message, response)
        if status == 402:
            code, message = error_details(response)
            if code == "tx_rejected":
                self._ledger.quarantine(built)
            raise PaymentRejectedError(status, code, message, response)

        if raw_receipt is None:
            self._ledger.quarantine(built)
            if 200 <= status < 300:
                raise PaymentReceiptError(
                    "paid request succeeded but the response carries no PAYMENT-RESPONSE header",
                    response,
                    built.tx_id,
                )
            return response

        try:
            parsed = PaymentResponse.from_header(raw_receipt)
        except ValueError as exc:
            self._ledger.quarantine(built)
            raise PaymentReceiptError(f"PAYMENT-RESPONSE cannot be decoded: {exc}", response, built.tx_id) from exc
        if parsed.txId != built.tx_id or parsed.network != self.network:
            self._ledger.quarantine(built)
            raise PaymentReceiptError(
                f"PAYMENT-RESPONSE settles txId {parsed.txId} on {parsed.network}; expected {built.tx_id} "
                f"on {self.network}",
                response,
                built.tx_id,
            )

        self._ledger.mark_spent(built)
        response.extensions[RECEIPT_EXTENSION] = PaymentReceipt(
            tx_id=built.tx_id,
            network=parsed.network,
            payer_address=built.payer_address,
            pay_to=challenge.payTo,
            fee_to=challenge.platformFee.feeTo,
            amount_sompi=challenge.total_sompi,
            merchant_sompi=built.merchant_sompi,
            fee_sompi=built.fee_sompi,
            network_fee_sompi=built.network_fee_sompi,
            usd_value_at_settlement=parsed.usdValueAtSettlement,
        )
        return response
