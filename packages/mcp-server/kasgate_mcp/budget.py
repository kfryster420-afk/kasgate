"""Atomic session spend tracker for budgeted MCP paid calls.

Reservations are taken against the quoted price before anything is signed. Concurrent callers
serialize on a lock so two in-flight calls cannot together exceed the session cap. A failed call
releases its reservation; a successful call commits the reserved (quoted) amount, never more.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


class BudgetError(Exception):
    """The quoted price would exceed the per-call maximum or the remaining session cap."""


@dataclass
class SpendBudget:
    per_call_max_sompi: int
    session_cap_sompi: int
    spent_sompi: int = 0
    reserved_sompi: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.per_call_max_sompi, int)
            or isinstance(self.per_call_max_sompi, bool)
            or self.per_call_max_sompi <= 0
        ):
            raise ValueError("per_call_max_sompi must be a positive integer")
        if (
            not isinstance(self.session_cap_sompi, int)
            or isinstance(self.session_cap_sompi, bool)
            or self.session_cap_sompi <= 0
        ):
            raise ValueError("session_cap_sompi must be a positive integer")

    @property
    def remaining_sompi(self) -> int:
        return max(0, self.session_cap_sompi - self.spent_sompi - self.reserved_sompi)

    def snapshot(self) -> dict[str, int]:
        return {
            "per_call_max_sompi": self.per_call_max_sompi,
            "session_cap_sompi": self.session_cap_sompi,
            "spent_sompi": self.spent_sompi,
            "reserved_sompi": self.reserved_sompi,
            "remaining_sompi": self.remaining_sompi,
        }

    async def reserve(self, amount_sompi: int, *, per_call_max_sompi: int | None = None) -> int:
        """Reserve `amount_sompi` against the session cap. Returns the reserved amount.

        `per_call_max_sompi` can only tighten the configured per-call maximum, never raise it.
        """
        if not isinstance(amount_sompi, int) or isinstance(amount_sompi, bool) or amount_sompi <= 0:
            raise BudgetError("quoted price must be a positive integer number of sompi")
        cap = self.per_call_max_sompi
        if per_call_max_sompi is not None:
            if (
                not isinstance(per_call_max_sompi, int)
                or isinstance(per_call_max_sompi, bool)
                or per_call_max_sompi <= 0
            ):
                raise BudgetError("per-call maximum must be a positive integer number of sompi")
            cap = min(cap, per_call_max_sompi)
        async with self._lock:
            if amount_sompi > cap:
                raise BudgetError(
                    f"quoted price {amount_sompi} sompi exceeds the per-call maximum of {cap} sompi"
                )
            if self.spent_sompi + self.reserved_sompi + amount_sompi > self.session_cap_sompi:
                remaining = self.remaining_sompi
                raise BudgetError(
                    f"quoted price {amount_sompi} sompi exceeds the remaining session budget of {remaining} sompi"
                )
            self.reserved_sompi += amount_sompi
            return amount_sompi

    async def commit(self, amount_sompi: int) -> None:
        async with self._lock:
            if amount_sompi > self.reserved_sompi:
                raise BudgetError("cannot commit more than is reserved")
            self.reserved_sompi -= amount_sompi
            self.spent_sompi += amount_sompi

    async def release(self, amount_sompi: int) -> None:
        async with self._lock:
            if amount_sompi > self.reserved_sompi:
                amount_sompi = self.reserved_sompi
            self.reserved_sompi -= amount_sompi
