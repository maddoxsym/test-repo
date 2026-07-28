"""Position sizing — automatic, evidence-aware, and always bounded.

Trades are **not** all the same size. Size responds to equity, stop distance,
volatility, strategy confidence, measured expectancy, current drawdown, regime,
and liquidity. But every one of those inputs is clamped, and the final result
passes a hard risk ceiling before an order can exist.

Perpetual-swap specifics: risk is computed in **base currency** (BTC) — stop
distance × base quantity is the dollar risk regardless of leverage — and the
result is then converted to **contracts** through the discovered instrument
spec (``ctVal``/``ctMult``/``lotSz``/``minSz``). Leverage does not change the
risk of a stop-out; it changes the *margin* the position locks up, which is
validated against the available balance here.

The pipeline is exactly the one the brief specifies::

    POSITION SIZE → CONTRACT CONVERSION → EXCHANGE ROUNDING
                  → MIN/MAX VALIDATION → NOTIONAL + MARGIN VALIDATION
                  → FINAL RISK CHECK → ORDER

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
    leverage: float = 1.0
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
    """The sizing decision, with a full audit trail.

    ``quantity`` is the **base-currency** quantity (drives risk and PnL
    accounting); ``contracts`` / ``quantity_str`` are what the exchange order
    actually carries.
    """

    approved: bool
    quantity: Decimal = Decimal(0)           # base currency (BTC)
    contracts: Decimal = Decimal(0)          # exchange order size
    quantity_str: str = "0"                  # formatted contract size for the order
    notional: float = 0.0
    required_margin: float = 0.0
    leverage: float = 1.0
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
            "contracts": float(self.contracts),
            "quantity_str": self.quantity_str,
            "notional": self.notional,
            "required_margin": self.required_margin,
            "leverage": self.leverage,
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
        if not is_finite_positive(inputs.leverage) or inputs.leverage > 10.0 + 1e-9:
            return _rejected(f"invalid leverage {inputs.leverage!r} (must be in (0, 10])")

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

        raw_base_qty = risk_amount / stop_distance
        if not is_finite_positive(raw_base_qty):
            return _rejected(f"computed quantity is invalid ({raw_base_qty!r})")
        reasoning.append(
            f"risk ${risk_amount:,.2f} ÷ stop {stop_distance:,.2f} = {raw_base_qty:.8f} "
            f"{instrument.base_ccy or 'base'}"
        )

        # --- 3. notional caps (leverage-aware, before rounding) ----------
        # Margin is what the balance actually constrains on a perp: the
        # notional a position may reach is available margin × leverage, and it
        # is additionally capped as a fraction of equity.
        leverage = max(1.0, inputs.leverage)
        margin_cap = max(0.0, inputs.available_balance) * (
            self.config.leverage.margin_utilization_cap
        )
        notional_cap = min(
            inputs.equity * self.config.max_notional_pct_equity * leverage,
            margin_cap * leverage,
        )
        if notional_cap <= 0:
            return _rejected("no available margin to trade with")

        raw_notional = raw_base_qty * inputs.entry_price
        if raw_notional > notional_cap:
            raw_base_qty = notional_cap / inputs.entry_price
            reasoning.append(
                f"notional capped at ${notional_cap:,.2f} "
                f"(margin cap ${margin_cap:,.2f} × {leverage:.1f}x) → {raw_base_qty:.8f}"
            )

        # --- 4. CONTRACT CONVERSION + EXCHANGE ROUNDING -----------------
        try:
            if instrument.is_derivative:
                contracts = instrument.contracts_from_base(raw_base_qty)
                base_qty = instrument.base_from_contracts(contracts)
            else:
                contracts = instrument.round_qty(raw_base_qty)
                base_qty = contracts
        except (ValueError, ArithmeticError) as exc:
            return _rejected(f"contract conversion failed: {exc}")
        if instrument.is_derivative:
            reasoning.append(
                f"{raw_base_qty:.8f} {instrument.base_ccy} ÷ "
                f"(ctVal {instrument.ct_val} × mult {instrument.ct_mult}) → "
                f"{contracts} contract(s) (lot {instrument.lot_size})"
            )
        else:
            reasoning.append(f"rounded to step {instrument.lot_size} → {contracts}")

        # --- 5. MIN/MAX VALIDATION --------------------------------------
        valid, message = instrument.qty_within_bounds(contracts)
        if not valid:
            return _rejected(message)

        if instrument.max_mkt_size is not None and contracts > instrument.max_mkt_size:
            contracts = instrument.round_qty(instrument.max_mkt_size)
            base_qty = (
                instrument.base_from_contracts(contracts)
                if instrument.is_derivative
                else contracts
            )
            reasoning.append(f"reduced to exchange market-order maximum {contracts}")

        # --- 6. NOTIONAL + MARGIN VALIDATION ----------------------------
        notional = base_qty * to_decimal(inputs.entry_price)
        required_margin = safe_div(float(notional), leverage)
        if required_margin > margin_cap + 1e-6:
            return _rejected(
                f"required margin ${required_margin:,.2f} at {leverage:.1f}x exceeds the "
                f"margin budget ${margin_cap:,.2f} "
                f"({self.config.leverage.margin_utilization_cap * 100:.0f}% of available)"
            )

        # --- 7. FINAL RISK CHECK ----------------------------------------
        final_risk_amount = float(base_qty) * stop_distance
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
        if contracts <= 0 or base_qty <= 0:
            return _rejected("final quantity is not positive")

        reasoning.append(
            f"final: {contracts} contract(s) = {base_qty} {instrument.base_ccy or 'base'}, "
            f"notional ${float(notional):,.2f}, margin ${required_margin:,.2f} at {leverage:.1f}x, "
            f"risk ${final_risk_amount:,.2f} ({final_risk_pct * 100:.3f}% of equity)"
        )

        return SizingResult(
            approved=True,
            quantity=base_qty,
            contracts=contracts,
            quantity_str=format_qty(contracts, instrument.lot_size),
            notional=float(notional),
            required_margin=required_margin,
            leverage=leverage,
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
