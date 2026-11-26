"""Kaspa wire primitives shared by the v1 (`kaspa-exact-v1`) and v2 (`exact` / `kaspa-exact-v2`) header models.

Kept free of any header model so `app.schemas` and `app.x402_v2` can both import it without a cycle.
"""

import re
from typing import Annotated

from kaspa import Address, pay_to_address_script
from pydantic import Field

from app.config import NETWORK_ADDRESS_PREFIX, get_settings

KASPA_ADDRESS_RE = re.compile(r"^(kaspa|kaspatest):[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{61,63}$")
TX_ID_RE = re.compile(r"^[0-9a-f]{64}$")
P2PK_SCHNORR_SCRIPT_RE = re.compile(r"20([0-9a-f]{64})ac")
SOMPI_STR_RE = r"^[1-9][0-9]{0,17}$"

MAX_SOMPI = 999_999_999_999_999_999  # 18 digits, same cap as SompiStr
Sompi = Annotated[int, Field(gt=0, le=MAX_SOMPI)]
SompiStr = Annotated[str, Field(pattern=SOMPI_STR_RE)]


def validate_kaspa_address(value: str, network: str | None = None) -> str:
    value = value.strip().lower()
    if not KASPA_ADDRESS_RE.fullmatch(value):
        raise ValueError("invalid Kaspa address")
    if not Address.validate(value):
        raise ValueError("invalid Kaspa address checksum")
    target = network or get_settings().kaspa_network
    prefix = NETWORK_ADDRESS_PREFIX[target]
    if not value.startswith(f"{prefix}:"):
        raise ValueError(f"address must use the '{prefix}:' prefix on {target}")
    return value


def validate_tx_id(value: str) -> str:
    value = value.strip().lower()
    if not TX_ID_RE.fullmatch(value):
        raise ValueError("txId must be 64 lowercase hex characters")
    return value


def serialized_script_public_key(address: str) -> str:
    """Locking script of `address` in the serialized wire form: 2-byte big-endian version + script, lowercase hex.

    This is the `payToScriptPublicKey` encoding of kaspa-exact-v2 and the `scriptPublicKey` encoding of the
    kaspa-sdk-safe-json-v2.0.0 transaction projection.
    """
    script = pay_to_address_script(Address(address))
    return f"{int(script.version):04x}{str(script.script).lower()}"


def payer_public_key(address: str) -> str | None:
    """The x-only Schnorr key of a P2PK address (script 20 <key> ac), or None for any other address kind."""
    try:
        script = pay_to_address_script(Address(address))
    except Exception:
        return None
    if int(script.version) != 0:
        return None
    match = P2PK_SCHNORR_SCRIPT_RE.fullmatch(str(script.script).lower())
    return match.group(1) if match else None
