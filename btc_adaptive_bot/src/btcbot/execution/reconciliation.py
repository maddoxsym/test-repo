"""Fill reconciliation — deciding whether an accepted order actually filled.

OKX is eventually consistent across its read endpoints, and they do not settle
in the same order. After a market order is accepted you may see, in this order:

1. ``/api/v5/trade/order`` reports ``state=filled`` with ``accFillSz`` set —
   this reflects the match engine and settles first;
2. ``/api/v5/account/positions`` shows the position;
3. ``/api/v5/trade/fills`` lists the individual trades — routinely **last**,
   sometimes by a second or more.

Asking only the fills endpoint, immediately, therefore produces the exact
symptom this module exists to fix: a real, filled, visible position reported as
"no fill matched the client order ID".

The rule this module encodes: **order details are the authority on whether the
order filled; the fills endpoint is the authority on the per-fill detail.** A
missing per-fill record when order details already prove the fill is a delay,
not a failure — and is reported as such.

Identifiers
-----------

Reconciliation keys on OKX's ``ordId`` whenever it is known, because that is
what the exchange assigns and what every other endpoint keys on. ``clOrdId`` is
a fallback for the one case where ``ordId`` is unavailable: a transport failure
after the order was sent but before its acknowledgement arrived.

This module performs **no order submission of any kind**. It reads, and it
reports. A caller that cannot confirm an order must reconcile — never resubmit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..exchange.models import Execution, OrderDetails
from ..utils.errors import ApiError, TransportError
from ..utils.logging import get_logger

log = get_logger(__name__)

# Delay *before* each attempt, so the first is immediate. Total wall time is
# the sum: 0 + .25 + .5 + 1 + 2 + 2 + 2 = 7.75s of waiting across 7 attempts,
# inside the 8–10s budget a market order on a liquid perp needs.
ORDER_POLL_BACKOFF: tuple[float, ...] = (0.0, 0.25, 0.5, 1.0, 2.0, 2.0, 2.0)

# The fills endpoint lags the order endpoint, but once order details prove the
# fill we are only waiting for bookkeeping — a shorter budget (1.75s) is enough,
# and a miss is a warning rather than a failure.
FILLS_POLL_BACKOFF: tuple[float, ...] = (0.0, 0.25, 0.5, 1.0)


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """What reconciliation could actually prove about an order.

    ``confirmed`` means order details showed contracts changing hands. It is
    the only field a caller should gate "the order worked" on — never the
    presence of per-fill records, which arrive later.
    """

    order: OrderDetails | None
    fills: tuple[Execution, ...] = ()
    confirmed: bool = False
    order_attempts: int = 0
    fill_attempts: int = 0
    waited_seconds: float = 0.0
    detail: str = ""

    @property
    def state(self) -> str:
        """OKX's state, or ``unconfirmed`` when nothing could be established."""
        return self.order.state if self.order else "unconfirmed"

    @property
    def filled(self) -> bool:
        return bool(self.order and self.order.is_filled)

    @property
    def partially_filled(self) -> bool:
        return bool(self.order and self.order.is_partially_filled)

    @property
    def canceled(self) -> bool:
        return bool(self.order and self.order.is_canceled)

    @property
    def fills_delayed(self) -> bool:
        """The fill is proven but its per-fill records have not appeared yet.

        Bookkeeping detail only — the position is real either way.
        """
        return self.confirmed and not self.fills

    @property
    def filled_size(self) -> float:
        return self.order.filled_size if self.order else 0.0

    @property
    def avg_price(self) -> float:
        return self.order.avg_price if self.order else 0.0

    @property
    def fill_ids(self) -> list[str]:
        return [fill.exec_id for fill in self.fills if fill.exec_id]

    def describe(self) -> str:
        return self.detail


def match_fills(
    fills: Sequence[Execution],
    *,
    order_id: str | None = None,
    client_order_id: str | None = None,
) -> list[Execution]:
    """Select the fills belonging to one order — ``ordId`` first.

    ``ordId`` is exact and exchange-assigned. ``clOrdId`` is only consulted
    when the ``ordId`` match found nothing, because a fill whose ``clOrdId``
    field the exchange left blank would otherwise be missed — and because an
    order whose ``ordId`` we never learned still needs reconciling.
    """
    if order_id:
        by_order_id = [fill for fill in fills if fill.order_id == order_id]
        if by_order_id:
            return by_order_id
    if client_order_id:
        return [fill for fill in fills if fill.client_order_id == client_order_id]
    return []


