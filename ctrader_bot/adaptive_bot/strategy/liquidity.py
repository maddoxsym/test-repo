"""
Liquidity: resting pools (equal highs/lows, swing highs/lows, previous
day/week highs/lows, session highs/lows) and OBJECTIVE sweep detection.

Sweep definition: price TRADES THROUGH a recognised level (high above
buy-side liquidity / low below sell-side liquidity) AND EITHER the candle
closes back on the original side of the level OR a displacement candle
moves away within SWEEP_CONFIRM_CANDLES.  A wick through alone is NOT a
sweep, and a touch alone never justifies an entry.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from ..core.config import Config
from ..core.helpers import atr_at, atr_series, is_displacement, resample, utcnow
from ..core.models import (Candle, Direction, LiquidityKind, LiquidityLevel,
                           LiquidityState, SweepEvent, SwingKind, SwingPoint,
                           Timeframe, new_id)


class LiquidityDetector:

    SWEEP_CONFIRM_CANDLES = 2

    def __init__(self, cfg: Config):
        self.cfg = cfg

    # -- level construction --------------------------------------------------
    def detect_levels(self, candles: Sequence[Candle],
                      swings: Sequence[SwingPoint],
                      session_marks: Optional[Dict[str, float]] = None,
                      ) -> List[LiquidityLevel]:
        levels: List[LiquidityLevel] = []
        atr = atr_at(candles, self.cfg.atr_period)
        tol = self.cfg.eq_level_atr_tol * atr if atr > 0 else 0.0

        highs = [s for s in swings if s.kind == SwingKind.HIGH]
        lows = [s for s in swings if s.kind == SwingKind.LOW]

        # equal highs / equal lows (>=2 swings within tolerance)
        levels += self._equal_clusters(highs, tol, True)
        levels += self._equal_clusters(lows, tol, False)

        # individual recent swing highs/lows (last 8 each)
        for s in highs[-8:]:
            levels.append(LiquidityLevel(new_id("liq"), LiquidityKind.SWING_HIGH,
                                         s.price, s.time, buy_side=True))
        for s in lows[-8:]:
            levels.append(LiquidityLevel(new_id("liq"), LiquidityKind.SWING_LOW,
                                         s.price, s.time, buy_side=False))

        # previous day / week highs & lows from resampled completed periods
        d1 = resample(candles, Timeframe.D1)
        if len(d1) >= 2:
            pd_ = d1[-2]
            levels.append(LiquidityLevel(new_id("liq"), LiquidityKind.PDH,
                                         pd_.high, pd_.time, buy_side=True))
            levels.append(LiquidityLevel(new_id("liq"), LiquidityKind.PDL,
                                         pd_.low, pd_.time, buy_side=False))
        w1 = resample(candles, Timeframe.W1)
        if len(w1) >= 2:
            pw = w1[-2]
            levels.append(LiquidityLevel(new_id("liq"), LiquidityKind.PWH,
                                         pw.high, pw.time, buy_side=True))
            levels.append(LiquidityLevel(new_id("liq"), LiquidityKind.PWL,
                                         pw.low, pw.time, buy_side=False))

        # session highs/lows supplied by the SessionManager
        if session_marks:
            t = candles[-1].time if candles else utcnow()
            for name, price in session_marks.items():
                if price is None:
                    continue
                buy_side = name.endswith("_high")
                levels.append(LiquidityLevel(
                    new_id("liq"),
                    LiquidityKind.SESSION_HIGH if buy_side else LiquidityKind.SESSION_LOW,
                    price, t, buy_side=buy_side))
        return levels

    def _equal_clusters(self, swings: Sequence[SwingPoint], tol: float,
                        buy_side: bool) -> List[LiquidityLevel]:
        out: List[LiquidityLevel] = []
        if tol <= 0 or len(swings) < 2:
            return out
        used: set = set()
        pts = list(swings[-12:])
        for i in range(len(pts)):
            if i in used:
                continue
            cluster = [pts[i]]
            for j in range(i + 1, len(pts)):
                if j in used:
                    continue
                if abs(pts[j].price - pts[i].price) <= tol:
                    cluster.append(pts[j])
                    used.add(j)
            if len(cluster) >= 2:
                used.add(i)
                # stops rest just beyond the extreme of the cluster
                price = (max if buy_side else min)(p.price for p in cluster)
                kind = LiquidityKind.EQUAL_HIGHS if buy_side else LiquidityKind.EQUAL_LOWS
                out.append(LiquidityLevel(
                    new_id("liq"), kind, price, cluster[-1].time,
                    buy_side=buy_side,
                    member_prices=tuple(p.price for p in cluster)))
        return out

    # -- state updates & sweep detection --------------------------------------
    def update_states(self, levels: List[LiquidityLevel],
                      candles: Sequence[Candle],
                      atr: Optional[List[float]] = None) -> List[SweepEvent]:
        """Walk candles chronologically, updating level states.
        Returns the sweep events found (most recent last)."""
        cfg = self.cfg
        atr = atr or atr_series(candles, cfg.atr_period)
        sweeps: List[SweepEvent] = []
        for lvl in levels:
            if lvl.state != LiquidityState.UNTOUCHED:
                continue
            # only consider candles after the level existed
            for i, c in enumerate(candles):
                if c.time <= lvl.time:
                    continue
                pierced = c.high > lvl.price if lvl.buy_side else c.low < lvl.price
                if not pierced:
                    continue
                closed_back = c.close < lvl.price if lvl.buy_side else c.close > lvl.price
                displaced = False
                # displacement away within confirm window
                for k in range(i, min(i + self.SWEEP_CONFIRM_CANDLES + 1, len(candles))):
                    ck = candles[k]
                    a = atr[k] if k < len(atr) else 0.0
                    if not is_displacement(ck, a, cfg.displacement_atr_mult,
                                           cfg.displacement_body_ratio):
                        continue
                    if lvl.buy_side and ck.bearish and ck.close < lvl.price:
                        displaced = True
                        break
                    if not lvl.buy_side and ck.bullish and ck.close > lvl.price:
                        displaced = True
                        break
                extreme = c.high if lvl.buy_side else c.low
                ev = SweepEvent(level=lvl, index=i, time=c.time,
                                extreme=extreme, closed_back=closed_back,
                                displaced_away=displaced)
                if ev.valid:
                    lvl.state = LiquidityState.SWEPT
                    lvl.swept_time = c.time
                    sweeps.append(ev)
                else:
                    # traded through and stayed beyond => level consumed
                    if (lvl.buy_side and c.close > lvl.price) or \
                       (not lvl.buy_side and c.close < lvl.price):
                        lvl.state = LiquidityState.INVALIDATED
                break
        sweeps.sort(key=lambda s: s.index)
        return sweeps

    @staticmethod
    def nearest_target(levels: Sequence[LiquidityLevel], price: float,
                       direction: Direction) -> Optional[LiquidityLevel]:
        """Nearest untouched opposing liquidity in the trade direction."""
        if direction == Direction.LONG:
            cands = [l for l in levels if l.buy_side and l.price > price
                     and l.state == LiquidityState.UNTOUCHED]
            return min(cands, key=lambda l: l.price - price) if cands else None
        cands = [l for l in levels if not l.buy_side and l.price < price
                 and l.state == LiquidityState.UNTOUCHED]
        return min(cands, key=lambda l: price - l.price) if cands else None

    @staticmethod
    def targets_beyond(levels: Sequence[LiquidityLevel], price: float,
                       direction: Direction, count: int = 3) -> List[LiquidityLevel]:
        if direction == Direction.LONG:
            cands = sorted([l for l in levels if l.buy_side and l.price > price
                            and l.state == LiquidityState.UNTOUCHED],
                           key=lambda l: l.price)
        else:
            cands = sorted([l for l in levels if not l.buy_side and l.price < price
                            and l.state == LiquidityState.UNTOUCHED],
                           key=lambda l: -l.price)
        return cands[:count]
