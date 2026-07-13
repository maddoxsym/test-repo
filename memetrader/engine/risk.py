"""Risk engine — owns every exit and every position size.

Strategies find entries; this module decides how much to bet and when to
get out. The rules encode the only reliably profitable memecoin behavior:
cut rugs instantly, take profit into strength on a ladder, let a small
moonbag ride with a trailing stop, and never let one ticket sink the book.

Exit priority (checked in order every tick):
  1. rug signal      -> market-dump everything immediately
  2. hard stop-loss  -> dump remaining
  3. take-profit ladder rungs
  4. trailing stop (armed after the first rung fills)
  5. time stop for positions going nowhere
"""
from __future__ import annotations

import time

from ..config import RiskParams
from ..models import Position, Signal, TokenSnapshot, TradeRecord
from .paper_broker import PaperBroker
from .portfolio import Portfolio


class RiskEngine:
    def __init__(self, risk: RiskParams, broker: PaperBroker):
        self.risk = risk
        self.broker = broker

    # ------------------------------ entries ---------------------------
    def size_and_open(self, sig: Signal, t: TokenSnapshot, pf: Portfolio,
                      now: float) -> Position | None:
        r = self.risk
        if sig.address in pf.positions:
            return None
        if len(pf.positions) >= r.max_concurrent_positions:
            return None
        same_strat = sum(1 for p in pf.positions.values() if p.strategy == sig.strategy)
        if same_strat >= r.max_per_strategy:
            return None
        equity = pf.equity({})
        usd = equity * r.risk_per_trade * (0.5 + sig.confidence)
        usd = min(max(usd, r.min_position_usd), r.max_position_usd, pf.cash * 0.5)
        if usd < r.min_position_usd or usd > pf.cash:
            return None
        tokens, spent = self.broker.buy(t, usd)
        if tokens <= 0:
            return None
        pf.cash -= spent
        pos = Position(
            address=t.address, symbol=t.symbol, strategy=sig.strategy,
            entry_price=t.price, entry_mcap=t.market_cap, size_usd=spent,
            tokens=tokens, opened_at=now, high_water_price=t.price,
        )
        pf.positions[t.address] = pos
        return pos

    # ------------------------------- exits ----------------------------
    def manage(self, pos: Position, t: TokenSnapshot, pf: Portfolio,
               now: float, rug_alert: bool) -> TradeRecord | None:
        r = self.risk
        pos.high_water_price = max(pos.high_water_price, t.price)
        mult = pos.unrealized_multiple(t.price)
        hold_min = (now - pos.opened_at) / 60.0

        # 1. rug alert from the Rug Checker: get out at any price
        if rug_alert or (t.liquidity < 300.0):
            return self._close_all(pos, t, pf, now, "rug_exit")

        # 2. hard stop
        if mult <= (1.0 - r.stop_loss):
            return self._close_all(pos, t, pf, now, "stop_loss")

        # 3. spike exit — memecoin verticals retrace; sell INTO the spike,
        #    the second it happens, while someone is still paying up
        if (pos.last_price > 0
                and t.price >= pos.last_price * (1.0 + r.spike_sell_threshold)
                and mult >= r.spike_min_multiple):
            sell_tokens = pos.tokens * r.spike_sell_fraction
            proceeds = self.broker.sell(t, sell_tokens)
            pf.cash += proceeds
            pos.realized_usd += proceeds
            pos.tokens -= sell_tokens
            pos.remaining_fraction *= (1.0 - r.spike_sell_fraction)
            if pos.tokens <= 0 or pos.remaining_fraction <= 0.02:
                pos.last_price = t.price
                return self._close_all(pos, t, pf, now, "spike_exit")

        # 4. take-profit ladder
        while (pos.tp_levels_hit < len(r.tp_multiples)
               and mult >= r.tp_multiples[pos.tp_levels_hit]):
            frac = r.tp_fractions[pos.tp_levels_hit]
            sell_tokens = pos.tokens * (frac / pos.remaining_fraction)
            sell_tokens = min(sell_tokens, pos.tokens)
            proceeds = self.broker.sell(t, sell_tokens)
            pf.cash += proceeds
            pos.realized_usd += proceeds
            pos.tokens -= sell_tokens
            pos.remaining_fraction = max(0.0, pos.remaining_fraction - frac)
            pos.tp_levels_hit += 1
            if pos.tokens <= 0 or pos.remaining_fraction <= 0.001:
                return self._finalize(pos, t, pf, now, "tp_ladder_complete")

        # 5. trailing stop on the moonbag (armed after first TP or spike sell)
        if ((pos.tp_levels_hit > 0 or pos.remaining_fraction < 0.999)
                and pos.high_water_price > 0):
            if t.price <= pos.high_water_price * (1.0 - r.trailing_stop):
                return self._close_all(pos, t, pf, now, "trailing_stop")

        # 6. time stop for dead money
        if hold_min >= r.max_hold_minutes and mult < r.stale_exit_multiple:
            return self._close_all(pos, t, pf, now, "time_stop")

        pos.last_price = t.price
        return None

    # ------------------------------------------------------------------
    def _close_all(self, pos: Position, t: TokenSnapshot, pf: Portfolio,
                   now: float, reason: str) -> TradeRecord:
        proceeds = self.broker.sell(t, pos.tokens)
        pf.cash += proceeds
        pos.realized_usd += proceeds
        pos.tokens = 0.0
        return self._finalize(pos, t, pf, now, reason)

    def _finalize(self, pos: Position, t: TokenSnapshot, pf: Portfolio,
                  now: float, reason: str) -> TradeRecord:
        pf.positions.pop(pos.address, None)
        # exact accounting: every sell credited pos.realized_usd, so per-trade
        # pnl is measured cash out minus cash in — no estimation
        pnl = pos.realized_usd - pos.size_usd
        rec = TradeRecord(
            address=pos.address, symbol=pos.symbol, strategy=pos.strategy,
            entry_price=pos.entry_price, exit_price=t.price,
            entry_mcap=pos.entry_mcap, size_usd=pos.size_usd,
            pnl_usd=pnl,
            multiple=pos.realized_usd / pos.size_usd if pos.size_usd else 0.0,
            hold_minutes=(now - pos.opened_at) / 60.0, exit_reason=reason,
            opened_at=pos.opened_at, closed_at=now,
        )
        pf.trades.append(rec)
        return rec
