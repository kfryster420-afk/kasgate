"""Red-team pass over the pre-PR-#1 features on main @4d39527 (prepaid credit, free trials, signed webhooks).

`test_hole_*` fails on 4d39527 and passes once the finding is fixed. Lows are listed in
/workspace/kg-wt/rt-earlier-features-review.md without tests.
"""

from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import PrepaidBalance
from tests.conftest import Seed
from tests.test_prepaid import CREDIT_SOMPI, DEPOSIT_SOMPI, post_deposit, quote_deposit, shop  # noqa: F401


async def test_hole_two_quotes_taken_together_settle_past_the_prepaid_balance_cap(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: Any,
) -> None:
    """The cap is checked only at /deposits/quote against the balance of that moment. Quotes taken before any
    deposit lands each pass, and POST /deposits settles and credits without re-checking, so N quotes put the
    balance at N x credit regardless of prepaid_max_balance_sompi."""
    override_settings(prepaid_max_balance_sompi=60_000_000)  # room for ONE 40_000_000 credit
    first = await quote_deposit(client, DEPOSIT_SOMPI)
    second = await quote_deposit(client, DEPOSIT_SOMPI)  # balance still 0 here, so this quote is issued too
    assert (await post_deposit(client, first, tx_id="a1" * 32)).status_code == 201
    over = await post_deposit(client, second, tx_id="a2" * 32)

    async with sessionmaker() as session:
        balance = (await session.execute(select(PrepaidBalance.balance_sompi))).scalar_one()
    # Fixed behaviour: the second deposit is refused before it is submitted (nothing settles), so the cap holds.
    assert over.status_code == 422, over.text
    assert balance == CREDIT_SOMPI <= 60_000_000
