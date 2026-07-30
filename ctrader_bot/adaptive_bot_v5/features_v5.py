"""
Per-timeframe feature computation on COMPLETED candles only.

One TFFeatures object is built per timeframe per completed bar and shared by
every setup family, so the comparatively expensive structure / liquidity /
zone analysis runs once per bar rather than once per strategy.  All indicator
values at index i use candles up to and including i — the same no-look-ahead
discipline as the detectors themselves.

V5 additions over the V4 feature set:
  * volume_ratio        last volume vs its 20-bar mean (momentum confirmation)
  * body_atr            last body measured in ATR (displacement strength)
  * displacement_flags  per-bar displacement booleans, so "displacement in the
                        last N bars" is a lookup rather than a re-computation
  * protected_swing()   the swing that must hold for a given direction, used
                        by the trailing stop and the early-exit detector
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from adaptive_bot.core.helpers import (atr_series, efficiency_ratio,
                                       is_displacement, rate_of_change)
from adaptive_bot.core.models import (Candle, Direction, FairValueGap,
                                      LiquidityLevel, OrderBlock, SweepEvent,
                                      SwingKind, SwingPoint, Timeframe, Zone)
from adaptive_bot.strategy.fair_value_gap import FVGDetector
from adaptive_bot.strategy.liquidity import LiquidityDetector
from adaptive_bot.strategy.market_structure import (StructureAnalyzer,
                                                    StructureState)
from adaptive_bot.strategy.supply_demand import (OrderBlockDetector,
                                                 SupplyDemandDetector)


def ema_series(values: Sequence[float], n: int) -> List[float]:
    """EMA; warm-up is a running mean so out[i] is always defined."""
    out: List[float] = []
    if not values:
        return out
    k = 2.0 / (n + 1)
    run = 0.0
    for i, v in enumerate(values):
        if i < n:
            run += v
            out.append(run / (i + 1))
        else:
            out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_series(values: Sequence[float], n: int = 14) -> List[float]:
    """Wilder RSI; neutral 50 during warm-up."""
    out = [50.0] * len(values)
    if len(values) < n + 1:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n
    out[n] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    return out


def highest(candles: Sequence[Candle], n: int,
            exclude_last: bool = True) -> float:
    """Highest high of the previous n candles (excluding the current one)."""
    src = candles[:-1] if exclude_last else candles
    window = src[-n:] if len(src) >= 1 else []
    return max(c.high for c in window) if window else float("nan")


def lowest(candles: Sequence[Candle], n: int,
           exclude_last: bool = True) -> float:
    src = candles[:-1] if exclude_last else candles
    window = src[-n:] if len(src) >= 1 else []
    return min(c.low for c in window) if window else float("nan")


@dataclass
class TFFeatures:
    tf: Timeframe
    candles: List[Candle]
    atr: List[float]
    ema20: List[float]
    ema50: List[float]
    ema100: List[float]
    rsi14: List[float]
    structure: StructureState
    zones: List[Zone]
    fvgs: List[FairValueGap]
    order_blocks: List[OrderBlock]
    liquidity: List[LiquidityLevel]
    sweeps: List[SweepEvent]
    displacement_flags: List[bool] = field(default_factory=list)
    eff_ratio: float = 0.0
    roc: float = 0.0
    vol_ma20: float = 0.0
    volume_ratio: float = 1.0
    body_atr: float = 0.0
    atr_percentile: float = 0.5
    extras: Dict[str, float] = field(default_factory=dict)

    @property
    def last(self) -> Candle:
        return self.candles[-1]

    @property
    def prev(self) -> Candle:
        return self.candles[-2]

    @property
    def atr_now(self) -> float:
        return self.atr[-1] if self.atr else 0.0

    @property
    def close(self) -> float:
        return self.candles[-1].close

    @property
    def ema20_now(self) -> float:
        return self.ema20[-1] if self.ema20 else 0.0

    @property
    def ema50_now(self) -> float:
        return self.ema50[-1] if self.ema50 else 0.0

    def displaced_within(self, bars: int,
                         direction: Optional[Direction] = None) -> bool:
        """True when a displacement candle closed in `bars` recent bars."""
        n = len(self.candles)
        start = max(0, n - bars)
        for i in range(start, n):
            if i >= len(self.displacement_flags) or not self.displacement_flags[i]:
                continue
            if direction is None:
                return True
            c = self.candles[i]
            if (c.close > c.open) == (direction == Direction.LONG):
                return True
        return False

    def protected_swing(self, direction: Direction) -> Optional[SwingPoint]:
        """The swing that must hold for a trade in `direction`.

        For a long that is the most recent confirmed higher low; for a short
        the most recent confirmed lower high.  Falls back to the last
        confirmed swing of the right kind when the structure engine has not
        promoted one yet."""
        st = self.structure
        if direction == Direction.LONG:
            if st.protected_low is not None:
                return st.protected_low
            return st.last_confirmed_low
        if st.protected_high is not None:
            return st.protected_high
        return st.last_confirmed_high

    def swing_extreme(self, kind: SwingKind,
                      lookback: int = 12) -> Optional[SwingPoint]:
        """Most recent confirmed swing of `kind` within `lookback` swings."""
        for s in reversed(self.structure.swings[-lookback:]):
            if s.kind == kind:
                return s
        return None


class FeatureBuilder:

    def __init__(self, cfg):
        """cfg: V5Config (carries the shared detector parameters)."""
        self.cfg = cfg

    def build(self, tf: Timeframe, candles: List[Candle],
              session_marks: Optional[Dict[str, float]] = None
              ) -> Optional[TFFeatures]:
        cfg = self.cfg
        if len(candles) < max(cfg.min_candles_required, cfg.atr_period + 25):
            return None
        closes = [c.close for c in candles]
        vols = [c.volume for c in candles]
        atr = atr_series(candles, cfg.atr_period)
        analyzer = StructureAnalyzer(cfg)
        structure = analyzer.analyze(candles, atr)
        zones = SupplyDemandDetector(cfg, tf).detect(candles, structure)
        fvgs = FVGDetector(cfg, tf).detect(candles)
        liq = LiquidityDetector(cfg)
        levels = liq.detect_levels(candles, structure.swings, session_marks)
        sweeps = liq.update_states(levels, candles, atr)
        obs = OrderBlockDetector(cfg, tf).detect(candles, structure, sweeps)

        flags = [is_displacement(candles[i], atr[i] if i < len(atr) else 0.0,
                                 cfg.displacement_atr_mult,
                                 cfg.displacement_body_ratio)
                 for i in range(len(candles))]
        hist = [a for a in atr[-200:] if a > 0]
        atr_pct = (sum(1 for a in hist if a <= atr[-1]) / len(hist)) \
            if hist else 0.5
        vol_ma = (sum(vols[-20:]) / 20.0) if len(vols) >= 20 else 0.0
        vol_ratio = (vols[-1] / vol_ma) if vol_ma > 0 else 1.0
        atr_now = atr[-1] if atr else 0.0
        body_atr = (candles[-1].body / atr_now) if atr_now > 0 else 0.0

        return TFFeatures(
            tf=tf, candles=candles, atr=atr,
            ema20=ema_series(closes, 20), ema50=ema_series(closes, 50),
            ema100=ema_series(closes, 100), rsi14=rsi_series(closes, 14),
            structure=structure, zones=zones, fvgs=fvgs, order_blocks=obs,
            liquidity=levels, sweeps=sweeps, displacement_flags=flags,
            eff_ratio=efficiency_ratio(candles, 20),
            roc=rate_of_change(candles, 20),
            vol_ma20=vol_ma, volume_ratio=vol_ratio, body_atr=body_atr,
            atr_percentile=atr_pct)
