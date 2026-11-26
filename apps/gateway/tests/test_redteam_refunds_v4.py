"""Verification of CONTRACT v2.13 refunds at feat/prepaid-refund @ bd4804f.

`test_hole_*` states an invariant this revision does not keep yet.
"""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.payments import PaymentVerificationError
from tests.conftest import Seed, StubEngine
from tests.test_prepaid import fund
from tests.test_refund_hardening import claim
from tests.test_refunds import open_refund, refund_row, shop

__all__ = ["shop"]


async def test_hole_a_claim_renewal_must_not_backfill_a_missing_first_claimed_daa_score(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
) -> None:
    """If the node cannot be asked at the first claim, `first_claimed_daa_score` is null and the pre-cancel UTXO
    filter is skipped (CONTRACTS). A later successful claim renewal currently writes the later virtual DAA via
    `coalesce`, so a payout accepted after the first claim but at or below the renewal score would be ignored by
    the scan and a cancel could release the credit."""
    await fund(client)
    refund = await open_refund(client)
    stub_engine.virtual_daa_error = PaymentVerificationError("service_unavailable", "node down")
    assert (await claim(client, shop, refund.id)).status_code == 200
    first = await refund_row(sessionmaker, refund.id)
    assert first.first_claimed_at is not None and first.first_claimed_daa_score is None

    stub_engine.virtual_daa_error = None
    stub_engine.virtual_daa_score_value = 2_000_000
    assert (await claim(client, shop, refund.id)).status_code == 200
    renewed = await refund_row(sessionmaker, refund.id)
    assert renewed.first_claimed_daa_score is None, (
        f"renewal backfilled first_claimed_daa_score={renewed.first_claimed_daa_score}; "
        "the pre-cancel scan would then ignore payouts accepted after the first claim"
    )
