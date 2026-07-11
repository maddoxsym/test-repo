"""
The strategy analysts.  Each one looks at the same candles and either
proposes a trade (direction + confidence + reasoning) or stays silent.
None of them can trade alone -- the TradeCouncil requires multiple
independent confirmations, and the PerformanceCoach reweights each
analyst's vote by its live track record.

    trend_rider      : EMA20/EMA50 alignment with ADX strength, H1 agreement
    range_fader      : Bollinger-band + RSI mean reversion (ranging markets)
    breakout_hunter  : Donchian-channel breakout with range expansion
    momentum_surfer  : MACD histogram flip with RSI alignment
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .indicators import (Candle, adx, atr, bollinger, closes, donchian, ema,
                         macd, rsi, slope)
from .regime import RANGING, TRENDING_DOWN, TRENDING_UP, VOLATILE

LONG, SHORT = 1, -1


@dataclass
class Signal:
    strategy: str
    direction: int              # +1 long, -1 short
    confidence: float           # 0..1
    reason: str
    preferred_regimes: tuple[str, ...] = field(default=())


class Strategy:
    name = "base"
    preferred_regimes: tuple[str, ...] = ()

    def evaluate(self, m5: list[Candle], h1: list[Candle]) -> Signal | None:
        raise NotImplementedError

    def _make(self, direction: int, confidence: float, reason: str) -> Signal:
        return Signal(self.name, direction, max(0.0, min(1.0, confidence)),
                      reason, self.preferred_regimes)


class TrendRider(Strategy):
    name = "trend_rider"
    preferred_regimes = (TRENDING_UP, TRENDING_DOWN)

    def evaluate(self, m5, h1):
        if len(m5) < 60 or len(h1) < 60:
            return None
        c = closes(m5)
        e20, e50 = ema(c, 20), ema(c, 50)
        a = adx(m5, 14)
        if None in (e20[-1], e50[-1], a[-1]):
            return None
        if a[-1] < 20:
            return None
        h1_slope = slope(ema(closes(h1), 50), 10)
        if h1_slope is None:
            return None
        price = c[-1]
        conf = min(1.0, 0.45 + (a[-1] - 20) / 60)
        if e20[-1] > e50[-1] and price > e20[-1] and h1_slope > 0:
            return self._make(LONG, conf + 0.15,
                              f"EMA20>EMA50, price above, ADX {a[-1]:.0f}, H1 up")
        if e20[-1] < e50[-1] and price < e20[-1] and h1_slope < 0:
            return self._make(SHORT, conf + 0.15,
                              f"EMA20<EMA50, price below, ADX {a[-1]:.0f}, H1 down")
        return None


class RangeFader(Strategy):
    name = "range_fader"
    preferred_regimes = (RANGING,)

    def evaluate(self, m5, h1):
        if len(m5) < 40:
            return None
        c = closes(m5)
        _, upper, lower = bollinger(c, 20, 2.0)
        r = rsi(c, 14)
        a = atr(m5, 14)
        if None in (upper[-1], lower[-1], r[-1], a[-1]) or a[-1] <= 0:
            return None
        price = c[-1]
        if price < lower[-1] and r[-1] < 30:
            depth = (lower[-1] - price) / a[-1]
            return self._make(LONG, 0.5 + min(0.4, depth * 0.4),
                              f"below lower Bollinger, RSI {r[-1]:.0f}")
        if price > upper[-1] and r[-1] > 70:
            depth = (price - upper[-1]) / a[-1]
            return self._make(SHORT, 0.5 + min(0.4, depth * 0.4),
                              f"above upper Bollinger, RSI {r[-1]:.0f}")
        return None


class BreakoutHunter(Strategy):
    name = "breakout_hunter"
    preferred_regimes = (TRENDING_UP, TRENDING_DOWN, VOLATILE)

    def evaluate(self, m5, h1):
        if len(m5) < 40:
            return None
        c = closes(m5)
        up, lo = donchian(m5, 20)
        a = atr(m5, 14)
        if None in (up[-1], lo[-1], a[-1]) or a[-1] <= 0:
            return None
        last = m5[-1]
        cur_range = last.high - last.low
        expansion = cur_range / a[-1]
        if expansion < 1.2:
            return None                       # breakout without energy = trap
        conf = 0.5 + min(0.4, (expansion - 1.2) * 0.3)
        if last.close > up[-1]:
            return self._make(LONG, conf,
                              f"closed above 20-bar high, range {expansion:.1f}x ATR")
        if last.close < lo[-1]:
            return self._make(SHORT, conf,
                              f"closed below 20-bar low, range {expansion:.1f}x ATR")
        return None


class MomentumSurfer(Strategy):
    name = "momentum_surfer"
    preferred_regimes = (TRENDING_UP, TRENDING_DOWN)

    def evaluate(self, m5, h1):
        if len(m5) < 60:
            return None
        c = closes(m5)
        _, _, hist = macd(c)
        r = rsi(c, 14)
        if len(hist) < 2 or None in (hist[-1], hist[-2], r[-1]):
            return None
        flipped_up = hist[-2] <= 0 < hist[-1]
        flipped_down = hist[-2] >= 0 > hist[-1]
        if flipped_up and 50 <= r[-1] <= 70:
            return self._make(LONG, 0.55 + (r[-1] - 50) / 100,
                              f"MACD hist flipped positive, RSI {r[-1]:.0f}")
        if flipped_down and 30 <= r[-1] <= 50:
            return self._make(SHORT, 0.55 + (50 - r[-1]) / 100,
                              f"MACD hist flipped negative, RSI {r[-1]:.0f}")
        return None


ALL_STRATEGIES: list[Strategy] = [
    TrendRider(), RangeFader(), BreakoutHunter(), MomentumSurfer(),
]
