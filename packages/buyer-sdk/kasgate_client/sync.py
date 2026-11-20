"""Blocking wrapper around KasGateClient.

The Kaspa RPC futures are bound to the event loop that created them, so the wrapper owns one background loop thread
for its whole lifetime instead of calling asyncio.run() per request.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

import httpx

from .client import KasGateClient

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

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._call(self._client.aclose())
        finally:
            self._stop_loop()

    def __enter__(self) -> KasGateSyncClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
