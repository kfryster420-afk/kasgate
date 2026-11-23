"""Public catalog models returned by GET /v1/catalog and GET /.well-known/x402."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .constants import X402_SCHEME


class CatalogEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    description: str | None = None
    methods: list[str]
    price_currency: str = "KAS"
    price_sompi: int | None = Field(default=None, gt=0)
    price_kas: str | None = None
    price_usd_micros: int | None = Field(default=None, gt=0)
    price_usd: str | None = None
    network: str
    schemes: list[str]
    paid_url: str
    input_schema: dict[str, Any] | None = None

    def is_within_max_price(self, max_price_sompi: int) -> bool:
        # USD listings have no locked sompi price until quote/402 time, so they are not payable from the catalog alone.
        return self.price_sompi is not None and self.price_sompi <= max_price_sompi


class CatalogPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_version: Literal[1] = 1
    x402_version: Literal[2] = 2
    scheme: Literal["kaspa-exact-v1"] = X402_SCHEME  # type: ignore[assignment]
    asset: Literal["KAS"] = "KAS"
    network: str
    gateway_base_url: str
    items: list[CatalogEndpoint]
    next_cursor: str | None = None
    limit: int = Field(ge=1)


class DiscoverResult(BaseModel):
    """A catalog page plus the client's max_price_sompi circuit breaker.

    Discovery never pays. Endpoints priced above the cap stay in `items`; `payable` lists only
    those the client would be willing to sign for.
    """

    model_config = ConfigDict(extra="forbid")

    catalog: CatalogPage
    max_price_sompi: int = Field(gt=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def items(self) -> list[CatalogEndpoint]:
        return self.catalog.items

    @computed_field  # type: ignore[prop-decorator]
    @property
    def next_cursor(self) -> str | None:
        return self.catalog.next_cursor

    @computed_field  # type: ignore[prop-decorator]
    @property
    def payable(self) -> list[CatalogEndpoint]:
        return [item for item in self.catalog.items if item.is_within_max_price(self.max_price_sompi)]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def exceeding_max_price(self) -> list[CatalogEndpoint]:
        return [item for item in self.catalog.items if not item.is_within_max_price(self.max_price_sompi)]
