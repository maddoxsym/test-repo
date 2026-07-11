"""
Objective market-regime classification from structure + volatility inputs.
Determines which of the six entry models are allowed to run right now.
"""

from __future__ import annotations

from typing import Sequence

from ..core.config import Config
from ..core.helpers import (atr_series, efficiency_ratio, is_displacement,
                            percentile_rank, rate_of_change)
from ..core.models import (Candle, Regime, RegimeReading, StructureEventKind,
                           TrendState)
from .market_structure import StructureState


class MarketRegimeDetector:

    ATR_HISTORY = 200
    EFF_LOOKBACK = 20
    RANGE_LOOKBACK = 40

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def classify(self, candles: Sequence[Candle],
                 structure: StructureState,
                 spread_points: float = 0.0,
                 news_active: bool = False) -> RegimeReading:
        cfg = self.cfg
        if len(candles) < cfg.atr_period + self.EFF_LOOKBACK + 5:
            return RegimeReading(Regime.UNSAFE, TrendState.UNDEFINED, 0.0,
                                 0.0, 0.5, 0.0, "insufficient candle history")
        atr_all = atr_series(candles, cfg.atr_period)
        atr_now = atr_all[-1]
        hist = atr_all[-self.ATR_HISTORY:]
        atr_pct = percentile_rank(hist, atr_now)
        eff = efficiency_ratio(candles, self.EFF_LOOKBACK)
        roc = rate_of_change(candles, self.EFF_LOOKBACK)
        trend = structure.trend

        # spread safety first
        if spread_points > cfg.max_spread_points:
            return RegimeReading(Regime.ABNORMAL_SPREAD, trend, 0.9, atr_now,
                                 atr_pct, eff,
                                 f"spread {spread_points:.0f}pt > max "
                                 f"{cfg.max_spread_points:.0f}pt")
        if news_active:
            return RegimeReading(Regime.NEWS_VOLATILITY, trend, 0.9, atr_now,
                                 atr_pct, eff, "news window active")

        # abnormal candle: last completed candle is a huge outlier
        last = candles[-1]
        if atr_now > 0 and last.range > 4.0 * atr_now:
            return RegimeReading(Regime.NEWS_VOLATILITY, trend, 0.8, atr_now,
                                 atr_pct, eff,
                                 f"outlier candle range {last.range:.2f} > 4x ATR")

        window = candles[-self.RANGE_LOOKBACK:]
        width = max(c.high for c in window) - min(c.low for c in window)
        width_atr = width / atr_now if atr_now > 0 else 0.0

        recent_events = [ev for ev in structure.events
                         if ev.index >= len(candles) - self.EFF_LOOKBACK]
        recent_choch = [ev for ev in recent_events
                        if ev.kind in (StructureEventKind.CHOCH,
                                       StructureEventKind.MSS)]
        disp_count = sum(
            1 for i in range(len(candles) - 6, len(candles))
            if i >= 0 and is_displacement(candles[i], atr_all[i],
                                          cfg.displacement_atr_mult,
                                          cfg.displacement_body_ratio))

        # compression: low vol percentile + narrow range
        if atr_pct <= 0.25 and width_atr < 8.0 and eff < 0.25:
            return RegimeReading(Regime.COMPRESSION, trend, 0.7, atr_now,
                                 atr_pct, eff,
                                 f"ATR pct {atr_pct:.2f}, width {width_atr:.1f} "
                                 f"ATR, eff {eff:.2f}")
        # expansion: vol percentile spiking + displacement burst
        if atr_pct >= 0.85 and disp_count >= 2:
            return RegimeReading(Regime.EXPANSION, trend, 0.7, atr_now,
                                 atr_pct, eff,
                                 f"ATR pct {atr_pct:.2f} with {disp_count} "
                                 f"displacement candles")
        # reversal attempt: fresh counter-trend CHoCH/MSS
        if recent_choch:
            return RegimeReading(Regime.REVERSAL_ATTEMPT, trend, 0.6, atr_now,
                                 atr_pct, eff,
                                 f"recent {recent_choch[-1].kind.value} "
                                 f"against prior trend")
        if trend == TrendState.BULLISH:
            strong = eff >= 0.35 and roc > 0
            return RegimeReading(Regime.STRONG_BULL if strong else Regime.WEAK_BULL,
                                 trend, 0.7 if strong else 0.55, atr_now,
                                 atr_pct, eff,
                                 f"bullish structure, eff {eff:.2f}, roc {roc:+.4f}")
        if trend == TrendState.BEARISH:
            strong = eff >= 0.35 and roc < 0
            return RegimeReading(Regime.STRONG_BEAR if strong else Regime.WEAK_BEAR,
                                 trend, 0.7 if strong else 0.55, atr_now,
                                 atr_pct, eff,
                                 f"bearish structure, eff {eff:.2f}, roc {roc:+.4f}")
        return RegimeReading(Regime.RANGE, trend, 0.6, atr_now, atr_pct, eff,
                             f"no directional structure, eff {eff:.2f}, "
                             f"width {width_atr:.1f} ATR")
