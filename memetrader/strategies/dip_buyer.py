"""Survivor Dip Buyer.

Thesis: a memecoin that is several days old, still holds a real mcap, and
keeps adding holders has survived the window in which ~98% of tokens die.
These survivors regularly retrace 30-60% between legs; buying that dip is
the lowest-variance trade in the book (smaller multiples, far higher hit
rate). This is the only strategy exempt from the max-age filter — by
design it trades the OTHER side of the lifecycle.
"""
from __future__ import annotations

from ..models import Signal, TokenSnapshot
from .base import Context, Strategy


class DipBuyer(Strategy):
    name = "dip_buyer"

    def evaluate(self, t: TokenSnapshot, ctx: Context) -> Signal | None:
        p = self.params
        if t.age_minutes < p.dip_min_age_minutes:
            return None
        if t.market_cap < p.dip_min_mcap:
            return None
        if not ctx.safety.passed:
            return None
        dd = t.drawdown_from_high
        if not (p.dip_drawdown_low <= dd <= p.dip_drawdown_high):
            return None
        if t.holders < p.mom_min_holders:
            return None

        conf = 0.40 + 0.20 * ((dd - p.dip_drawdown_low) /
                              max(1e-9, p.dip_drawdown_high - p.dip_drawdown_low))
        conf += 0.10 * ctx.smart_money + ctx.narrative_delta
        return self._signal(
            t, conf,
            f"survivor dip -{dd:.0%} from high, age {t.age_minutes/60:.0f}h, "
            f"mcap ${t.market_cap:,.0f}",
        )
