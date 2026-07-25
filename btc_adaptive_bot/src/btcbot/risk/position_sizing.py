"""Position sizing — automatic, evidence-aware, and always bounded.

Trades are **not** all the same size. Size responds to equity, stop distance,
volatility, strategy confidence, measured expectancy, current drawdown, regime,
and liquidity. But every one of those inputs is clamped, and the final result
passes a hard risk ceiling before an order can exist.

The pipeline is exactly the one the brief specifies::

    POSITION SIZE → EXCHANGE ROUNDING → MIN/MAX VALIDATION
                  → NOTIONAL VALIDATION → FINAL RISK CHECK → ORDER

Any failure returns a :class:`SizingResult` with ``approved=False`` and a
reason. There is no path that returns an unvalidated quantity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..config.schema import RiskConfig
from ..exchange.models import InstrumentSpec
from ..strategies.base import Direction
from ..utils.errors import PositionSizingError
from ..utils.logging import get_logger
from ..utils.numeric import clamp, format_qty, is_finite_positive, safe_div, to_decimal

log = get_logger(__name__)


@dataclass(slots=True)
class SizingInputs:
    """Everything the sizer is allowed to consider."""

    equity: float
    available_balance: float
    entry_price: float
    stop_price: float
    direction: Direction
    confidence: float = 0.5
    atr: float | None = None
    expectancy_r: float = 0.0
    observations: int = 0
    drawdown_pct: float = 0.0
    regime: str = "UNCERTAIN"
    regime_confidence: float = 0.5
    spread_bps: float = 0.0
    news_size_factor: float = 1.0
    volatility_pct: float | None = None


@dataclass(slots=True)
class SizingResult:
    """The sizing decision, with a full audit trail."""

    approved: bool
    quantity: Decimal = Decimal(0)
    quantity_str: str = "0"
    notional: float = 0.0
    risk_amount: float = 0.0
    risk_pct: float = 0.0
    risk_pct_of_equity: float = 0.0
    stop_distance: float = 0.0
    reason: str = ""
    reasoning: list[str] = field(default_factory=list)
    adjustments: dict[str, float] = field(default_factory=dict)

    def explain(self) -> str:
        return " | ".join(self.reasoning) if self.reasoning else self.reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "quantity": float(self.quantity),
            "quantity_str": self.quantity_str,
            "notional": self.notional,
            "risk_amount": self.risk_amount,
            "risk_pct_of_equity": self.risk_pct_of_equity,
            "stop_distance": self.stop_distance,
            "reason": self.reason,
            "reasoning": self.reasoning,
            "adjustments": self.adjustments,
        }


def _rejected(reason: str) -> SizingResult:
    return SizingResult(approved=False, reason=reason, reasoning=[f"REJECTED: {reason}"])


class PositionSizer:
    """Computes a bounded, exchange-legal position size."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def calculate(self, inputs: SizingInputs, instrument: InstrumentSpec) -> SizingResult:
        """Run the full pipeline. Never raises on bad input — it rejects."""
        reasoning: list[str] = []
        adjustments: dict[str, float] = {}

        # --- 0. sanity on the inputs themselves -------------------------
        if not is_finite_positive(inputs.equity):
            return _rejected(f"invalid equity {inputs.equity!r}")
        if not is_finite_positive(inputs.entry_price):
            return _rejected(f"invalid entry price {inputs.entry_price!r}")
        if not is_finite_positive(inputs.stop_price):
            return _rejected(f"invalid stop price {inputs.stop_price!r}")
        if inputs.available_balance < 0 or not math.isfinite(inputs.available_balance):
            return _rejected(f"invalid available balance {inputs.available_balance!r}")

        # Stop on the wrong side means the strategy is broken; never size it.
        if inputs.direction is Direction.LONG and inputs.stop_price >= inputs.entry_price:
            return _rejected("long stop is at or above entry")
        if inputs.direction is Direction.SHORT and inputs.stop_price <= inputs.entry_price:
            return _rejected("short stop is at or below entry")

        stop_distance = abs(inputs.entry_price - inputs.stop_price)
        if stop_distance <= 0:
            return _rejected("stop distance is zero — refusing to divide by zero")

        stop_pct = safe_div(stop_distance, inputs.entry_price)
        # A microscopic stop would otherwise produce an enormous position.
        if stop_pct < self.config.min_stop_distance_pct:
            return _rejected(
                f"stop distance {stop_pct * 100:.4f}% is below the minimum "
                f"{self.config.min_stop_distance_pct * 100:.4f}% (would inflate position size)"
            )
        if stop_pct > self.config.max_stop_distance_pct:
            return _rejected(
                f"stop distance {stop_pct * 100:.2f}% exceeds the maximum "
                f"{self.config.max_stop_distance_pct * 100:.2f}%"
            )
        if inputs.atr and inputs.atr > 0:
            min_atr_distance = inputs.atr * self.config.min_stop_distance_atr_mult
            if stop_distance < min_atr_distance:
                return _rejected(
                    f"stop distance {stop_distance:.2f} is below {self.config.min_stop_distance_atr_mult}× "
                    f"ATR ({min_atr_distance:.2f}) — too tight for current volatility"
                )

        reasoning.append(f"stop distance {stop_distance:,.2f} ({stop_pct * 100:.3f}%)")

        # --- 1. base risk fraction --------------------------------------
        risk_pct = self.config.normal_risk_pct
        reasoning.append(f"base risk {risk_pct * 100:.2f}% of equity")

        # Confidence: scales within a configured band, never beyond it.
        confidence = clamp(inputs.confidence, 0.0, 1.0)
        confidence_factor = clamp(
            self.config.confidence_size_floor
            + (self.config.confidence_size_ceiling - self.config.confidence_size_floor) * confidence,
            self.config.confidence_size_floor,
            self.config.confidence_size_ceiling,
        )
        risk_pct *= confidence_factor
        adjustments["confidence"] = round(confidence_factor, 4)
        reasoning.append(f"confidence {confidence:.2f} → ×{confidence_factor:.2f}")

        # Expectancy: only trusted once there is a real sample behind it.
        if inputs.observations >= 20:
            expectancy_factor = clamp(1.0 + inputs.expectancy_r * 0.25, 0.7, 1.3)
            risk_pct *= expectancy_factor
            adjustments["expectancy"] = round(expectancy_factor, 4)
            reasoning.append(
                f"expectancy {inputs.expectancy_r:+.2f}R over {inputs.observations} "
                f"observations → ×{expectancy_factor:.2f}"
            )

        # Drawdown de-risking.
        if inputs.drawdown_pct >= self.config.drawdown_derisk_threshold_pct:
            risk_pct *= self.config.drawdown_derisk_factor
            adjustments["drawdown"] = self.config.drawdown_derisk_factor
            reasoning.append(
                f"drawdown {inputs.drawdown_pct * 100:.1f}% ≥ threshold → "
                f"×{self.config.drawdown_derisk_factor:.2f}"
            )

        # Volatility: unusually wide conditions get smaller positions even
        # though the stop already widened, because slippage risk grows too.
        if inputs.volatility_pct and inputs.volatility_pct > 0:
            vol_factor = clamp(safe_div(0.015, inputs.volatility_pct, 1.0), 0.6, 1.2)
            risk_pct *= vol_factor
            adjustments["volatility"] = round(vol_factor, 4)
            reasoning.append(f"volatility {inputs.volatility_pct * 100:.2f}% → ×{vol_factor:.2f}")

        # Regime confidence: an UNCERTAIN read means smaller bets.
        regime_factor = clamp(0.8 + 0.2 * clamp(inputs.regime_confidence, 0.0, 1.0), 0.8, 1.0)
        risk_pct *= regime_factor
        adjustments["regime"] = round(regime_factor, 4)

        # Liquidity: a wide spread makes the true cost higher than modelled.
        if inputs.spread_bps > 5.0:
            spread_factor = clamp(1.0 - (inputs.spread_bps - 5.0) * 0.02, 0.5, 1.0)
            risk_pct *= spread_factor
            adjustments["spread"] = round(spread_factor, 4)
            reasoning.append(f"spread {inputs.spread_bps:.1f}bps → ×{spread_factor:.2f}")

        # News risk.
        news_factor = clamp(inputs.news_size_factor, 0.1, 1.0)
        if news_factor < 1.0:
            risk_pct *= news_factor
            adjustments["news"] = round(news_factor, 4)
            reasoning.append(f"news risk → ×{news_factor:.2f}")

        # --- 2. HARD BOUNDS ---------------------------------------------
        # This clamp is the single most important line in the module: whatever
        # the multipliers did, risk cannot exceed the configured maximum.
        bounded_risk_pct = clamp(risk_pct, self.config.min_risk_pct, self.config.max_risk_pct)
        if abs(bounded_risk_pct - risk_pct) > 1e-9:
            reasoning.append(
                f"risk clamped {risk_pct * 100:.3f}% → {bounded_risk_pct * 100:.3f}% "
                f"(bounds {self.config.min_risk_pct * 100:.2f}%–{self.config.max_risk_pct * 100:.2f}%)"
            )
        risk_pct = bounded_risk_pct

        risk_amount = inputs.equity * risk_pct
        if not is_finite_positive(risk_amount):
            return _rejected(f"computed risk amount is invalid ({risk_amount!r})")

        raw_quantity = risk_amount / stop_distance
        if not is_finite_positive(raw_quantity):
            return _rejected(f"computed quantity is invalid ({raw_quantity!r})")
        reasoning.append(
            f"risk ${risk_amount:,.2f} ÷ stop {stop_distance:,.2f} = {raw_quantity:.8f} units"
        )

        # --- 3. notional caps (before rounding) --------------------------
        notional_cap = min(
            inputs.equity * self.config.max_notional_pct_equity,
            max(0.0, inputs.available_balance),
        )
        if notional_cap <= 0:
            return _rejected("no available balance to trade with")

        raw_notional = raw_quantity * inputs.entry_price
        if raw_notional > notional_cap:
            raw_quantity = notional_cap / inputs.entry_price
            reasoning.append(
                f"notional capped at ${notional_cap:,.2f} → {raw_quantity:.8f} units"
            )

        # --- 4. EXCHANGE ROUNDING ---------------------------------------
        try:
            quantity = instrument.round_qty(raw_quantity)
        except (ValueError, ArithmeticError) as exc:
            return _rejected(f"quantity rounding failed: {exc}")
        reasoning.append(f"rounded to step {instrument.qty_step} → {quantity}")

        # --- 5. MIN/MAX VALIDATION --------------------------------------
        valid, message = instrument.qty_within_bounds(quantity)
        if not valid:
            return _rejected(message)

        if instrument.max_market_order_qty is not None and quantity > instrument.max_market_order_qty:
            quantity = instrument.round_qty(instrument.max_market_order_qty)
            reasoning.append(f"reduced to exchange market-order maximum {quantity}")

        # --- 6. NOTIONAL VALIDATION -------------------------------------
        notional = quantity * to_decimal(inputs.entry_price)
        valid, message = instrument.notional_within_bounds(notional)
        if not valid:
            # If we are under the exchange minimum, try rounding up to it — but
            # only if that still respects the risk ceiling (checked in step 7).
            if instrument.min_order_amt is not None and notional < instrument.min_order_amt:
                required_qty = instrument.min_order_amt / to_decimal(inputs.entry_price)
                from ..utils.numeric import round_step_up

                bumped = round_step_up(required_qty, instrument.qty_step)
                bumped_notional = bumped * to_decimal(inputs.entry_price)
                bumped_risk = float(bumped) * stop_distance
                if bumped_risk <= inputs.equity * self.config.max_risk_pct and float(
                    bumped_notional
                ) <= notional_cap:
                    quantity = bumped
                    notional = bumped_notional
                    reasoning.append(
                        f"raised to exchange minimum notional {instrument.min_order_amt} "
                        f"→ {quantity} units (risk ${bumped_risk:,.2f})"
                    )
                else:
                    return _rejected(
                        f"position below the exchange minimum notional "
                        f"({notional} < {instrument.min_order_amt}) and raising it would breach "
                        f"the {self.config.max_risk_pct * 100:.1f}% risk cap"
                    )
            else:
                return _rejected(message)

        # --- 7. FINAL RISK CHECK ----------------------------------------
        final_risk_amount = float(quantity) * stop_distance
        final_risk_pct = safe_div(final_risk_amount, inputs.equity)
        if final_risk_pct > self.config.max_risk_pct + 1e-9:
            return _rejected(
                f"final risk {final_risk_pct * 100:.3f}% exceeds the hard cap "
                f"{self.config.max_risk_pct * 100:.2f}% after rounding"
            )
        if float(notional) > notional_cap + 1e-6:
            return _rejected(
                f"final notional ${float(notional):,.2f} exceeds the cap ${notional_cap:,.2f}"
            )
        if quantity <= 0:
            return _rejected("final quantity is not positive")

        reasoning.append(
            f"final: {quantity} units, notional ${float(notional):,.2f}, "
            f"risk ${final_risk_amount:,.2f} ({final_risk_pct * 100:.3f}% of equity)"
        )

        return SizingResult(
            approved=True,
            quantity=quantity,
            quantity_str=format_qty(quantity, instrument.qty_step),
            notional=float(notional),
            risk_amount=final_risk_amount,
            risk_pct=risk_pct,
            risk_pct_of_equity=final_risk_pct,
            stop_distance=stop_distance,
            reason="approved",
            reasoning=reasoning,
            adjustments=adjustments,
        )

    def validate_or_raise(self, result: SizingResult) -> SizingResult:
        """Convert a rejection into an exception where a caller needs one."""
        if not result.approved:
            raise PositionSizingError(result.reason)
        return result
