"""Gold orchestrator — one instrument, leveraged margin engine (MT5-style).

Keeps the rolling minute-price history, asks each strategy for a signal,
and hands entries/exits to the leverage engine. One position at a time;
the day guard wraps the whole session. allow_short/max_leverage come
from GoldParams.
"""
from __future__ import annotations

from collections import deque

from memetrader.engine.portfolio import Portfolio
from memetrader.models import TradeRecord

from .config import GoldParams
from .leverage import LevEngine
from .strategies import ALL_STRATEGIES


class GoldOrchestrator:
    def __init__(self, params: GoldParams, portfolio: Portfolio | None = None,
                 verbose: bool = False):
        self.params = params
        self.verbose = verbose
        self.portfolio = portfolio or Portfolio(params.risk.starting_bankroll_usd)
        self.engine = LevEngine(params.risk, max_leverage=params.max_leverage,
                                exit_style=getattr(params, "exit_style", "trail"))
        self.prices: deque[float] = deque(maxlen=1600)

    def on_price(self, price: float, now: float) -> list[TradeRecord]:
        self.prices.append(price)
        closed: list[TradeRecord] = []

        # exits before entries, always
        rec = self.engine.manage(price, self.portfolio, now)
        if rec:
            closed.append(rec)
            if self.verbose:
                print(f"  CLOSE [{rec.exit_reason:>13}] {rec.symbol} "
                      f"pnl ${rec.pnl_usd:+.2f} held {rec.hold_minutes:.0f}m")

        if self.engine.pos is None:
            history = list(self.prices)
            for strat in ALL_STRATEGIES:
                sig = strat(history, self.params)
                if sig is None:
                    continue
                if sig.direction < 0 and not self.params.allow_short:
                    continue
                pos = self.engine.open(sig.direction, price, self.portfolio,
                                       now, sig.strategy)
                if pos:
                    if self.verbose:
                        side = "LONG" if sig.direction > 0 else "SHORT"
                        lev = pos.notional / max(1e-9, self.portfolio.cash)
                        print(f"  OPEN  [{sig.strategy:>13}] {side} "
                              f"${pos.notional:,.0f} ({lev:.1f}x) @ {price:.2f} "
                              f"— {sig.reason}")
                    break

        self.portfolio.equity_curve.append(self.engine.equity(self.portfolio, price))
        return closed

    def equity(self, price: float | None = None) -> float:
        p = price if price is not None else (self.prices[-1] if self.prices else 0.0)
        return self.engine.equity(self.portfolio, p)

    def liquidate(self, now: float, reason: str) -> None:
        if self.engine.pos is not None and self.prices:
            self.engine.close(self.prices[-1], self.portfolio, now, reason)
