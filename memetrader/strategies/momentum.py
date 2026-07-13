"""Momentum Breakout.

Thesis: memecoin runs are reflexive — volume attracts eyes, eyes attract
volume. A 5-minute volume spike several multiples above the hourly
baseline, on a token whose holder base is actually growing, catches
runners in the first leg while mcap is still low. Chart shape matters
less than flow.

Entry: 5m volume >= mom_vol_spike x (1h volume / 12), holders above
threshold, safety pass, still inside the low-cap window.
"""
from __future__ import annotations

from ..models import Signal, TokenSnapshot
from .base import Context, Strategy


class MomentumBreakout(Strategy):
    name = "momentum"

    def evaluate(self, t: TokenSnapshot, ctx: Context) -> Signal | None:
        p = self.params
        if not ctx.safety.passed:
            return None
        if t.market_cap < p.min_mcap or t.market_cap > p.max_mcap:
            return None
        if t.holders < p.mom_min_holders:
            return None
        if t.volume_5m < p.mom_min_volume_5m:
            return None
        baseline_5m = t.volume_1h / 12.0 if t.volume_1h > 0 else 0.0
        if baseline_5m <= 0 or t.volume_5m < p.mom_vol_spike * baseline_5m:
            return None
        # don't buy a spike that is actually a dump
        if t.drawdown_from_high > 0.25:
            return None

        spike_ratio = t.volume_5m / baseline_5m
        conf = 0.35 + min(0.25, 0.05 * (spike_ratio - p.mom_vol_spike))
        conf += 0.15 * ctx.smart_money + ctx.narrative_delta
        return self._signal(
            t, conf,
            f"vol spike {spike_ratio:.1f}x baseline, {t.holders} holders, "
            f"mcap ${t.market_cap:,.0f}",
        )
