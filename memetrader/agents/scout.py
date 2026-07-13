"""Scout agent — discovers newly launched coins worth evaluating.

Pulls from every configured feed (pump.fun bonding curve, DexScreener
new pairs) and applies the non-negotiable house rule: every coin is
considered ONLY while its market cap is still low. Nothing that already
did a 50x gets scouted — asymmetric upside exists only at small caps.
"""
from __future__ import annotations

from ..config import StrategyParams
from ..models import TokenSnapshot


class Scout:
    name = "scout"

    def __init__(self, params: StrategyParams, feed):
        self.params = params
        self.feed = feed
        self.seen: set[str] = set()

    def discover(self) -> list[TokenSnapshot]:
        p = self.params
        out: list[TokenSnapshot] = []
        for t in self.feed.new_tokens(max_age_ticks=int(p.max_age_minutes)):
            if t.market_cap < p.min_mcap or t.market_cap > p.max_mcap:
                continue
            if t.liquidity < p.min_liquidity and t.graduated:
                continue
            if t.age_minutes > p.max_age_minutes:
                continue
            out.append(t)
            self.seen.add(t.address)
        return out

    def refresh(self, address: str) -> TokenSnapshot | None:
        return self.feed.snapshot(address)
