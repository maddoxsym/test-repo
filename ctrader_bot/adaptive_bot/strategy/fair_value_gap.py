"""
Strict three-candle fair value gaps (imbalances).

Bullish FVG at middle candle i: low[i+1] > high[i-1]  (gap up)
Bearish FVG at middle candle i: high[i+1] < low[i-1]  (gap down)
Gap must be >= fvg_min_size_atr * ATR. Fill state is tracked candle by
candle (UNFILLED / PARTIAL / MITIGATED). An FVG is confluence only — it is
never enough to enter a trade by itself.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from ..core.config import Config
from ..core.helpers import atr_series, is_displacement
from ..core.models import (Candle, Direction, FairValueGap, FVGState,
                           Timeframe, new_id)


class FVGDetector:

    def __init__(self, cfg: Config, timeframe: Timeframe):
        self.cfg = cfg
        self.tf = timeframe

    def detect(self, candles: Sequence[Candle]) -> List[FairValueGap]:
        cfg = self.cfg
        n = len(candles)
        if n < 3:
            return []
        atr = atr_series(candles, cfg.atr_period)
        gaps: List[FairValueGap] = []
        for i in range(1, n - 1):
            a = atr[i]
            if a <= 0:
                continue
            c_prev, c_mid, c_next = candles[i - 1], candles[i], candles[i + 1]
            min_size = cfg.fvg_min_size_atr * a
            if c_next.low > c_prev.high and (c_next.low - c_prev.high) >= min_size:
                gaps.append(FairValueGap(
                    fvg_id=new_id("fvg"), direction=Direction.LONG,
                    upper=c_next.low, lower=c_prev.high, timeframe=self.tf,
                    created_time=c_mid.time, created_index=i,
                    from_displacement=is_displacement(
                        c_mid, a, cfg.displacement_atr_mult,
                        cfg.displacement_body_ratio)))
            elif c_next.high < c_prev.low and (c_prev.low - c_next.high) >= min_size:
                gaps.append(FairValueGap(
                    fvg_id=new_id("fvg"), direction=Direction.SHORT,
                    upper=c_prev.low, lower=c_next.high, timeframe=self.tf,
                    created_time=c_mid.time, created_index=i,
                    from_displacement=is_displacement(
                        c_mid, a, cfg.displacement_atr_mult,
                        cfg.displacement_body_ratio)))
        self._update_fill_states(gaps, candles)
        return gaps

    @staticmethod
    def _update_fill_states(gaps: List[FairValueGap],
                            candles: Sequence[Candle]) -> None:
        n = len(candles)
        for g in gaps:
            worst_penetration = 0.0
            for j in range(g.created_index + 2, n):
                c = candles[j]
                if g.direction == Direction.LONG:
                    if c.low <= g.lower:
                        worst_penetration = g.size
                        break
                    if c.low < g.upper:
                        worst_penetration = max(worst_penetration, g.upper - c.low)
                else:
                    if c.high >= g.upper:
                        worst_penetration = g.size
                        break
                    if c.high > g.lower:
                        worst_penetration = max(worst_penetration, c.high - g.lower)
            if g.size <= 0:
                g.state = FVGState.MITIGATED
                g.fill_fraction = 1.0
                continue
            g.fill_fraction = min(1.0, worst_penetration / g.size)
            if g.fill_fraction >= 1.0:
                g.state = FVGState.MITIGATED
            elif g.fill_fraction > 0.0:
                g.state = FVGState.PARTIAL
            else:
                g.state = FVGState.UNFILLED

    @staticmethod
    def usable(gaps: Sequence[FairValueGap],
               direction: Optional[Direction] = None) -> List[FairValueGap]:
        out = [g for g in gaps if g.state != FVGState.MITIGATED]
        if direction:
            out = [g for g in out if g.direction == direction]
        return out
