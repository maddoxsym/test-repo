"""
Position sizing from TRUE monetary risk using the live cTrader symbol
specification (tick size, tick value, volume min/max/step in UNITS).

Rules enforced here:
  * volume is ALWAYS rounded DOWN to the broker volume step;
  * if the minimum broker volume already risks more than allowed, the
    trade is REJECTED (never forced);
  * risk never exceeds cfg.max_risk_per_trade (0.25% hard ceiling);
  * the cost model includes spread + slippage buffer + optional commission
    so the sized risk is the worst-case loss, not the optimistic one;
  * a degenerate/unverifiable symbol spec rejects the trade outright.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.config import Config
from ..core.models import CTraderSymbolSpec


@dataclass
class SizingResult:
    volume_units: float
    risk_money: float                 # actual worst-case risk at this volume
    intended_risk_money: float
    risk_fraction_actual: float
    stop_points: float
    cost_estimate: float
    rejected: bool = False
    reason: str = ""


class PositionSizer:

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def size(self, spec: CTraderSymbolSpec, equity: float,
             risk_fraction: float, entry: float, stop: float) -> SizingResult:
        cfg = self.cfg
        risk_fraction = min(risk_fraction, cfg.max_risk_per_trade)
        ok, why = spec.valid()
        if not ok:
            return SizingResult(0, 0, 0, 0, 0, 0, True,
                                f"symbol spec unverifiable: {why}")
        stop_dist = abs(entry - stop)
        if stop_dist <= 0 or equity <= 0:
            return SizingResult(0, 0, 0, 0, 0, 0, True, "invalid stop/equity")
        money_per_unit = spec.money_per_price_unit_per_unit()
        if money_per_unit <= 0:
            return SizingResult(0, 0, 0, 0, 0, 0, True,
                                "symbol spec has no tick value/size")
        intended = equity * risk_fraction
        # worst-case loss per UNIT: stop distance + slippage + spread + fees
        slip = cfg.slippage_buffer_points * spec.point
        spread_cost = spec.spread_points * spec.point
        loss_per_unit = (stop_dist + slip + spread_cost) * money_per_unit \
            + cfg.commission_per_unit
        if loss_per_unit <= 0:
            return SizingResult(0, 0, 0, 0, 0, 0, True, "degenerate cost model")
        raw_volume = intended / loss_per_unit
        volume = spec.round_volume_down(raw_volume)
        if volume <= 0:
            min_risk = spec.volume_min * loss_per_unit
            return SizingResult(
                0, 0, intended, 0, stop_dist / spec.point,
                cfg.commission_per_unit * spec.volume_min, True,
                f"minimum volume {spec.volume_min} units would risk "
                f"{min_risk:.2f} ({min_risk / equity:.2%}) > intended "
                f"{intended:.2f} ({risk_fraction:.2%}) — trade rejected, "
                f"never forced")
        actual = volume * loss_per_unit
        # rounding down can never exceed intended risk; double-check anyway
        if actual > intended * 1.0001:
            return SizingResult(0, 0, intended, 0, stop_dist / spec.point,
                                0, True, "sizing exceeded intended risk")
        return SizingResult(
            volume_units=volume, risk_money=actual,
            intended_risk_money=intended,
            risk_fraction_actual=actual / equity,
            stop_points=stop_dist / spec.point,
            cost_estimate=cfg.commission_per_unit * volume
            + (slip + spread_cost) * money_per_unit * volume)

    def risk_fraction_for(self, grade_value: str, score: float) -> float:
        """Risk by setup grade — but ALWAYS capped at max_risk_per_trade
        (0.25%). Better grades use the full allowance; B setups use less.
        There is no path above the cap: no martingale, no doubling after
        losses, no recovery mode."""
        cap = self.cfg.max_risk_per_trade
        if grade_value == "A+":
            return cap
        if grade_value == "A":
            return cap * 0.85
        if grade_value == "B":
            return cap * 0.7
        return 0.0
