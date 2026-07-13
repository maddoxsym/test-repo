"""Copy Trade.

Thesis: a small set of wallets is consistently early on runners. When
several independently profitable wallets buy the same fresh low-cap token
inside a 30-minute window, that convergence is information no chart shows.
We copy the ENTRY only — exits follow our own risk ladder, because smart
wallets often exit OTC or hold longer than our risk budget allows.

The WhaleTracker agent owns wallet scoring; this strategy only consumes
its signal.
"""
from __future__ import annotations

from ..models import Signal, TokenSnapshot
from .base import Context, Strategy


class CopyTrade(Strategy):
    name = "copy_trade"

    def evaluate(self, t: TokenSnapshot, ctx: Context) -> Signal | None:
        p = self.params
        if t.smart_wallet_buys_30m < p.copy_min_smart_buys:
            return None
        if not ctx.safety.passed:
            return None
        if t.market_cap > p.copy_max_mcap or t.market_cap < p.min_mcap:
            return None
        if t.age_minutes > p.max_age_minutes:
            return None

        conf = 0.40 + 0.15 * min(4, t.smart_wallet_buys_30m - p.copy_min_smart_buys)
        conf += ctx.narrative_delta
        return self._signal(
            t, conf,
            f"{t.smart_wallet_buys_30m} tracked profitable wallets bought in 30m, "
            f"mcap ${t.market_cap:,.0f}",
        )
