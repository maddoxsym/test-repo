"""
MarketRegimeAnalyst -- classifies the higher-timeframe market state so the
council knows which strategies to trust right now.

Regimes:
    trending_up / trending_down : ADX strong, EMA50 sloping
    volatile                    : ATR well above its recent norm
    ranging                     : everything else
"""

from __future__ import annotations

from dataclasses import dataclass

from .indicators import Candle, adx, atr, closes, ema, slope

TRENDING_UP = "trending_up"
TRENDING_DOWN = "trending_down"
RANGING = "ranging"
VOLATILE = "volatile"


@dataclass
class RegimeView:
    regime: str
    adx: float | None
    detail: str


class MarketRegimeAnalyst:
    def __init__(self, adx_trend_min: float = 25.0, atr_volatile_mult: float = 1.6):
        self.adx_trend_min = adx_trend_min
        self.atr_volatile_mult = atr_volatile_mult

    def classify(self, h1: list[Candle]) -> RegimeView:
        if len(h1) < 120:
            return RegimeView(RANGING, None, "not enough H1 history; assuming ranging")

        c = closes(h1)
        adx_series = adx(h1, 14)
        atr_series = atr(h1, 14)
        ema50 = ema(c, 50)

        cur_adx = adx_series[-1]
        cur_atr = atr_series[-1]
        ema_slope = slope(ema50, 10)

        # volatility check: current ATR vs median of the last 100 readings
        recent_atr = [v for v in atr_series[-100:] if v is not None]
        if cur_atr is not None and recent_atr:
            med = sorted(recent_atr)[len(recent_atr) // 2]
            if med > 0 and cur_atr > med * self.atr_volatile_mult:
                return RegimeView(
                    VOLATILE, cur_adx,
                    f"H1 ATR {cur_atr:.5f} is {cur_atr / med:.1f}x its median")

        if cur_adx is not None and cur_adx >= self.adx_trend_min and ema_slope:
            if ema_slope > 0:
                return RegimeView(TRENDING_UP, cur_adx,
                                  f"ADX {cur_adx:.0f}, EMA50 rising")
            return RegimeView(TRENDING_DOWN, cur_adx,
                              f"ADX {cur_adx:.0f}, EMA50 falling")

        return RegimeView(RANGING, cur_adx,
                          f"ADX {cur_adx:.0f} below {self.adx_trend_min:.0f}"
                          if cur_adx is not None else "ADX warming up")
