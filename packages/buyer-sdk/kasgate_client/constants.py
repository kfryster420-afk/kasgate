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

# CONTRACT v2: the gateway rejects Base64 headers larger than schemas.MAX_HEADER_BYTES and rawTx longer than
# schemas.MAX_RAW_TX_CHARS.
GATEWAY_MAX_HEADER_BYTES = 40_960
GATEWAY_MAX_RAW_TX_CHARS = 24_000
PAYMENT_PAYLOAD_PREFIX = "kasgate:v1:"
DIRECTORY_PATH = "/v1/directory"
MANIFEST_TIMEOUT_SECONDS = 5.0
# A challenge that expires sooner than this cannot be paid, mined and presented in time.
MIN_CHALLENGE_TTL_SECONDS = 3.0
# Real-world servers (uvicorn/h11 defaults to 16 KiB per header block, nginx to 8 KiB per line) are stricter than
# the gateway's own limit, so the SDK refuses to emit a PAYMENT-SIGNATURE larger than this by default.
DEFAULT_MAX_SIGNATURE_HEADER_BYTES = 12_000

DEFAULT_MAX_PRICE_SOMPI = 2_000_000_000  # 20 KAS: far above the 0.6 KAS minimum endpoint price, far below a drain.
# KIP-9 storage-mass floor per output (gateway DEFAULT_MIN_OUTPUT_SOMPI). CONTRACT v2.1: the treasury fee is
# max(ceil(1.5%), floor) and the merchant output is total - fee, so the minimum price is 3 * floor = 0.6 KAS.
DEFAULT_MIN_OUTPUT_SOMPI = 20_000_000
DEFAULT_MAX_REDELIVERIES = 1
# CONTRACT v2.3: change must be 0 or >= this, else the tx can exceed the KIP-9 storage-mass limit.
DEFAULT_MIN_CHANGE_SOMPI = 30_000_000
# CONTRACT v2.3: the gateway rejects payments drawing inputs from more than this many distinct addresses.
MAX_INPUT_ADDRESSES = 16
MAX_RETRY_AFTER_SECONDS = 10.0
DEFAULT_MAX_NETWORK_FEE_SOMPI = (
    5_000_000  # 0.05 KAS ceiling on the miner fee the payer funds on top of the price.
)
DEFAULT_UTXO_QUARANTINE_SECONDS = 180.0