@dataclass(slots=True)
class FillReconciler:
    """Polls OKX until an order's outcome is established, or the budget runs out.

    Used by both the production executor and the smoke test so there is one
    reconciliation behaviour to reason about and test, not two.
    """

    client: object  # OkxDemoClient, or any object with the same read methods
    order_backoff: tuple[float, ...] = ORDER_POLL_BACKOFF
    fills_backoff: tuple[float, ...] = FILLS_POLL_BACKOFF
    fills_limit: int = 100
    _slept: float = field(default=0.0, init=False)

    async def reconcile(
        self,
        inst_id: str,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
        fetch_fills: bool = True,
    ) -> ReconciliationOutcome:
        """Establish an order's outcome. Reads only — never submits anything."""
        if not order_id and not client_order_id:
            raise ValueError("reconciliation requires order_id or client_order_id")

        self._slept = 0.0
        details, attempts = await self._poll_order(
            inst_id, order_id=order_id, client_order_id=client_order_id
        )

        if details is None or not (details.proves_fill or details.is_canceled):
            seen = f"last seen {details.describe()}" if details else "never seen at the exchange"
            return ReconciliationOutcome(
                order=details,
                order_attempts=attempts,
                waited_seconds=self._slept,
                detail=(
                    "order details could not confirm the order within "
                    f"{self._slept:.2f}s ({attempts} attempt(s)) — {seen}"
                ),
            )

        # Prefer the exchange's own ordId from here on: it is what the fills
        # endpoint keys on, and it may be the first time we have seen it.
        resolved_order_id = details.order_id or order_id
        confirmed = details.proves_fill

        fills: tuple[Execution, ...] = ()
        fill_attempts = 0
        if confirmed and fetch_fills:
            matched, fill_attempts = await self._poll_fills(
                inst_id,
                order_id=resolved_order_id,
                client_order_id=details.client_order_id or client_order_id,
            )
            fills = tuple(matched)

        return ReconciliationOutcome(
            order=details,
            fills=fills,
            confirmed=confirmed,
            order_attempts=attempts,
            fill_attempts=fill_attempts,
            waited_seconds=self._slept,
            detail=self._describe(details, fills, attempts),
        )

    # --- polling ---------------------------------------------------------

    async def _poll_order(
        self, inst_id: str, *, order_id: str | None, client_order_id: str | None
    ) -> tuple[OrderDetails | None, int]:
        """Poll order details until the outcome is settled or the budget ends.

        Stops on a filled, partially filled, or canceled order. A ``live``
        order (or an unreadable one) keeps polling: it has not resolved yet.
        """
        last_seen: OrderDetails | None = None
        attempts = 0
        for delay in self.order_backoff:
            await self._sleep(delay)
            attempts += 1
            try:
                details = await self.client.get_order(   # type: ignore[attr-defined]
                    inst_id, order_id=order_id, client_order_id=client_order_id
                )
            except (ApiError, TransportError) as exc:
                # A transient read failure must not be read as "did not fill".
                log.debug("RECONCILE", f"order lookup attempt {attempts} failed: {exc}")
                continue
            if details is None:
                continue
            last_seen = details
            if details.proves_fill or details.is_canceled:
                return (details, attempts)
        # Budget spent without the order settling. Whatever was last seen is
        # returned so the caller can report the real state (usually `live`),
        # but nothing here has proven a fill.
        return (last_seen, attempts)

    async def _poll_fills(
        self, inst_id: str, *, order_id: str | None, client_order_id: str | None
    ) -> tuple[list[Execution], int]:
        """Fetch the per-fill records for a fill order details already proved."""
        for attempt, delay in enumerate(self.fills_backoff, start=1):
            await self._sleep(delay)
            try:
                fills = await self.client.get_executions(   # type: ignore[attr-defined]
                    inst_id, limit=self.fills_limit
                )
            except (ApiError, TransportError) as exc:
                log.debug("RECONCILE", f"fills lookup attempt {attempt} failed: {exc}")
                continue
            matched = match_fills(
                fills, order_id=order_id, client_order_id=client_order_id
            )
            if matched:
                return (matched, attempt)
        return ([], len(self.fills_backoff))

    async def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)
            self._slept += seconds

    @staticmethod
    def _describe(
        details: OrderDetails, fills: tuple[Execution, ...], attempts: int
    ) -> str:
        base = f"{details.describe()} after {attempts} poll(s)"
        if details.is_canceled:
            return f"order was canceled by the exchange — {base}"
        if not details.proves_fill:
            return f"order has not filled — {base}"
        if fills:
            return f"{base}; {len(fills)} fill record(s) {[f.exec_id for f in fills]}"
        return f"{base}; per-fill records not published yet (the fill itself is confirmed)"
