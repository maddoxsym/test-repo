"""Order-safety guard: duplicates, stale signals, races, and disclosure.

Layered protection, cheapest check first:

1. **In-flight set** (memory) — instant rejection of a concurrent retry.
2. **Database uniqueness** — ``UNIQUE(setup_id, intent)`` on ``demo_orders``.
   This survives restarts and is the authoritative guard: the row is inserted
   *before* the HTTP request, so a crash mid-submit still blocks a repeat.
3. **Signal age** — a signal older than the staleness budget is discarded
   rather than acted on with a price that has moved.
4. **Pre-order disclosure** — every field the brief requires is logged before
   submission, so the console is a complete audit trail.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..database.repositories import DemoOrderRepository
from ..utils.logging import get_logger
from ..utils.timeutil import now_utc

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    allowed: bool
    reason: str = ""


class OrderSafetyGuard:
    """Prevents duplicate, stale, and racing order submissions."""

    def __init__(
        self,
        repository: DemoOrderRepository,
        *,
        max_signal_age_seconds: float = 120.0,
    ) -> None:
        self.repo = repository
        self._max_signal_age = max_signal_age_seconds
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()
        self.duplicate_rejections = 0
        self.stale_rejections = 0

    def check(
        self,
        *,
        setup_id: str,
        intent: str,
        signal_time: datetime | None = None,
    ) -> GuardVerdict:
        """Run all pre-submission safety checks."""
        key = f"{setup_id}:{intent}"

        with self._lock:
            if key in self._in_flight:
                self.duplicate_rejections += 1
                return GuardVerdict(False, f"an order for {key} is already in flight")

        if self.repo.has_open_intent(setup_id, intent):
            self.duplicate_rejections += 1
            return GuardVerdict(
                False, f"setup {setup_id} already has a recorded '{intent}' order"
            )

        if signal_time is not None:
            age = (now_utc() - signal_time).total_seconds()
            if age > self._max_signal_age:
                self.stale_rejections += 1
                return GuardVerdict(
                    False,
                    f"signal is {age:.0f}s old (limit {self._max_signal_age:.0f}s) — "
                    "price has likely moved away from the setup",
                )

        return GuardVerdict(True)

    def begin(self, setup_id: str, intent: str) -> bool:
        """Claim the in-flight slot. False means another caller already has it."""
        key = f"{setup_id}:{intent}"
        with self._lock:
            if key in self._in_flight:
                return False
            self._in_flight.add(key)
            return True

    def finish(self, setup_id: str, intent: str) -> None:
        with self._lock:
            self._in_flight.discard(f"{setup_id}:{intent}")

    def in_flight_count(self) -> int:
        with self._lock:
            return len(self._in_flight)

    def snapshot(self) -> dict[str, Any]:
        return {
            "in_flight": self.in_flight_count(),
            "duplicate_rejections": self.duplicate_rejections,
            "stale_rejections": self.stale_rejections,
        }


def disclose_order(
    *,
    strategy_id: str,
    strategy_version: str,
    direction: str,
    symbol: str,
    category: str,
    order_type: str,
    quantity: str,
    estimated_notional: float,
    entry_reference: float,
    stop_price: float,
    target_price: float | None,
    estimated_risk_pct: float,
    regime: str,
    confidence: float,
    client_order_id: str,
) -> None:
    """Log every mandated field immediately before an order is submitted."""
    log.info("ORDER", "─" * 46)
    log.info("ORDER", f"  STRATEGY    {strategy_id} v{strategy_version}")
    log.info("ORDER", f"  DIRECTION   {direction.upper()}")
    log.info("ORDER", f"  SYMBOL      {symbol} ({category})")
    log.info("ORDER", f"  ORDER TYPE  {order_type}")
    log.info("ORDER", f"  QUANTITY    {quantity}")
    log.info("ORDER", f"  NOTIONAL    ${estimated_notional:,.2f} (estimated)")
    log.info("ORDER", f"  ENTRY       {entry_reference:,.2f}")
    log.info("ORDER", f"  STOP        {stop_price:,.2f}")
    log.info(
        "ORDER",
        f"  TARGET      {target_price:,.2f}" if target_price else "  TARGET      none (managed exit)",
    )
    log.info("ORDER", f"  RISK        {estimated_risk_pct * 100:.3f}% of demo equity")
    log.info("ORDER", f"  REGIME      {regime}")
    log.info("ORDER", f"  CONFIDENCE  {confidence:.2f}")
    log.info("ORDER", f"  CLIENT ID   {client_order_id}")
    log.info("ORDER", "─" * 46)
