"""Protocol constants. Mirrors apps/gateway/app/config.py and docs/CONTRACTS.md."""

from __future__ import annotations

SOMPI_PER_KAS = 100_000_000
PLATFORM_FEE_BPS = 150
BPS_DENOMINATOR = 10_000

X402_VERSION = 2
X402_SCHEME = "kaspa-exact-v1"
X402_ASSET = "KAS"

HEADER_PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
HEADER_PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"
HEADER_PAYMENT_RESPONSE = "PAYMENT-RESPONSE"

NETWORK_TESTNET = "kaspa:testnet-10"
NETWORK_MAINNET = "kaspa:mainnet"

NETWORK_ADDRESS_PREFIX: dict[str, str] = {
    NETWORK_TESTNET: "kaspatest",
    NETWORK_MAINNET: "kaspa",
}
NETWORK_ID: dict[str, str] = {
    NETWORK_TESTNET: "testnet-10",
    NETWORK_MAINNET: "mainnet",
}
NETWORK_TYPE: dict[str, str] = {
    NETWORK_TESTNET: "testnet",
    NETWORK_MAINNET: "mainnet",
}

# The gateway rejects Base64 headers larger than schemas.MAX_HEADER_BYTES and rawTx longer than 100_000 chars.
GATEWAY_MAX_HEADER_BYTES = 131_072
GATEWAY_MAX_RAW_TX_CHARS = 100_000
# Real-world servers (uvicorn/h11 defaults to 16 KiB per header block, nginx to 8 KiB per line) are stricter than
# the gateway's own limit, so the SDK refuses to emit a PAYMENT-SIGNATURE larger than this by default.
DEFAULT_MAX_SIGNATURE_HEADER_BYTES = 12_000

DEFAULT_MAX_PRICE_SOMPI = 2_000_000_000  # 20 KAS: above the ~13.33 KAS minimum endpoint price, far below a drain.
DEFAULT_MIN_OUTPUT_SOMPI = 20_000_000  # KIP-9 storage-mass floor per output (gateway DEFAULT_MIN_OUTPUT_SOMPI).
MAX_RETRY_AFTER_SECONDS = 10.0
DEFAULT_MAX_NETWORK_FEE_SOMPI = 5_000_000  # 0.05 KAS ceiling on the miner fee the payer funds on top of the price.
DEFAULT_UTXO_QUARANTINE_SECONDS = 180.0
