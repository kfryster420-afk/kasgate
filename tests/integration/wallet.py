"""Wallet helpers for the live auth flows (CONTRACT v2.2): challenge, register, login, all signed with kaspa."""

from __future__ import annotations

import secrets
import time
from typing import Any

import httpx
import kaspa

TESTNET = "testnet"


class Wallet:
    """A throw-away Kaspa testnet key that can sign auth challenges."""

    def __init__(self, key_hex: str | None = None) -> None:
        self.key_hex = key_hex or secrets.token_hex(32)
        self.key = kaspa.PrivateKey(self.key_hex)
        self.address = self.key.to_address(TESTNET).to_string()
        self.public_key = self.key.to_public_key().to_string()
        self.x_only_public_key = self.key.to_public_key().to_x_only_public_key().to_string()

    def sign(self, message: str) -> str:
        return kaspa.sign_message(message, self.key)


def post(url: str, **kwargs: Any) -> httpx.Response:
    """POST that sleeps through one 429 (the gateway's per-IP auth limits are per minute)."""
    response = httpx.post(url, timeout=15, **kwargs)
    if response.status_code == 429:
        time.sleep(min(int(response.headers.get("retry-after", "30")) + 1, 65))
        response = httpx.post(url, timeout=15, **kwargs)
    return response


def get_challenge(gateway_url: str, address: str, purpose: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"kaspa_address": address}
    if purpose is not None:
        body["purpose"] = purpose
    response = post(f"{gateway_url}/v1/auth/challenge", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def proof(wallet: Wallet, challenge: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "kaspa_address": wallet.address,
        "challenge": challenge,
        "public_key": wallet.public_key,
        "signature": wallet.sign(challenge),
    }
    body.update(overrides)
    return body


def verify(gateway_url: str, wallet: Wallet, challenge: str, **overrides: Any) -> httpx.Response:
    return post(f"{gateway_url}/v1/auth/verify", json=proof(wallet, challenge, **overrides))


def register(
    gateway_url: str,
    wallet: Wallet,
    *,
    display_name: str | None = None,
    email: str | None = None,
    challenge: str | None = None,
    **overrides: Any,
) -> httpx.Response:
    """POST /v1/merchants with a wallet proof over a fresh `register` challenge (or the supplied one)."""
    text = challenge if challenge is not None else get_challenge(gateway_url, wallet.address, "register")["challenge"]
    body = proof(wallet, text, display_name=display_name or f"qa-{secrets.token_hex(3)}")
    if email is not None:
        body["email"] = email
    body.update(overrides)
    return post(f"{gateway_url}/v1/merchants", json=body)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def me(gateway_url: str, token: str) -> httpx.Response:
    return httpx.get(f"{gateway_url}/v1/merchants/me", headers=bearer(token), timeout=10)
