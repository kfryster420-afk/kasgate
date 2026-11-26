"""KasGateClient: an httpx.AsyncClient wrapper that pays x402 `kaspa-exact-v1` challenges automatically."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import kaspa

from .catalog import CatalogPage, DiscoverResult
from .challenge import decode_challenge, validate_challenge
from .constants import (
    CATALOG_DEFAULT_LIMIT,
    CATALOG_MAX_LIMIT,
    CATALOG_MAX_PAGES,
    CATALOG_PATH,
    CATALOG_TIMEOUT_SECONDS,
    DEFAULT_MAX_NETWORK_FEE_SOMPI,
    DEFAULT_MAX_PRICE_SOMPI,
    DEFAULT_MAX_REDELIVERIES,
    DEFAULT_MAX_SIGNATURE_HEADER_BYTES,
    DEFAULT_MIN_OUTPUT_SOMPI,
    DEFAULT_UTXO_QUARANTINE_SECONDS,
    DIRECTORY_PATH,
    GATEWAY_MAX_HEADER_BYTES,
    GATEWAY_MAX_RAW_TX_CHARS,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    MANIFEST_TIMEOUT_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    NETWORK_ADDRESS_PREFIX,
    NETWORK_TYPE,
    WELL_KNOWN_X402_PATH,
)
from .exceptions import (
    ConfigurationError,
    InsufficientPrepaidBalanceError,
    InvalidChallengeError,
    PaymentExpiredError,
    PaymentReceiptError,
    PaymentRejectedError,
    PaymentReplayedError,
    ProtocolMismatchError,
    TransactionBuildError,
    error_details,
)
from .prepaid import (
    BALANCE_PATH,
    CHALLENGE_PATH,
    DEPOSIT_PATH,
    DEPOSIT_QUOTE_PATH,
    HEADER_PREPAID_DEBIT,
    HEADER_PREPAID_RECEIPT,
    PREPAID_RECEIPT_EXTENSION,
    REFUND_CHALLENGE_PATH,
    REFUND_STATUS_PATH,
    REFUNDS_PATH,
    PrepaidBalance,
    PrepaidChallenge,
    PrepaidDebitHeader,
    PrepaidDepositQuote,
    PrepaidDepositReceipt,
    PrepaidReceipt,
    PrepaidRefund,
    PrepaidRefundChallenge,
    RefundAction,
    prepaid_body_hash,
    slug_of,
    validate_debit_challenge,
    validate_refund_challenge,
)
from .protocol import (
    PaymentRequired,
    PaymentResponse,
    PaymentSignature,
    kas_to_sompi,
    min_price_sompi,
    payer_proof_message,
    payment_payload,
)
from .transaction import BuiltPayment, build_payment_transaction
from .trials import (
    HEADER_TRIAL_CALL,
    HEADER_TRIAL_OFFER,
    HEADER_TRIAL_RECEIPT,
    TRIAL_CHALLENGE_PATH,
    TRIAL_RECEIPT_EXTENSION,
    TRIAL_STATUS_PATH,
    TrialCallHeader,
    TrialChallenge,
    TrialReceipt,
    TrialStatus,
    validate_trial_challenge,
)
from .utxo import RpcUtxoProvider, UtxoLedger, UtxoProvider

RECEIPT_EXTENSION = "kasgate_receipt"
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
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


def prepaid_receipt_of(response: httpx.Response) -> PrepaidReceipt | None:
    """The CONTRACT v2.11 debit receipt attached by `KasGateClient.prepaid_request`, if any."""
    receipt = response.extensions.get(PREPAID_RECEIPT_EXTENSION)
    return receipt if isinstance(receipt, PrepaidReceipt) else None


def trial_receipt_of(response: httpx.Response) -> TrialReceipt | None:
    """The CONTRACT v2.12 free-trial receipt of a call answered from a trial (automatic or `trial_request`)."""
    receipt = response.extensions.get(TRIAL_RECEIPT_EXTENSION)
    return receipt if isinstance(receipt, TrialReceipt) else None


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
      * CONTRACT v2.1 floor: both outputs are at least `min_output_sompi` (taken from the gateway's
        `/v1/directory` manifest unless pinned in the constructor; contract default 20_000_000 as fallback), so no
        price below 3 * floor (0.6 KAS) is ever paid. An expired challenge is refused before anything is signed.
      * CONTRACT v2 nonce binding: the signed transaction carries `kasgate:v1:<nonce>` as its payload and the nonce
        is echoed in PAYMENT-SIGNATURE. A 402 `payment_expired` triggers exactly one re-challenge (the expired
        nonce never reached the node, so nothing was spent).
      * Redelivery: a settled payment whose upstream failed (5xx) is re-presented with the same signature up to
        `max_redeliveries` times, for idempotent methods only.
      * CONTRACT v2.12 free trials: with `use_free_trials` (default on) a 402 that offers a free trial is answered
        with ONE signed trial call before anything is paid. The signed text is rebuilt locally from this request and
        carries no amount, so a trial signature can never move coins. When the trial is used up (or refused) the
        gateway's normal 402 is paid as usual.
    """

    def __init__(
        self,
        private_key_hex: str,
        network: str = "kaspa:testnet-10",
        rpc_url: str | None = None,
        max_price_sompi: int = DEFAULT_MAX_PRICE_SOMPI,
        *,
        max_network_fee_sompi: int = DEFAULT_MAX_NETWORK_FEE_SOMPI,
        min_output_sompi: int | None = None,
        max_redeliveries: int = DEFAULT_MAX_REDELIVERIES,
        pinned_fee_to: str | None = None,
        unavailable_retry_delay: float | None = None,
        max_signature_header_bytes: int = DEFAULT_MAX_SIGNATURE_HEADER_BYTES,
        utxo_provider: UtxoProvider | None = None,
        utxo_quarantine_seconds: float = DEFAULT_UTXO_QUARANTINE_SECONDS,
        http_client: httpx.AsyncClient | None = None,
        use_free_trials: bool = True,
        **httpx_kwargs: Any,
    ) -> None:
        if network not in NETWORK_ADDRESS_PREFIX:
            raise ConfigurationError(f"unsupported network {network!r}")
        if (
            not isinstance(max_price_sompi, int)
            or isinstance(max_price_sompi, bool)
            or max_price_sompi <= 0
        ):
            raise ConfigurationError("max_price_sompi must be a positive integer")
        if max_network_fee_sompi < 0 or (
            min_output_sompi is not None and min_output_sompi < 1
        ):
            raise ConfigurationError(
                "max_network_fee_sompi must be >= 0 and min_output_sompi >= 1"
            )
        if max_redeliveries < 0:
            raise ConfigurationError("max_redeliveries must be >= 0")
        if not 1_000 <= max_signature_header_bytes <= GATEWAY_MAX_HEADER_BYTES:
            raise ConfigurationError(
                f"max_signature_header_bytes must be within 1000..{GATEWAY_MAX_HEADER_BYTES}"
            )
        match = (
            _PRIVATE_KEY_RE.fullmatch(private_key_hex.strip())
            if isinstance(private_key_hex, str)
            else None
        )
        if match is None:
            raise ConfigurationError(
                "private_key_hex must be a 64-character hex string"
            )
        try:
            self._private_key = kaspa.PrivateKey(match.group(1))
        except Exception as exc:
            raise ConfigurationError(
                "private_key_hex is not a valid secp256k1 secret key"
            ) from exc

        self.network = network
        self.max_price_sompi = max_price_sompi
        self.max_network_fee_sompi = max_network_fee_sompi
        self.min_output_sompi = min_output_sompi  # None = read DirectoryManifest.min_output_sompi per gateway
        self.max_redeliveries = max_redeliveries
        self.use_free_trials = bool(use_free_trials)
        self._manifest_floors: dict[str, int] = {}
        self.unavailable_retry_delay = unavailable_retry_delay
        self.max_signature_header_bytes = max_signature_header_bytes
        self.pinned_fee_to = pinned_fee_to.strip().lower() if pinned_fee_to else None
        self.address: str = self._private_key.to_address(
            NETWORK_TYPE[network]
        ).to_string()

        self._rpc_provider: RpcUtxoProvider | None = None
        if utxo_provider is None:
            self._rpc_provider = RpcUtxoProvider(network, rpc_url)
            utxo_provider = self._rpc_provider
        self._utxo_provider: UtxoProvider = utxo_provider
        self._ledger = UtxoLedger(utxo_quarantine_seconds)
        self._pay_lock = asyncio.Lock()
        self._owns_http = http_client is None
        self._http = (
            http_client
            if http_client is not None
            else httpx.AsyncClient(**httpx_kwargs)
        )

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
        max_price_kas: Decimal | str | float | None = None,
        expected_pay_to: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        cap = self._effective_cap(max_price_kas)
        self._require_replayable(kwargs)

        response = await self._http.request(method, url, **kwargs)
        if (
            response.status_code == 402
            and self.use_free_trials
            and HEADER_TRIAL_OFFER in response.headers
        ):
            tried = await self._trial_call(method, url, kwargs, strict=False)
            if tried is not None:
                await response.aclose()
                if trial_receipt_of(tried) is not None or tried.status_code < 400:
                    return tried  # answered from the free trial (a refunded 5xx included); nothing paid
                if tried.status_code == 402:
                    response = tried  # trial used up or refused: the gateway's normal 402, paid below
                else:
                    await tried.aclose()  # e.g. 429 trial rate limit or 409: pay instead
                    response = await self._http.request(method, url, **kwargs)
        for attempt in range(2):
            if response.status_code != 402:
                return response
            challenge = decode_challenge(response)
            if challenge is None:
                return response
            await response.aclose()

            floor = await self._resolve_min_output(url)
            merchant, fee = validate_challenge(
                challenge,
                network=self.network,
                cap_sompi=cap,
                min_output_sompi=floor,
                pinned_fee_to=self.pinned_fee_to,
                expected_pay_to=expected_pay_to,
            )
            try:
                async with self._pay_lock:
                    return await self._pay_and_retry(
                        method, url, kwargs, challenge, merchant, fee, floor
                    )
            except PaymentExpiredError:
                if attempt == 1:
                    raise
                # The gateway forgot the nonce before touching the node: nothing was broadcast. Ask for a fresh one.
                response = await self._http.request(method, url, **kwargs)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _resolve_min_output(self, url: str | httpx.URL) -> int:
        """Return the output floor: the pinned constructor value, else the gateway manifest, else the default."""
        if self.min_output_sompi is not None:
            return self.min_output_sompi
        target = httpx.URL(str(url))
        origin = f"{target.scheme}://{target.netloc.decode('ascii')}"
        cached = self._manifest_floors.get(origin)
        if cached is not None:
            return cached
        try:
            response = await self._http.get(
                origin + DIRECTORY_PATH, timeout=MANIFEST_TIMEOUT_SECONDS
            )
            body: Any = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return DEFAULT_MIN_OUTPUT_SOMPI
        floor = body.get("min_output_sompi") if isinstance(body, Mapping) else None
        network = body.get("network") if isinstance(body, Mapping) else None
        if (
            isinstance(floor, int)
            and not isinstance(floor, bool)
            and 1 <= floor <= self.max_price_sompi
            and network == self.network
        ):
            self._manifest_floors[origin] = floor
            return floor
        return DEFAULT_MIN_OUTPUT_SOMPI

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


    async def discover(
        self,
        gateway_url: str | httpx.URL,
        *,
        query: str | None = None,
        limit: int = CATALOG_DEFAULT_LIMIT,
        cursor: str | None = None,
        follow_pages: bool = True,
        well_known: bool = False,
    ) -> DiscoverResult:
        """Fetch the public catalog of listed endpoints. This never pays and never signs.

        By default every page is followed (up to CATALOG_MAX_PAGES) and `next_cursor` is None.
        Pass `follow_pages=False` to return a single page and keep `next_cursor` for the caller.
        Endpoints priced above `max_price_sompi` are included in `items` and omitted from `payable`.
        """
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= CATALOG_MAX_LIMIT
        ):
            raise ConfigurationError(
                f"limit must be an integer from 1 to {CATALOG_MAX_LIMIT}"
            )
        if query is not None and (not isinstance(query, str) or len(query) > 120):
            raise ConfigurationError("query must be a string of at most 120 characters")
        if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
            raise ConfigurationError("cursor must be a non-empty string")
        origin = str(gateway_url).rstrip("/")
        path = WELL_KNOWN_X402_PATH if well_known else CATALOG_PATH
        collected: list[Any] = []
        next_cursor = cursor
        pages = 0
        last_page: CatalogPage | None = None
        while True:
            pages += 1
            if pages > CATALOG_MAX_PAGES:
                raise ProtocolMismatchError(
                    f"catalog pagination exceeded {CATALOG_MAX_PAGES} pages"
                )
            params: dict[str, Any] = {"limit": limit}
            if query:
                params["q"] = query
            if next_cursor:
                params["cursor"] = next_cursor
            try:
                response = await self._http.get(
                    origin + path, params=params, timeout=CATALOG_TIMEOUT_SECONDS
                )
            except httpx.HTTPError as exc:
                raise ProtocolMismatchError(
                    f"catalog request failed: {type(exc).__name__}"
                ) from exc
            if response.status_code != 200:
                raise ProtocolMismatchError(
                    f"catalog answered {response.status_code}"
                )
            try:
                last_page = CatalogPage.model_validate(response.json())
            except (ValueError, TypeError) as exc:
                raise ProtocolMismatchError("catalog returned an invalid body") from exc
            collected.extend(last_page.items)
            if not follow_pages or last_page.next_cursor is None:
                break
            next_cursor = last_page.next_cursor
        assert last_page is not None
        if follow_pages:
            page = last_page.model_copy(
                update={"items": collected, "next_cursor": None, "limit": limit}
            )
        else:
            page = last_page
        return DiscoverResult(catalog=page, max_price_sompi=self.max_price_sompi)

    # ------------------------------------------------------------------ prepaid credit (CONTRACT v2.11)

    @staticmethod
    def _origin(url: httpx.URL) -> str:
        return f"{url.scheme}://{url.netloc.decode('ascii')}"

    def _absolute(self, url: str | httpx.URL) -> httpx.URL:
        return self._http.build_request("GET", url).url

    async def prepaid_deposit(
        self,
        gateway_url: str | httpx.URL,
        slug: str,
        amount_sompi: int,
        *,
        expected_pay_to: str | None = None,
    ) -> PrepaidDepositReceipt:
        """Deposit `amount_sompi` (the TOTAL, >= 0.6 KAS) to the merchant behind `slug` and return the receipt.

        The deposit is a kaspa-exact-v1 split payment checked exactly like a per-call payment (exact merchant and
        treasury outputs, fee floor, pinned fee_to, expiry). The credit is the merchant share only. `amount_sompi`
        is the explicit ceiling for this one payment; `max_price_sompi` does not apply to deposits.
        """
        if not isinstance(amount_sompi, int) or isinstance(amount_sompi, bool) or amount_sompi <= 0:
            raise ConfigurationError("amount_sompi must be a positive integer")
        origin = self._origin(self._absolute(gateway_url))
        floor = await self._resolve_min_output(origin)
        if amount_sompi < min_price_sompi(floor):
            raise ConfigurationError(f"a deposit must be at least {min_price_sompi(floor)} sompi")
        quoted = await self._http.post(
            origin + DEPOSIT_QUOTE_PATH,
            json={"slug": slug, "payer_address": self.address, "amount_sompi": amount_sompi},
        )
        if quoted.status_code != 201:
            code, message = error_details(quoted)
            raise PaymentRejectedError(quoted.status_code, code, message, quoted)
        try:
            quote = PrepaidDepositQuote.model_validate(quoted.json())
        except ValueError as exc:
            raise InvalidChallengeError(f"deposit quote cannot be decoded: {exc}") from exc
        challenge = quote.required
        merchant, fee = validate_challenge(
            challenge,
            network=self.network,
            cap_sompi=amount_sompi,
            min_output_sompi=floor,
            pinned_fee_to=self.pinned_fee_to,
            expected_pay_to=expected_pay_to,
        )
        if (
            quote.slug != slug
            or quote.payer_address != self.address
            or challenge.total_sompi != amount_sompi
            or int(quote.credit_sompi) != merchant
            or int(quote.fee_sompi) != fee
        ):
            raise InvalidChallengeError("deposit quote does not match the requested deposit")
        async with self._pay_lock:
            confirmed = await self._utxo_provider(self.address)
            entries = self._ledger.available(confirmed)
            built = self._build(challenge, entries, merchant, fee, floor)
            header = self._signature_header(built, challenge.nonce)
            try:
                paid = await self._http.post(origin + DEPOSIT_PATH, headers={HEADER_PAYMENT_SIGNATURE: header})
                if self._is_node_unavailable(paid):
                    await asyncio.sleep(self._retry_delay(paid))
                    paid = await self._http.post(origin + DEPOSIT_PATH, headers={HEADER_PAYMENT_SIGNATURE: header})
            except BaseException:
                self._ledger.quarantine(built)
                raise
            return self._finish_deposit(paid, built, merchant)

    def _finish_deposit(self, response: httpx.Response, built: BuiltPayment, credit: int) -> PrepaidDepositReceipt:
        status = response.status_code
        if status not in (200, 201):
            code, message = error_details(response)
            if status == 409:
                self._ledger.quarantine(built)
                raise PaymentReplayedError(status, code, message, response)
            if status == 402 and code == "payment_expired":
                raise PaymentExpiredError(status, code, message, response)
            if status != 402 or code == "tx_rejected":
                self._ledger.quarantine(built)
            raise PaymentRejectedError(status, code, message, response)
        raw_receipt = response.headers.get(HEADER_PAYMENT_RESPONSE)
        try:
            if raw_receipt is None:
                raise ValueError("no PAYMENT-RESPONSE header")
            parsed = PaymentResponse.from_header(raw_receipt)
            receipt = PrepaidDepositReceipt.model_validate(response.json())
        except ValueError as exc:
            self._ledger.quarantine(built)
            raise PaymentReceiptError(f"deposit receipt cannot be decoded: {exc}", response, built.tx_id) from exc
        if (
            parsed.txId != built.tx_id
            or parsed.network != self.network
            or receipt.tx_id != built.tx_id
            or int(receipt.credit_sompi) != credit
        ):
            self._ledger.quarantine(built)
            raise PaymentReceiptError(
                "deposit receipt does not match the transaction we signed", response, built.tx_id
            )
        self._ledger.mark_spent(built)
        return receipt

    async def prepaid_balance(self, gateway_url: str | httpx.URL, slug: str) -> PrepaidBalance:
        """This key's prepaid balance at the merchant behind `slug` (public read; never signs)."""
        origin = self._origin(self._absolute(gateway_url))
        response = await self._http.get(
            origin + BALANCE_PATH, params={"slug": slug, "payer_address": self.address}
        )
        if response.status_code != 200:
            code, message = error_details(response)
            raise PaymentRejectedError(response.status_code, code, message, response)
        return PrepaidBalance.model_validate(response.json())

    async def prepaid_request(
        self,
        method: str,
        url: str | httpx.URL,
        *,
        max_price_kas: Decimal | str | float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Call a /p/{slug} endpoint paid from the prepaid balance: one signed, single-use debit, no transaction.

        The SDK asks for the debit challenge, rebuilds its text from this very request (domain, payer, method,
        path + query, body hash) and the price cap, and only then signs it. The response carries the
        X-KasGate-Prepaid-Receipt, also available as `prepaid_receipt_of(response)`; a failed upstream (no response
        or 5xx) is refunded by the gateway and the receipt says `refunded`. Nothing is retried automatically.
        """
        cap = self._effective_cap(max_price_kas)
        self._require_replayable(kwargs)
        request = self._http.build_request(method, url, **kwargs)
        body = request.read()
        path = request.url.raw_path.decode("ascii")
        origin = self._origin(request.url)
        method = request.method.upper()
        challenged = await self._http.post(
            origin + CHALLENGE_PATH,
            json={
                "slug": slug_of(path),
                "payer_address": self.address,
                "method": method,
                "path": path,
                "body_hash": prepaid_body_hash(body),
            },
        )
        if challenged.status_code != 201:
            code, message = error_details(challenged)
            if code == "insufficient_balance":
                raise InsufficientPrepaidBalanceError(challenged.status_code, code, message, challenged)
            raise PaymentRejectedError(challenged.status_code, code, message, challenged)
        try:
            challenge = PrepaidChallenge.model_validate(challenged.json())
        except ValueError as exc:
            raise InvalidChallengeError(f"prepaid challenge cannot be decoded: {exc}") from exc
        validate_debit_challenge(
            challenge,
            domain=request.url.netloc.decode("ascii").lower(),
            address=self.address,
            method=method,
            path=path,
            body_hash=prepaid_body_hash(body),
            cap_sompi=cap,
        )
        signature = kaspa.sign_message(challenge.message, self._private_key)
        request.headers[HEADER_PREPAID_DEBIT] = PrepaidDebitHeader(
            nonce=challenge.nonce, signature=signature
        ).to_header()
        response = await self._http.send(request)
        raw_receipt = response.headers.get(HEADER_PREPAID_RECEIPT)
        if raw_receipt is None:
            code, message = error_details(response)
            if response.status_code == 409:
                raise PaymentReplayedError(409, code, message, response)
            if response.status_code == 402 and code == "insufficient_balance":
                raise InsufficientPrepaidBalanceError(402, code, message, response)
            if response.status_code == 402 and code == "payment_expired":
                raise PaymentExpiredError(402, code, message, response)
            if response.status_code in (402, 503):
                raise PaymentRejectedError(response.status_code, code, message, response)
            if 200 <= response.status_code < 300:
                raise PaymentReceiptError("prepaid call succeeded without a debit receipt", response)
            return response
        try:
            receipt = PrepaidReceipt.from_header(raw_receipt)
        except ValueError as exc:
            raise PaymentReceiptError(f"prepaid receipt cannot be decoded: {exc}", response) from exc
        if receipt.nonce != challenge.nonce or receipt.amountSompi != challenge.amount_sompi:
            raise PaymentReceiptError("prepaid receipt does not match the signed debit", response)
        response.extensions[PREPAID_RECEIPT_EXTENSION] = receipt
        return response

    # ------------------------------------------------------------------ prepaid refunds (CONTRACT v2.13)

    async def prepaid_refund_request(
        self, gateway_url: str | httpx.URL, slug: str, amount_sompi: int | None = None
    ) -> PrepaidRefund:
        """Ask the merchant behind `slug` to pay unused credit back to this key's address (all spendable credit
        when `amount_sompi` is None; at least 0.2 KAS). The amount is held at once; the merchant then pays it
        on-chain from their own wallet (no platform fee) and the refund becomes `paid`. Poll `prepaid_refunds`."""
        if amount_sompi is not None and (
            not isinstance(amount_sompi, int) or isinstance(amount_sompi, bool) or amount_sompi <= 0
        ):
            raise ConfigurationError("amount_sompi must be a positive integer or None")
        return await self._signed_refund(gateway_url, slug, "request", amount_sompi, None)

    async def prepaid_refund_cancel(
        self, gateway_url: str | httpx.URL, slug: str, refund_id: uuid.UUID | str
    ) -> PrepaidRefund:
        """Withdraw a refund the merchant has not paid yet; the held credit becomes spendable again."""
        return await self._signed_refund(gateway_url, slug, "cancel", None, uuid.UUID(str(refund_id)))

    async def prepaid_refunds(self, gateway_url: str | httpx.URL, slug: str) -> list[PrepaidRefund]:
        """This key's refunds at the merchant behind `slug`, newest first (public read; never signs)."""
        origin = self._origin(self._absolute(gateway_url))
        response = await self._http.get(
            origin + REFUND_STATUS_PATH, params={"slug": slug, "payer_address": self.address}
        )
        if response.status_code != 200:
            code, message = error_details(response)
            raise PaymentRejectedError(response.status_code, code, message, response)
        try:
            return [PrepaidRefund.model_validate(item) for item in response.json()]
        except (ValueError, TypeError) as exc:
            raise InvalidChallengeError(f"refund list cannot be decoded: {exc}") from exc

    async def _signed_refund(
        self,
        gateway_url: str | httpx.URL,
        slug: str,
        action: RefundAction,
        amount_sompi: int | None,
        refund_id: uuid.UUID | None,
    ) -> PrepaidRefund:
        url = self._absolute(gateway_url)
        origin = self._origin(url)
        body: dict[str, Any] = {"slug": slug, "payer_address": self.address, "action": action}
        if amount_sompi is not None:
            body["amount_sompi"] = amount_sompi
        if refund_id is not None:
            body["refund_id"] = str(refund_id)
        challenged = await self._http.post(origin + REFUND_CHALLENGE_PATH, json=body)
        if challenged.status_code != 201:
            code, message = error_details(challenged)
            if code == "insufficient_balance":
                raise InsufficientPrepaidBalanceError(challenged.status_code, code, message, challenged)
            raise PaymentRejectedError(challenged.status_code, code, message, challenged)
        try:
            challenge = PrepaidRefundChallenge.model_validate(challenged.json())
        except ValueError as exc:
            raise InvalidChallengeError(f"refund challenge cannot be decoded: {exc}") from exc
        validate_refund_challenge(
            challenge,
            domain=url.netloc.decode("ascii").lower(),
            address=self.address,
            slug=slug,
            action=action,
            amount_sompi=amount_sompi,
            refund_id=refund_id,
        )
        signed = {"nonce": challenge.nonce, "signature": kaspa.sign_message(challenge.message, self._private_key)}
        target = REFUNDS_PATH if refund_id is None else f"{REFUNDS_PATH}/{refund_id}/cancel"
        response = await self._http.post(origin + target, json=signed)
        if response.status_code not in (200, 201):
            code, message = error_details(response)
            if response.status_code == 409 and code == "challenge_replayed":
                raise PaymentReplayedError(409, code, message, response)
            raise PaymentRejectedError(response.status_code, code, message, response)
        try:
            refund = PrepaidRefund.model_validate(response.json())
        except ValueError as exc:
            raise PaymentReceiptError(f"refund cannot be decoded: {exc}", response) from exc
        if refund.payer_address != self.address or int(refund.amount_sompi) != int(challenge.amount_sompi):
            raise PaymentReceiptError("refund does not match the signed request", response)
        return refund

    # ------------------------------------------------------------------ free trial calls (CONTRACT v2.12)

    async def trial_status(self, gateway_url: str | httpx.URL, slug: str) -> TrialStatus:
        """How many free trial calls this key has left on `slug` (public read; never signs)."""
        origin = self._origin(self._absolute(gateway_url))
        response = await self._http.get(
            origin + TRIAL_STATUS_PATH, params={"slug": slug, "payer_address": self.address}
        )
        if response.status_code != 200:
            code, message = error_details(response)
            raise PaymentRejectedError(response.status_code, code, message, response)
        return TrialStatus.model_validate(response.json())

    async def trial_request(self, method: str, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        """Make ONE /p/{slug} call from this key's free trial and never pay: raises PaymentRejectedError (code
        `trial_exhausted`) when no trial call is left. The receipt is `trial_receipt_of(response)`; a failed upstream
        (no response or 5xx) gives the trial call back and the receipt says `refunded`."""
        self._require_replayable(kwargs)
        response = await self._trial_call(method, url, kwargs, strict=True)
        assert response is not None  # strict mode raises instead of returning None
        return response

    async def _trial_call(
        self, method: str, url: str | httpx.URL, kwargs: dict[str, Any], *, strict: bool
    ) -> httpx.Response | None:
        """Ask for a trial challenge, verify it against this exact request, sign it, send the call.

        Returns None (not strict) when the gateway refuses the challenge, e.g. the trial is used up: the caller then
        pays. A challenge that does not match the request is never signed (InvalidChallengeError).
        """
        request = self._http.build_request(method, url, **kwargs)
        body = request.read()
        path = request.url.raw_path.decode("ascii")
        try:
            slug = slug_of(path)
        except InvalidChallengeError:
            if strict:
                raise
            return None
        origin = self._origin(request.url)
        method = request.method.upper()
        body_hash = prepaid_body_hash(body)
        challenged = await self._http.post(
            origin + TRIAL_CHALLENGE_PATH,
            json={
                "slug": slug,
                "payer_address": self.address,
                "method": method,
                "path": path,
                "body_hash": body_hash,
            },
        )
        if challenged.status_code != 201:
            if not strict:
                return None
            code, message = error_details(challenged)
            raise PaymentRejectedError(challenged.status_code, code, message, challenged)
        try:
            challenge = TrialChallenge.model_validate(challenged.json())
        except ValueError as exc:
            raise InvalidChallengeError(f"trial challenge cannot be decoded: {exc}") from exc
        validate_trial_challenge(
            challenge,
            domain=request.url.netloc.decode("ascii").lower(),
            address=self.address,
            slug=slug,
            method=method,
            path=path,
            body_hash=body_hash,
        )
        signature = kaspa.sign_message(challenge.message, self._private_key)
        request.headers[HEADER_TRIAL_CALL] = TrialCallHeader(
            nonce=challenge.nonce, signature=signature
        ).to_header()
        response = await self._http.send(request)
        raw_receipt = response.headers.get(HEADER_TRIAL_RECEIPT)
        if raw_receipt is None:
            if strict:
                code, message = error_details(response)
                if response.status_code == 409:
                    raise PaymentReplayedError(409, code, message, response)
                if response.status_code == 402 and code == "payment_expired":
                    raise PaymentExpiredError(402, code, message, response)
                if response.status_code in (402, 429, 503):
                    raise PaymentRejectedError(response.status_code, code, message, response)
                if 200 <= response.status_code < 300:
                    raise PaymentReceiptError("trial call succeeded without a trial receipt", response)
            return response
        try:
            receipt = TrialReceipt.from_header(raw_receipt)
        except ValueError as exc:
            raise PaymentReceiptError(f"trial receipt cannot be decoded: {exc}", response) from exc
        if receipt.nonce != challenge.nonce:
            raise PaymentReceiptError("trial receipt does not match the signed trial call", response)
        response.extensions[TRIAL_RECEIPT_EXTENSION] = receipt
        return response

    # ------------------------------------------------------------------ internals

    def _effective_cap(self, per_call_kas: Decimal | str | float | None) -> int:
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
            raise ConfigurationError(
                "request content must be bytes or str so it can be replayed after payment"
            )
        if kwargs.get("files") is not None:
            raise ConfigurationError(
                "multipart file uploads cannot be replayed after payment"
            )

    def _build(
        self,
        challenge: PaymentRequired,
        entries: list[dict[str, Any]],
        merchant: int,
        fee: int,
        floor: int,
    ) -> BuiltPayment:
        return build_payment_transaction(
            private_key=self._private_key,
            network=self.network,
            utxo_entries=entries,
            pay_to=challenge.payTo,
            fee_to=challenge.platformFee.feeTo,
            merchant_sompi=merchant,
            fee_sompi=fee,
            max_network_fee_sompi=self.max_network_fee_sompi,
            payload=payment_payload(challenge.nonce),
            min_output_sompi=floor,
        )

    def _payer_proof(self, nonce: str, tx_id: str) -> str:
        """Schnorr proof that the payer key (not a mempool observer) presents this tx for this challenge (v2.6)."""
        message = payer_proof_message(nonce, tx_id)
        proof = kaspa.sign_message(message, self._private_key)
        if not kaspa.verify_message(
            message,
            proof,
            kaspa.PublicKey(
                self._private_key.to_public_key().to_x_only_public_key().to_string()
            ),
        ):
            raise TransactionBuildError(
                "the payer proof does not verify against the payer key"
            )
        return proof

    def _signature_header(self, built: BuiltPayment, nonce: str) -> str:
        if len(built.raw_tx) > GATEWAY_MAX_RAW_TX_CHARS:
            raise TransactionBuildError(
                "rawTx exceeds the gateway limit; consolidate the wallet's UTXOs"
            )
        header = PaymentSignature(
            txId=built.tx_id,
            rawTx=built.raw_tx,
            payerAddress=built.payer_address,
            nonce=nonce,
            payerProof=self._payer_proof(nonce, built.tx_id),
        ).to_header()
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
        floor: int,
    ) -> httpx.Response:
        confirmed = await self._utxo_provider(self.address)
        entries = self._ledger.available(confirmed)
        built = self._build(challenge, entries, merchant, fee, floor)
        header = self._signature_header(built, challenge.nonce)

        headers = httpx.Headers(kwargs.get("headers") or {})
        headers[HEADER_PAYMENT_SIGNATURE] = header
        retry_kwargs = {**kwargs, "headers": headers}
        try:
            response = await self._http.request(method, url, **retry_kwargs)
            if self._is_node_unavailable(response):
                # The gateway released its replay lock and did not settle: resending the SAME signature is safe.
                await asyncio.sleep(self._retry_delay(response))
                response = await self._http.request(method, url, **retry_kwargs)
            for _ in range(
                self.max_redeliveries if method.upper() in _IDEMPOTENT_METHODS else 0
            ):
                if not self._is_upstream_failure(response):
                    break
                await asyncio.sleep(self._retry_delay(response))
                again = await self._http.request(method, url, **retry_kwargs)
                if again.status_code in (402, 409):
                    break  # redelivery window or attempts exhausted: keep the original upstream failure
                response = again
        except BaseException:
            self._ledger.quarantine(built)
            raise
        return self._finish(response, built, challenge)

    @staticmethod
    def _is_node_unavailable(response: httpx.Response) -> bool:
        """503 rpc_unavailable. The current gateway emits code `tx_rejected` plus Retry-After for this case."""
        if (
            response.status_code != 503
            or response.headers.get(HEADER_PAYMENT_RESPONSE) is not None
        ):
            return False
        code, _ = error_details(response)
        return code == "rpc_unavailable" or (
            code == "tx_rejected" and "retry-after" in response.headers
        )

    @staticmethod
    def _is_upstream_failure(response: httpx.Response) -> bool:
        """The payment settled but the upstream answered 5xx: the gateway allows re-forwarding the same tx."""
        status = response.status_code
        return status in (500, 502, 504) or (
            status == 503 and response.headers.get(HEADER_PAYMENT_RESPONSE) is not None
        )

    def _retry_delay(self, response: httpx.Response) -> float:
        if self.unavailable_retry_delay is not None:
            return self.unavailable_retry_delay
        try:
            return max(
                0.0,
                min(
                    float(response.headers.get("retry-after", "1")),
                    MAX_RETRY_AFTER_SECONDS,
                ),
            )
        except ValueError:
            return 1.0

    def _finish(
        self, response: httpx.Response, built: BuiltPayment, challenge: PaymentRequired
    ) -> httpx.Response:
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
            if code == "payment_expired":
                # The gateway resolves the nonce before verifying or broadcasting anything: nothing was spent.
                raise PaymentExpiredError(status, code, message, response)
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
            raise PaymentReceiptError(
                f"PAYMENT-RESPONSE cannot be decoded: {exc}", response, built.tx_id
            ) from exc
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
