"""Circuit breakers and SAFE_MODE.

Even in a demo, a software bug must not be allowed to wreck the experiment's
data. These checks sit between the engine's intent and any action with
consequences, and they trip on the conditions the brief lists: impossible
prices, absurd quantities, invalid balances, stale data, repeated API errors,
exchange/database mismatches, corrupt strategy output, abnormal order rates, and
duplicate order attempts.

When a breaker trips the system enters ``SAFE_MODE``: market data keeps flowing
and shadow research continues, but real order submission stops until the
condition clears and a cooldown elapses.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from ..config.schema import SafetyConfig
from ..utils.logging import get_logger
from ..utils.numeric import safe_div
from ..utils.timeutil import now_utc

log = get_logger(__name__)


class BreakerType(str, Enum):
    IMPOSSIBLE_PRICE = "impossible_price"
    PRICE_JUMP = "price_jump"
    EXTREME_QUANTITY = "extreme_quantity"
    INVALID_BALANCE = "invalid_balance"
    STALE_DATA = "stale_data"
    API_ERRORS = "repeated_api_errors"
    STATE_MISMATCH = "exchange_state_mismatch"
    DATABASE_FAILURE = "database_failure"
    CORRUPT_STRATEGY_OUTPUT = "corrupt_strategy_output"
    ORDER_FREQUENCY = "abnormal_order_frequency"
    DUPLICATE_ORDER = "duplicate_order_attempt"
    DEMO_UNVERIFIED = "demo_unverified"
    CLOCK_DRIFT = "clock_drift"
    LIQUIDATION_RISK = "liquidation_risk"
    UNCONFIRMED_FILL = "unconfirmed_fill"
    UNPROTECTED_POSITION = "unprotected_position"


@dataclass(slots=True)
class BreakerTrip:
    breaker: BreakerType
    reason: str
    tripped_at: datetime
    details: dict[str, Any] = field(default_factory=dict)


class SafeMode:
    """Tracks whether the system may act, and why not."""

    def __init__(self, cooldown_seconds: int = 300) -> None:
        self._active = False
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._entered_at: datetime | None = None
        self._trips: list[BreakerTrip] = []
        self._current_reason: str = ""

    @property
    def active(self) -> bool:
        return self._active

    @property
    def reason(self) -> str:
        return self._current_reason

    @property
    def entered_at(self) -> datetime | None:
        return self._entered_at

    def enter(self, trip: BreakerTrip) -> None:
        self._trips.append(trip)
        self._current_reason = f"{trip.breaker.value}: {trip.reason}"
        if not self._active:
            self._active = True
            self._entered_at = trip.tripped_at
            log.critical("SAFETY", f"SAFE_MODE ENTERED — {self._current_reason}")
        else:
            self._entered_at = trip.tripped_at  # extend the cooldown

    def try_exit(self) -> bool:
        """Leave SAFE_MODE once the cooldown has elapsed. Returns True on exit."""
        if not self._active or self._entered_at is None:
            return False
        if now_utc() - self._entered_at < self._cooldown:
            return False
        self._active = False
        self._entered_at = None
        log.info("SAFETY", f"SAFE_MODE cleared (was: {self._current_reason})")
        self._current_reason = ""
        return True

    def recent_trips(self, limit: int = 10) -> list[BreakerTrip]:
        return self._trips[-limit:]

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "reason": self._current_reason,
            "entered_at": self._entered_at.isoformat() if self._entered_at else None,
            "trip_count": len(self._trips),
            "recent": [
                {"breaker": t.breaker.value, "reason": t.reason, "at": t.tripped_at.isoformat()}
                for t in self._trips[-5:]
            ],
        }


class CircuitBreakers:
    """The checks themselves."""

    def __init__(self, config: SafetyConfig) -> None:
        self.config = config
        self.safe_mode = SafeMode(config.safe_mode_cooldown_seconds)
        self._order_times: deque[datetime] = deque(maxlen=500)
        self._last_price: float | None = None
        self._duplicate_attempts = 0
        self._db_failures = 0

    # --- individual checks -----------------------------------------------

    def check_price(self, price: float) -> BreakerTrip | None:
        """Reject impossible prices and implausible instantaneous jumps."""
        if not math.isfinite(price) or price <= 0:
            return self._trip(BreakerType.IMPOSSIBLE_PRICE, f"non-finite or non-positive price {price!r}")
        if price < self.config.price_sanity_min or price > self.config.price_sanity_max:
            return self._trip(
                BreakerType.IMPOSSIBLE_PRICE,
                f"price {price:,.2f} outside the sanity band "
                f"[{self.config.price_sanity_min:,.0f}, {self.config.price_sanity_max:,.0f}]",
            )
        if self._last_price is not None and self._last_price > 0:
            jump = abs(safe_div(price - self._last_price, self._last_price))
            if jump > self.config.price_jump_reject_pct:
                trip = self._trip(
                    BreakerType.PRICE_JUMP,
                    f"price jumped {jump * 100:.1f}% ({self._last_price:,.2f} → {price:,.2f}) "
                    "in one update — likely a bad tick",
                )
                self._last_price = price
                return trip
        self._last_price = price
        return None

    def check_quantity(self, quantity: float, *, equity: float, price: float) -> BreakerTrip | None:
        """Reject quantities that cannot possibly be intended."""
        if not math.isfinite(quantity) or quantity <= 0:
            return self._trip(
                BreakerType.EXTREME_QUANTITY, f"non-finite or non-positive quantity {quantity!r}"
            )
        notional = quantity * price
        if equity > 0 and notional > equity * 5:
            return self._trip(
                BreakerType.EXTREME_QUANTITY,
                f"order notional ${notional:,.2f} is more than 5× account equity "
                f"${equity:,.2f} — refusing",
            )
        return None

    def check_balance(self, equity: float, available: float) -> BreakerTrip | None:
        if not math.isfinite(equity) or equity < 0:
            return self._trip(BreakerType.INVALID_BALANCE, f"invalid equity {equity!r}")
        if not math.isfinite(available) or available < 0:
            return self._trip(BreakerType.INVALID_BALANCE, f"invalid available balance {available!r}")
        if available > equity * 1.5 and equity > 0:
            return self._trip(
                BreakerType.INVALID_BALANCE,
                f"available ${available:,.2f} implausibly exceeds equity ${equity:,.2f}",
            )
        return None

    def check_data_freshness(self, healthy: bool, detail: str) -> BreakerTrip | None:
        if not healthy:
            return self._trip(BreakerType.STALE_DATA, f"market data unhealthy: {detail}")
        return None

    def check_api_errors(self, consecutive_errors: int) -> BreakerTrip | None:
        if consecutive_errors >= self.config.max_consecutive_api_errors:
            return self._trip(
                BreakerType.API_ERRORS,
                f"{consecutive_errors} consecutive API errors "
                f"(limit {self.config.max_consecutive_api_errors})",
            )
        return None

    def check_order_rate(self) -> BreakerTrip | None:
        """Guard against a runaway loop firing orders."""
        cutoff = now_utc() - timedelta(minutes=1)
        recent = [t for t in self._order_times if t >= cutoff]
        if len(recent) >= self.config.max_orders_per_minute:
            return self._trip(
                BreakerType.ORDER_FREQUENCY,
                f"{len(recent)} orders in the last minute "
                f"(limit {self.config.max_orders_per_minute}) — possible runaway loop",
            )
        return None

    def check_state_consistency(
        self, *, exchange_positions: int, ledger_positions: int
    ) -> BreakerTrip | None:
        if exchange_positions != ledger_positions:
            return self._trip(
                BreakerType.STATE_MISMATCH,
                f"exchange reports {exchange_positions} position(s) but the ledger has "
                f"{ledger_positions} — reconciliation required before trading",
            )
        return None

    def check_strategy_output(self, signal: Any) -> BreakerTrip | None:
        """Catch structurally invalid strategy output before it becomes an order."""
        try:
            entry = float(signal.entry_reference)
            stop = float(signal.stop_price)
            confidence = float(signal.confidence)
        except (AttributeError, TypeError, ValueError) as exc:
            return self._trip(
                BreakerType.CORRUPT_STRATEGY_OUTPUT, f"signal missing required numeric fields: {exc}"
            )
        if not all(math.isfinite(v) for v in (entry, stop, confidence)):
            return self._trip(
                BreakerType.CORRUPT_STRATEGY_OUTPUT, "signal contains non-finite values"
            )
        if entry <= 0 or stop <= 0:
            return self._trip(
                BreakerType.CORRUPT_STRATEGY_OUTPUT, f"non-positive prices (entry {entry}, stop {stop})"
            )
        if not 0.0 <= confidence <= 1.0:
            return self._trip(
                BreakerType.CORRUPT_STRATEGY_OUTPUT, f"confidence {confidence} outside [0, 1]"
            )
        return None

    def check_clock_drift(self, drift_ms: int) -> BreakerTrip | None:
        """Pause authenticated trading when the local clock wanders.

        OKX rejects requests whose timestamp strays too far from server time,
        and a machine whose clock cannot be trusted cannot stamp orders. The
        drift is measured against the exchange's own time endpoint.
        """
        if abs(drift_ms) > self.config.max_clock_drift_ms:
            return self._trip(
                BreakerType.CLOCK_DRIFT,
                f"local clock differs from OKX server time by {drift_ms}ms "
                f"(limit {self.config.max_clock_drift_ms}ms) — authenticated trading paused; "
                "enable NTP time sync",
            )
        return None

    def check_liquidation_risk(
        self, *, margin_ratio: float | None, inst_id: str
    ) -> BreakerTrip | None:
        """Trip when a live position's margin ratio degrades toward liquidation.

        OKX's ``mgnRatio`` shrinks toward 1.0 as a position approaches its
        maintenance level; below the configured floor the position must be
        flattened and trading paused.
        """
        if margin_ratio is None:
            return None
        if margin_ratio <= 0:
            return None
        if margin_ratio < self.config.liquidation_margin_ratio_floor:
            return self._trip(
                BreakerType.LIQUIDATION_RISK,
                f"{inst_id} margin ratio {margin_ratio:.2f} is below the floor "
                f"{self.config.liquidation_margin_ratio_floor:.2f} — flattening and pausing",
            )
        return None

    def record_duplicate_attempt(self, setup_id: str) -> BreakerTrip | None:
        self._duplicate_attempts += 1
        # One duplicate is a benign race; a stream of them is a loop.
        if self._duplicate_attempts >= 5:
            return self._trip(
                BreakerType.DUPLICATE_ORDER,
                f"{self._duplicate_attempts} duplicate order attempts (latest setup {setup_id})",
            )
        return None

    def record_database_failure(self, error: str) -> BreakerTrip | None:
        self._db_failures += 1
        if self._db_failures >= 3:
            return self._trip(
                BreakerType.DATABASE_FAILURE, f"{self._db_failures} database failures: {error}"
            )
        return None

    def record_unconfirmed_order(
        self, *, order_id: str, client_order_id: str, detail: str
    ) -> BreakerTrip:
        """An accepted order whose outcome the exchange would not confirm.

        This is the one state where the system genuinely does not know whether
        it holds a position, so it stops rather than guessing. Trading pauses,
        the caller reconciles against the exchange, and **no replacement order
        is ever sent** — a blind retry here is how one intended position
        becomes two.
        """
        return self._trip(
            BreakerType.UNCONFIRMED_FILL,
            f"order {order_id or client_order_id} accepted but not confirmed: {detail}",
            order_id=order_id,
            client_order_id=client_order_id,
        )

    def record_unprotected_position(
        self, *, inst_id: str, detail: str, closed: bool
    ) -> BreakerTrip:
        """A real position existed without a verified exchange-side stop.

        The most serious state this system can reach: real exposure with no
        protection that survives this process. Trading stops immediately and
        does not resume on a timer alone — the operator should confirm the
        account is flat before restarting.
        """
        outcome = "position was closed" if closed else "POSITION MAY STILL BE OPEN"
        return self._trip(
            BreakerType.UNPROTECTED_POSITION,
            f"{inst_id} had no verified exchange stop ({outcome}): {detail}",
            inst_id=inst_id,
            closed=closed,
        )

    def record_order_submitted(self) -> None:
        self._order_times.append(now_utc())

    def reset_counters(self) -> None:
        """Clear soft counters after a successful reconciliation."""
        self._duplicate_attempts = 0
        self._db_failures = 0

    # --- helpers ---------------------------------------------------------

    def _trip(self, breaker: BreakerType, reason: str, **details: Any) -> BreakerTrip:
        trip = BreakerTrip(breaker=breaker, reason=reason, tripped_at=now_utc(), details=details)
        log.error("SAFETY", f"Circuit breaker {breaker.value}: {reason}")
        self.safe_mode.enter(trip)
        return trip

    def orders_allowed(self) -> tuple[bool, str]:
        """Whether order submission is currently permitted."""
        if self.safe_mode.active:
            self.safe_mode.try_exit()
        if self.safe_mode.active:
            return (False, f"SAFE_MODE active: {self.safe_mode.reason}")
        return (True, "")

    def snapshot(self) -> dict[str, Any]:
        return {
            "safe_mode": self.safe_mode.snapshot(),
            "duplicate_attempts": self._duplicate_attempts,
            "database_failures": self._db_failures,
            "orders_last_minute": len(
                [t for t in self._order_times if t >= now_utc() - timedelta(minutes=1)]
            ),
        }
