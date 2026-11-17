"""Rate-limit behaviour (HTTP 429 + Retry-After). Marker: `ratelimit`; opt-in; deliberately sorted last.

The normal suite runs against a gateway started with RATE_LIMIT_ENABLED=false (all host traffic reaches the gateway
from one Docker bridge IP, so default limits such as 5 merchant sign-ups per hour would starve every other test).
To run THESE tests, start the gateway separately with limiting on and low limits, e.g. in infra/.env:

    RATE_LIMIT_ENABLED=true
    RL_MERCHANT_CREATE_PER_HOUR=3
    RL_AUTH_CHALLENGE_PER_MINUTE=4
    RL_AUTH_VERIFY_PER_MINUTE=4

then export the same values for the tests and select the marker:

    KASGATE_RATE_LIMIT_ENABLED=true RL_MERCHANT_CREATE_PER_HOUR=3 RL_AUTH_CHALLENGE_PER_MINUTE=4 \\
    RL_AUTH_VERIFY_PER_MINUTE=4 pytest -m ratelimit tests/integration

The expected limits are read from the RL_* variables (via the gateway's own Settings, defaults 5/10/10), so the
test environment must mirror the gateway's. Each run burns the caller IP's budget for the window; restart Redis or
delete the kasgate:rl:* keys between runs.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any, NamedTuple

import httpx
import pytest
from app.config import get_settings

from conftest import address_for_key

ENABLED = os.environ.get("KASGATE_RATE_LIMIT_ENABLED", "").strip().lower() in {"1", "true", "yes"}

pytestmark = [
    pytest.mark.live,
    pytest.mark.ratelimit,
    pytest.mark.skipif(
        not ENABLED,
        reason=(
            "rate limiting is disabled for the normal suite (RATE_LIMIT_ENABLED=false in infra/.env); set "
            "KASGATE_RATE_LIMIT_ENABLED=true plus matching low RL_* values and run `pytest -m ratelimit`"
        ),
    ),
]


class RateLimit(NamedTuple):
    route: str
    limit: int
    window_seconds: int


_settings = get_settings()
RATE_LIMIT_CREATE_MERCHANT = RateLimit("merchants", _settings.rl_merchant_create_per_hour, 3600)
RATE_LIMIT_AUTH_CHALLENGE = RateLimit("auth_challenge", _settings.rl_auth_challenge_per_minute, 60)
RATE_LIMIT_AUTH_VERIFY = RateLimit("auth_verify", _settings.rl_auth_verify_per_minute, 60)


def fresh_address() -> str:
    return address_for_key(secrets.token_hex(32))


def hammer(url: str, make_body, attempts: int) -> list[httpx.Response]:
    responses: list[httpx.Response] = []
    with httpx.Client(timeout=15) as client:
        for _ in range(attempts):
            responses.append(client.post(url, json=make_body()))
            if responses[-1].status_code == 429:
                break
    return responses


def assert_limited(responses: list[httpx.Response], limit: int) -> None:
    last = responses[-1]
    assert last.status_code == 429, [r.status_code for r in responses]
    assert len(responses) <= limit + 1, f"limit of {limit} not enforced: {[r.status_code for r in responses]}"
    assert int(last.headers["retry-after"]) > 0
    assert "rate limit" in last.json()["detail"]
    assert all(r.status_code != 429 for r in responses[:-1])


def wait_for_window(response: httpx.Response) -> None:
    time.sleep(min(int(response.headers.get("retry-after", "60")) + 1, 65))


def test_auth_challenge_is_rate_limited(gateway_url: str) -> None:
    rule = RATE_LIMIT_AUTH_CHALLENGE
    responses = hammer(
        f"{gateway_url}/v1/auth/challenge", lambda: {"kaspa_address": fresh_address()}, rule.limit + 1
    )
    assert_limited(responses, rule.limit)
    wait_for_window(responses[-1])


def test_auth_challenge_is_also_limited_per_address(gateway_url: str) -> None:
    address = fresh_address()
    responses = hammer(f"{gateway_url}/v1/auth/challenge", lambda: {"kaspa_address": address}, RATE_LIMIT_AUTH_CHALLENGE.limit + 1)
    assert_limited(responses, RATE_LIMIT_AUTH_CHALLENGE.limit)
    wait_for_window(responses[-1])


def test_auth_verify_is_rate_limited(gateway_url: str) -> None:
    rule = RATE_LIMIT_AUTH_VERIFY

    def body() -> dict[str, Any]:
        return {
            "kaspa_address": fresh_address(),
            "challenge": "x" * 40,
            "public_key": "02" + "ab" * 32,
            "signature": "cd" * 64,
        }

    responses = hammer(f"{gateway_url}/v1/auth/verify", body, rule.limit + 1)
    assert_limited(responses, rule.limit)
    assert all(r.status_code == 401 for r in responses[:-1])
    wait_for_window(responses[-1])


def test_merchant_registration_is_rate_limited(gateway_url: str) -> None:
    rule = RATE_LIMIT_CREATE_MERCHANT
    responses = hammer(
        f"{gateway_url}/v1/merchants",
        lambda: {"kaspa_address": fresh_address(), "display_name": f"qa-rl-{secrets.token_hex(3)}"},
        rule.limit + 1,
    )
    assert_limited(responses, rule.limit)
    assert all(r.status_code == 201 for r in responses[:-1])
