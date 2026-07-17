"""
Per-timeframe feature computation on COMPLETED candles only.

One TFFeatures object is built per timeframe per completed bar and shared
by every strategy that trades that timeframe, so the (comparatively
expensive) structure/liquidity/zone analysis runs once, not per strategy.
All indicator values at index i use candles up to and including i — the
same no-look-ahead discipline as the detectors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from adaptive_bot.core.helpers import atr_series, efficiency_ratio, rate_of_change
from adaptive_bot.core.models import (Candle, FairValueGap, LiquidityLevel,
                                      OrderBlock, SweepEvent, Timeframe, Zone)
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


def highest(candles: Sequence[Candle], n: int, exclude_last: bool = True) -> float:
    """Highest high of the previous n candles (excluding the current one)."""
    src = candles[:-1] if exclude_last else candles
    window = src[-n:] if len(src) >= 1 else []
    return max(c.high for c in window) if window else float("nan")


def lowest(candles: Sequence[Candle], n: int, exclude_last: bool = True) -> float:
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
    eff_ratio: float = 0.0
    roc: float = 0.0
    vol_ma20: float = 0.0
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


class FeatureBuilder:

    def __init__(self, cfg):
        """cfg: V4Config (carries the detector parameters)."""
        self.cfg = cfg

    def build(self, tf: Timeframe, candles: List[Candle],
              session_marks: Optional[Dict[str, float]] = None) -> Optional[TFFeatures]:
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
        hist = [a for a in atr[-200:] if a > 0]
        atr_pct = (sum(1 for a in hist if a <= atr[-1]) / len(hist)) if hist else 0.5
        return TFFeatures(
            tf=tf, candles=candles, atr=atr,
            ema20=ema_series(closes, 20), ema50=ema_series(closes, 50),
            ema100=ema_series(closes, 100), rsi14=rsi_series(closes, 14),
            structure=structure, zones=zones, fvgs=fvgs, order_blocks=obs,
            liquidity=levels, sweeps=sweeps,
            eff_ratio=efficiency_ratio(candles, 20),
            roc=rate_of_change(candles, 20),
            vol_ma20=(sum(vols[-20:]) / 20.0) if len(vols) >= 20 else 0.0,
            atr_percentile=atr_pct,
        )
