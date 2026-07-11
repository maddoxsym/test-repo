"""
RiskManager -- decides how big a trade may be and when the bot must stand
down for the day.  Its answer is final; no strategy can override it.

Sizing model (USD-quoted instruments -- both EUR_USD and XAU_USD):
    1 unit gains/loses 1 USD per 1.0 move in price, so
    units = risk_usd / stop_distance, capped by notional leverage and
    rounded down to the instrument's trade precision.

NOTE: this assumes a USD-denominated account.  If your OANDA account is in
another currency the risk percentages remain approximately right but not
exact; convert or adjust risk_per_trade_pct accordingly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from .config import RiskConfig
from .journal import Journal
from .oanda import InstrumentSpec

log = logging.getLogger("risk")


@dataclass
class SizedTrade:
    units: float                 # signed: + long, - short
    stop_price: float
    tp_price: float
    risk_usd: float
    notional_usd: float


class RiskManager:
    def __init__(self, cfg: RiskConfig, journal: Journal):
        self.cfg = cfg
        self.journal = journal

    # ------------------------------------------------------- daily gates

    def daily_gate(self, balance: float, now: Optional[float] = None,
                   open_trade_count: int = 0) -> Optional[str]:
        """Returns a reason to stand down, or None if trading is allowed."""
        if open_trade_count >= self.cfg.max_open_trades:
            return (f"max open trades reached "
                    f"({open_trade_count}/{self.cfg.max_open_trades})")
        if self.journal.trades_opened_today(now) >= self.cfg.max_trades_per_day:
            return f"daily trade cap reached ({self.cfg.max_trades_per_day})"
        pnl = self.journal.daily_pnl(now)
        if balance > 0:
            pct = pnl / balance * 100
            if pct <= -self.cfg.daily_loss_limit_pct:
                return (f"daily loss limit hit ({pct:.1f}% <= "
                        f"-{self.cfg.daily_loss_limit_pct}%) -- done for today")
            if pct >= self.cfg.daily_profit_target_pct:
                return (f"daily profit target reached ({pct:.1f}%) -- "
                        f"banking the day")
        return None

    # ------------------------------------------------------------ sizing

    def size(self, direction: int, price: float, atr_value: float,
             balance: float, spec: InstrumentSpec) -> Optional[SizedTrade]:
        if atr_value <= 0 or price <= 0 or balance <= 0:
            return None
        stop_dist = atr_value * self.cfg.stop_atr_mult
        risk_usd = balance * self.cfg.risk_per_trade_pct / 100.0

        raw_units = risk_usd / stop_dist

        # cap notional exposure
        max_notional = balance * self.cfg.max_notional_leverage
        if raw_units * price > max_notional:
            raw_units = max_notional / price
            risk_usd = raw_units * stop_dist
            log.info("units capped by notional leverage limit")

        # round DOWN to the instrument's unit precision
        quant = 10 ** spec.trade_units_precision
        units = math.floor(raw_units * quant) / quant
        if units < spec.minimum_trade_size:
            log.info("sized units %.4f below minimum trade size %s -- skipping",
                     units, spec.minimum_trade_size)
            return None
        risk_usd = units * stop_dist

        if direction > 0:
            stop_price = price - stop_dist
            tp_price = price + stop_dist * self.cfg.take_profit_r
        else:
            stop_price = price + stop_dist
            tp_price = price - stop_dist * self.cfg.take_profit_r

        return SizedTrade(
            units=units * direction,
            stop_price=stop_price,
            tp_price=tp_price,
            risk_usd=risk_usd,
            notional_usd=units * price,
        )

    # -------------------------------------------------- trade management

    def breakeven_stop(self, direction: int, entry: float, stop: float,
                       current_price: float) -> Optional[float]:
        """Once a trade is up `breakeven_at_r` R, protect it at entry.
        Returns the new stop price, or None if no change is needed."""
        risk_dist = abs(entry - stop)
        if risk_dist <= 0:
            return None
        progress = (current_price - entry) * direction / risk_dist
        if progress < self.cfg.breakeven_at_r:
            return None
        buffer = risk_dist * 0.05
        new_stop = entry + buffer * direction
        # only ever tighten the stop
        if direction > 0 and new_stop > stop:
            return new_stop
        if direction < 0 and new_stop < stop:
            return new_stop
        return None
