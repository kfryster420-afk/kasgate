"""Blocking wrapper around KasGateClient.

The Kaspa RPC futures are bound to the event loop that created them, so the wrapper owns one background loop thread
for its whole lifetime instead of calling asyncio.run() per request.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Coroutine
from typing import Any, TypeVar

import httpx
from typing_extensions import Self

from .catalog import DiscoverResult
from .client import KasGateClient
from .credit import CreditObservation, FailedDeliveryCreditHint
from .prepaid import PrepaidBalance, PrepaidDepositReceipt, PrepaidRefund
from .trials import TrialStatus

T = TypeVar("T")


class KasGateSyncClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="kasgate-sync-loop", daemon=True
        )
        self._thread.start()
        self._closed = False
        try:
            self._client = self._call(self._create(*args, **kwargs))
        except BaseException:
            self._stop_loop()
            raise

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    @staticmethod
    async def _create(*args: Any, **kwargs: Any) -> KasGateClient:
        return KasGateClient(*args, **kwargs)

    def _call(self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _stop_loop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._loop.is_running():
            self._loop.close()

    @property
    def address(self) -> str:
        return self._client.address

    def request(
        self, method: str, url: str | httpx.URL, **kwargs: Any
    ) -> httpx.Response:
        return self._call(self._client.request(method, url, **kwargs))

    def get(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)


    def discover(self, gateway_url: str | httpx.URL, **kwargs: Any) -> DiscoverResult:
        return self._call(self._client.discover(gateway_url, **kwargs))

    def prepaid_deposit(
        self, gateway_url: str | httpx.URL, slug: str, amount_sompi: int, **kwargs: Any
    ) -> PrepaidDepositReceipt:
        return self._call(self._client.prepaid_deposit(gateway_url, slug, amount_sompi, **kwargs))

    def prepaid_balance(self, gateway_url: str | httpx.URL, slug: str) -> PrepaidBalance:
        return self._call(self._client.prepaid_balance(gateway_url, slug))

    def confirm_credit(
        self,
        gateway_url: str | httpx.URL,
        slug: str,
        credit: CreditObservation | FailedDeliveryCreditHint | httpx.Response,
        *,
        baseline_credited_sompi: int | None = None,
    ) -> CreditObservation:
        return self._call(
            self._client.confirm_credit(gateway_url, slug, credit, baseline_credited_sompi=baseline_credited_sompi)
        )

    def prepaid_request(self, method: str, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self._call(self._client.prepaid_request(method, url, **kwargs))

    def prepaid_refund_request(
        self, gateway_url: str | httpx.URL, slug: str, amount_sompi: int | None = None
    ) -> PrepaidRefund:
        return self._call(self._client.prepaid_refund_request(gateway_url, slug, amount_sompi))

    def prepaid_refund_cancel(
        self, gateway_url: str | httpx.URL, slug: str, refund_id: uuid.UUID | str
    ) -> PrepaidRefund:
        return self._call(self._client.prepaid_refund_cancel(gateway_url, slug, refund_id))

    def prepaid_refunds(self, gateway_url: str | httpx.URL, slug: str) -> list[PrepaidRefund]:
        return self._call(self._client.prepaid_refunds(gateway_url, slug))

    def trial_status(self, gateway_url: str | httpx.URL, slug: str) -> TrialStatus:
        return self._call(self._client.trial_status(gateway_url, slug))

    def trial_request(self, method: str, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self._call(self._client.trial_request(method, url, **kwargs))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._call(self._client.aclose())
        finally:
            self._stop_loop()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
