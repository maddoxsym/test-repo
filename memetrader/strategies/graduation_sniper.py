"""Graduation Sniper.

Thesis: the single most repeatable memecoin entry window is the pump.fun
bonding-curve completion (~$69k mcap). At that moment the token has already
proven demand (someone bought the whole curve), liquidity is guaranteed to
migrate to the DEX, and the mcap is still tiny relative to any runner's
eventual peak. ~99% of launches never get here — the curve itself is a
survival filter that costs us nothing.

Entry: bonding progress >= grad_min_bonding (or just graduated), safety
pass, real holder spread, minimum community size.
"""
from __future__ import annotations

from ..models import Signal, TokenSnapshot
from .base import Context, Strategy


class GraduationSniper(Strategy):
    name = "graduation_sniper"

    def evaluate(self, t: TokenSnapshot, ctx: Context) -> Signal | None:
        p = self.params
        # near-graduation entries additionally require curve ACCELERATION —
        # a curve at 90% that stopped moving is a curve that dies at 90%
        near_grad = (t.bonding_progress >= p.grad_min_bonding
                     and not t.graduated
                     and t.bonding_velocity >= p.grad_min_velocity)
        fresh_grad = t.graduated and t.age_minutes <= p.max_age_minutes
        if not (near_grad or fresh_grad):
            return None
        if not ctx.safety.passed:
            return None
        if t.holders < p.grad_min_holders:
            return None
        if t.top10_holder_pct > p.grad_max_top10_pct:
            return None
        if t.market_cap > p.max_mcap:
            return None  # too late — the cheap entry is gone

        conf = 0.45
        conf += 0.15 * (ctx.safety.score - p.min_safety_score) / max(1, 100 - p.min_safety_score)
        conf += 0.20 * ctx.smart_money
        conf += ctx.narrative_delta
        return self._signal(
            t, conf,
            f"bonding {t.bonding_progress:.0%}, {t.holders} holders, "
            f"safety {ctx.safety.score}, mcap ${t.market_cap:,.0f}",
        )
