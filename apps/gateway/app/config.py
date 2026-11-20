from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
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
    rate_limit_enabled: bool = True
    rate_limit_fail_closed: bool = True
    rl_merchant_create_per_hour: int = Field(default=5, ge=1)
    rl_auth_challenge_per_minute: int = Field(default=10, ge=1)
    rl_auth_verify_per_minute: int = Field(default=10, ge=1)
    rl_unpaid_proxy_per_minute: int = Field(default=300, ge=1)

    @property
    def address_prefix(self) -> str:
        return NETWORK_ADDRESS_PREFIX[self.kaspa_network]

    @model_validator(mode="after")
    def _treasury_matches_network(self) -> "Settings":
        if not self.kasgate_treasury_address.startswith(f"{self.address_prefix}:"):
            raise ValueError(
                f"KASGATE_TREASURY_ADDRESS must start with '{self.address_prefix}:' on {self.kaspa_network}"
            )
        if self.kaspa_network == "kaspa:mainnet" and not self.public_base_url.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL must use https:// on kaspa:mainnet")
        if self.public_base_url.endswith("/"):
            self.public_base_url = self.public_base_url.rstrip("/")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
