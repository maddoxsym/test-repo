"""Leveraged margin engine — MT5-style gold trading, long and short.

Sizing is RISK-based, the way professionals size forex/metals:
    risk_usd  = equity * risk_per_trade          (e.g. 1% of account)
    notional  = risk_usd / stop_distance         (stop defines the size)
    notional <= equity * max_leverage            (hard leverage cap)

The stop-loss therefore always risks ~the same fraction of the account
regardless of leverage; leverage only determines whether the desired
size is ALLOWED. Margin stop-out is modeled: if floating loss eats 80%
of cash the position is force-closed (a demo margin call).

One position at a time. No pyramiding, no martingale, ever.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass

from memetrader.engine.portfolio import Portfolio
from memetrader.models import TradeRecord

HALF_SPREAD = 0.00013     # ~$0.45/oz each way on XAUUSD, typical MT5 raw+comm


@dataclass
class LevPosition:
    direction: int            # +1 long, -1 short
    notional: float           # USD exposure
    entry_price: float        # spread-adjusted fill
    sl_price: float
    tp_price: float
    opened_at: float
    strategy: str
    extreme: float            # most favorable price seen (trailing)


class LevEngine:
    def __init__(self, risk, max_leverage: float = 10.0,
                 exit_style: str = "trail"):
        self.risk = risk
        self.max_leverage = max_leverage
        self.exit_style = exit_style   # trail | breakeven | binary
        self.pos: LevPosition | None = None

    # ------------------------------------------------------------------
    def equity(self, pf: Portfolio, price: float) -> float:
        eq = pf.cash
        if self.pos:
            eq += self._unrealized(price)
        return eq

    def _unrealized(self, price: float) -> float:
        p = self.pos
        exit_eff = price * (1 - p.direction * HALF_SPREAD)
        return p.notional * p.direction * (exit_eff / p.entry_price - 1.0)

    # ------------------------------------------------------------------
    def open(self, direction: int, price: float, pf: Portfolio, now: float,
             strategy: str) -> LevPosition | None:
        if self.pos is not None or pf.cash <= 0:
            return None
        r = self.risk
        equity = pf.cash
        risk_usd = equity * r.risk_per_trade
        stop_dist = r.stop_loss
        tp_dist = r.tp_multiples[0] - 1.0
        notional = min(risk_usd / stop_dist, equity * self.max_leverage)
        if notional < 1.0:
            return None
        entry = price * (1 + direction * HALF_SPREAD)
        self.pos = LevPosition(
            direction=direction, notional=notional, entry_price=entry,
            sl_price=entry * (1 - direction * stop_dist),
            tp_price=entry * (1 + direction * tp_dist),
            opened_at=now, strategy=strategy, extreme=entry,
        )
        return self.pos

    # ------------------------------------------------------------------
    def manage(self, price: float, pf: Portfolio, now: float) -> TradeRecord | None:
        p = self.pos
        if p is None:
            return None
        r = self.risk
        # favorable extreme for the trailing stop
        if (price - p.extreme) * p.direction > 0:
            p.extreme = price

        halfway = abs(p.extreme / p.entry_price - 1.0) >= (r.tp_multiples[0] - 1.0) / 2
        # breakeven mode: once halfway to target, the stop moves to entry —
        # losers become scratches, winners run to the FULL target
        if self.exit_style == "breakeven" and halfway:
            be = p.entry_price * (1 + p.direction * 2 * HALF_SPREAD)
            if (be - p.sl_price) * p.direction > 0:
                p.sl_price = be

        hit_sl = (price - p.sl_price) * p.direction <= 0
        hit_tp = (price - p.tp_price) * p.direction >= 0
        # trail mode: after halfway, trail the favorable extreme
        trail_hit = (self.exit_style == "trail" and halfway
                     and abs(price / p.extreme - 1.0) >= r.trailing_stop
                     and (price - p.extreme) * p.direction < 0)
        timed_out = (now - p.opened_at) / 60.0 >= r.max_hold_minutes
        margin_call = self._unrealized(price) <= -0.8 * pf.cash

        reason = ("margin_call" if margin_call else
                  "stop_loss" if hit_sl else
                  "take_profit" if hit_tp else
                  "trailing_stop" if trail_hit else
                  "time_stop" if timed_out else None)
        if reason is None:
            return None
        return self.close(price, pf, now, reason)

    def close(self, price: float, pf: Portfolio, now: float,
              reason: str) -> TradeRecord | None:
        p = self.pos
        if p is None:
            return None
        pnl = self._unrealized(price)
        pf.cash += pnl
        rec = TradeRecord(
            address="XAUUSD", symbol=("XAU-L" if p.direction > 0 else "XAU-S"),
            strategy=p.strategy, entry_price=p.entry_price, exit_price=price,
            entry_mcap=0.0, size_usd=p.notional, pnl_usd=pnl,
            multiple=1.0 + pnl / p.notional if p.notional else 1.0,
            hold_minutes=(now - p.opened_at) / 60.0, exit_reason=reason,
            opened_at=p.opened_at, closed_at=now,
        )
        pf.trades.append(rec)
        self.pos = None
        return rec
