import ipaddress
import math
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

KaspaNetwork = Literal["kaspa:testnet-10", "kaspa:mainnet"]

SOMPI_PER_KAS: int = 100_000_000
PLATFORM_FEE_BPS: int = 150
BPS_DENOMINATOR: int = 10_000
X402_VERSION: int = 2
X402_SCHEME: str = "kaspa-exact-v1"
X402_ASSET: str = "KAS"
DEFAULT_MIN_OUTPUT_SOMPI: int = 20_000_000

NETWORK_ADDRESS_PREFIX: dict[str, str] = {
    "kaspa:testnet-10": "kaspatest",
    "kaspa:mainnet": "kaspa",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    kaspa_network: KaspaNetwork = "kaspa:testnet-10"
    kaspa_rpc_url: str | None = None
    kaspa_connect_timeout_seconds: float = Field(default=6.0, gt=0, le=120)
    kaspa_rpc_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    kasgate_treasury_address: str
    min_output_sompi: int = Field(default=DEFAULT_MIN_OUTPUT_SOMPI, ge=1)
    encryption_key: str = Field(min_length=44, max_length=44)
    database_url: str
    redis_url: str
    public_base_url: str = "http://localhost:8000"
    cors_origins: list[str] = ["http://localhost:3000"]
    payment_max_age_seconds: int = Field(default=120, ge=10, le=3600)
    settlement_mode: Literal["accepted", "mempool"] = "accepted"
    settlement_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    redelivery_window_seconds: int = Field(default=600, ge=0, le=3600)
    redelivery_max_attempts: int = Field(default=3, ge=0, le=10)
    session_ttl_seconds: int = Field(default=604_800, ge=300)
    kas_usd_price_url: str = "https://api.coingecko.com/api/v3/simple/price?ids=kaspa&vs_currencies=usd"
    oracle_cache_ttl_seconds: int = Field(default=60, ge=1, le=3600)
    oracle_max_staleness_seconds: int = Field(default=300, ge=1, le=86_400)
    oracle_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    oracle_min_sources: int = Field(default=2, ge=1, le=8)
    oracle_outlier_max_deviation: Decimal = Field(default=Decimal("0.5"), gt=0, le=10)
    oracle_source_coingecko: str = "https://api.coingecko.com/api/v3/simple/price?ids=kaspa&vs_currencies=usd"
    oracle_source_coinpaprika: str = "https://api.coinpaprika.com/v1/tickers/kas-kaspa"
    oracle_source_kraken: str = "https://api.kraken.com/0/public/Ticker?pair=KASUSD"
    oracle_source_mexc: str = "https://api.mexc.com/api/v3/ticker/price?symbol=KASUSDT"
    rate_limit_enabled: bool = True
    rate_limit_fail_closed: bool = True
    rl_merchant_create_per_hour: int = Field(default=5, ge=1)
    rl_auth_challenge_per_minute: int = Field(default=10, ge=1)
    rl_auth_verify_per_minute: int = Field(default=10, ge=1)
    rl_unpaid_proxy_per_minute: int = Field(default=300, ge=1)
    # CONTRACT v2.11: cap on one buyer's prepaid balance at one merchant (checked when a deposit is quoted).
    prepaid_max_balance_sompi: int = Field(default=10_000_000_000_000, ge=60_000_000, le=999_999_999_999_999_999)
    # CONTRACT v2.12: free trial calls consumed per client IP per hour (on top of the per-endpoint daily cap).
    rl_trial_calls_per_ip_per_hour: int = Field(default=30, ge=1)
    # CONTRACT v2.12 merchant webhooks: the outbox dispatcher and its retry schedule.
    webhook_dispatcher_enabled: bool = True
    webhook_poll_interval_seconds: float = Field(default=2.0, gt=0, le=60)
    webhook_batch_size: int = Field(default=20, ge=1, le=500)
    webhook_concurrency: int = Field(default=8, ge=1, le=64)
    webhook_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    webhook_max_attempts: int = Field(default=8, ge=1, le=20)
    webhook_backoff_base_seconds: float = Field(default=30.0, gt=0, le=3600)
    webhook_backoff_max_seconds: float = Field(default=3600.0, gt=0, le=86_400)
    webhook_lease_seconds: int = Field(default=60, ge=15, le=600)
    webhook_max_per_merchant: int = Field(default=10, ge=1, le=100)
    # CONTRACT v2.13 prepaid ledger worker: credit expiry and re-checks of submitted refund payouts.
    ledger_worker_enabled: bool = True
    ledger_poll_interval_seconds: float = Field(default=30.0, gt=0, le=3600)
    ledger_batch_size: int = Field(default=100, ge=1, le=1000)
    # A submitted refund payout that the node still cannot see after this long goes back to `requested` (its
    # tx_id stays on record and is still re-checked until `prepaid_payout_attempt_max_seconds`).
    prepaid_payout_pending_max_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    # Total time one upstream call may take (connect, response and body) before it counts as a timeout.
    upstream_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    # Failed-delivery auto-credit (run by the ledger worker): a paid call whose last delivery failed is credited once
    # its redelivery window, one full upstream attempt (UPSTREAM_TIMEOUT_SECONDS, rounded up) and this extra margin
    # have passed, so a redelivery claimed just before the window closed has finished first.
    failed_delivery_credit_grace_seconds: int = Field(default=30, ge=1, le=3600)
    # How far back the sweeper looks for uncredited failed deliveries (bounds each pass).
    failed_delivery_credit_lookback_seconds: int = Field(default=604_800, ge=3600, le=7_776_000)
    # An attempted payout tx_id the node has never confirmed is given up (`abandoned`) after this long.
    prepaid_payout_attempt_max_seconds: int = Field(default=604_800, ge=3600, le=31_536_000)
    # Re-check backoff after a transient node error on an attempted payout: base * 2^errors, capped.
    prepaid_payout_backoff_base_seconds: float = Field(default=30.0, gt=0, le=3600)
    prepaid_payout_backoff_max_seconds: float = Field(default=1800.0, gt=0, le=86_400)
    # A merchant's claim (`paying`) blocks the buyer's cancel for this long before a payout must be posted.
    prepaid_refund_claim_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    # Total time a refund can stay claimed, counted from its FIRST claim (`first_claimed_at`): no renewals past it.
    prepaid_refund_claim_max_seconds: int = Field(default=3600, ge=60, le=604_800)
    # After the buyer cancels a refund the merchant had claimed, its amount stays held this long before it becomes
    # spendable again (a payout broadcast just before the claim lapsed can still land and be recorded).
    prepaid_refund_cancel_cooldown_seconds: int = Field(default=1800, ge=0, le=604_800)
    # Pending (unconfirmed) payout attempts allowed per refund; more are refused with 409 `too_many_attempts`.
    prepaid_payout_max_pending_attempts: int = Field(default=3, ge=1, le=20)
    # Unseen attempts are re-checked after (attempt age x this fraction), capped by the backoff maximum.
    prepaid_payout_unseen_backoff_fraction: float = Field(default=0.1, gt=0, le=1)
    # POST /v1/prepaid/refunds/{id}/payout per merchant per minute.
    rl_refund_payout_per_merchant_per_minute: int = Field(default=20, ge=1)
    # Signed buyer refund/credit-status writes (POST /v1/prepaid/refunds, /cancel, /status, /credits/status)
    # per client IP per minute.
    rl_prepaid_refund_per_minute: int = Field(default=30, ge=1)
    # Reverse proxies (IPs or CIDRs, JSON list) whose X-Forwarded-For is trusted for the client IP used by rate
    # limits. Empty (default): X-Forwarded-For is ignored and the TCP peer address is the client.
    trusted_proxy_ips: list[str] = []
    # Testnet faucet (POST /v1/faucet/claim). Disabled unless FAUCET_PRIVATE_KEY (64 hex chars) is set; a dedicated hot
    # wallet key, never a buyer, treasury or merchant key. Enabling it on any network but testnet-10 refuses to start.
    faucet_private_key: SecretStr | None = None
    faucet_amount_sompi: int = Field(default=5_000_000_000, ge=1, le=1_000_000_000_000)
    faucet_daily_cap_sompi: int = Field(default=500_000_000_000, ge=1)
    faucet_max_network_fee_sompi: int = Field(default=10_000_000, ge=1)

    @field_validator("faucet_private_key")
    @classmethod
    def _faucet_key(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None or not v.get_secret_value().strip():
            return None
        raw = v.get_secret_value().strip().lower()
        if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
            raise ValueError("FAUCET_PRIVATE_KEY must be 64 hex characters")
        return SecretStr(raw)

    @property
    def faucet_enabled(self) -> bool:
        return self.faucet_private_key is not None

    @field_validator("trusted_proxy_ips")
    @classmethod
    def _proxies(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for item in v:
            try:
                out.append(str(ipaddress.ip_network(item.strip(), strict=False)))
            except ValueError as exc:
                raise ValueError(f"TRUSTED_PROXY_IPS entry {item!r} is not an IP address or CIDR") from exc
        return out

    @property
    def trusted_proxy_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return tuple(ipaddress.ip_network(item) for item in self.trusted_proxy_ips)

    @property
    def address_prefix(self) -> str:
        return NETWORK_ADDRESS_PREFIX[self.kaspa_network]

    @property
    def failed_delivery_credit_wait_seconds(self) -> int:
        """Seconds after the redelivery window before the sweep may credit: one upstream attempt plus the grace."""
        return math.ceil(self.upstream_timeout_seconds) + self.failed_delivery_credit_grace_seconds

    @model_validator(mode="after")
    def _faucet_testnet_only(self) -> "Settings":
        if self.faucet_private_key is not None and self.kaspa_network != "kaspa:testnet-10":
            raise ValueError("the faucet is testnet only: FAUCET_PRIVATE_KEY requires KASPA_NETWORK=kaspa:testnet-10")
        return self

    @model_validator(mode="after")
    def _treasury_matches_network(self) -> "Settings":
        if not self.kasgate_treasury_address.startswith(f"{self.address_prefix}:"):
            raise ValueError(
                f"KASGATE_TREASURY_ADDRESS must start with '{self.address_prefix}:' on {self.kaspa_network}"
            )
        from kaspa import Address

        if not Address.validate(self.kasgate_treasury_address):
            raise ValueError("KASGATE_TREASURY_ADDRESS is not a valid Kaspa address (bad checksum or placeholder)")
        if self.kaspa_network == "kaspa:mainnet" and not self.public_base_url.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL must use https:// on kaspa:mainnet")
        if self.public_base_url.endswith("/"):
            self.public_base_url = self.public_base_url.rstrip("/")
        default_coingecko = "https://api.coingecko.com/api/v3/simple/price?ids=kaspa&vs_currencies=usd"
        if self.kas_usd_price_url != default_coingecko and self.oracle_source_coingecko == default_coingecko:
            self.oracle_source_coingecko = self.kas_usd_price_url
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
