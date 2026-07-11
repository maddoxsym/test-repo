"""
Small pure-math utilities shared by the strategy modules.
All functions operate on COMPLETED candles only and never look ahead:
value at index i uses candles up to and including i.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from .models import Candle, Timeframe

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def true_range(prev_close: float, c: Candle) -> float:
    return max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))


def atr_series(candles: Sequence[Candle], period: int) -> List[float]:
    """Wilder ATR. atr[i] uses candles up to and including i (no look-ahead)."""
    n = len(candles)
    out = [0.0] * n
    if n == 0:
        return out
    trs = [candles[0].range]
    for i in range(1, n):
        trs.append(true_range(candles[i - 1].close, candles[i]))
    if n < period:
        run = 0.0
        for i in range(n):
            run += trs[i]
            out[i] = run / (i + 1)
        return out
    first = sum(trs[:period]) / period
    for i in range(period):
        out[i] = sum(trs[:i + 1]) / (i + 1)
    out[period - 1] = first
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period
    return out


def atr_at(candles: Sequence[Candle], period: int) -> float:
    if not candles:
        return 0.0
    return atr_series(candles, period)[-1]


def percentile_rank(values: Sequence[float], x: float) -> float:
    """Fraction of values <= x. 0..1."""
    if not values:
        return 0.5
    return sum(1 for v in values if v <= x) / len(values)


def efficiency_ratio(candles: Sequence[Candle], lookback: int) -> float:
    """Kaufman efficiency: |net move| / sum(|candle-to-candle moves|)."""
    if len(candles) < lookback + 1:
        return 0.0
    window = candles[-(lookback + 1):]
    net = abs(window[-1].close - window[0].close)
    path = sum(abs(window[i].close - window[i - 1].close)
               for i in range(1, len(window)))
    return net / path if path > 0 else 0.0


def rate_of_change(candles: Sequence[Candle], lookback: int) -> float:
    if len(candles) < lookback + 1 or candles[-(lookback + 1)].close == 0:
        return 0.0
    return ((candles[-1].close - candles[-(lookback + 1)].close)
            / candles[-(lookback + 1)].close)


def is_displacement(candle: Candle, atr: float,
                    atr_mult: float, body_ratio: float) -> bool:
    """Objective displacement: large body relative to ATR, dominant body."""
    if atr <= 0:
        return False
    return (candle.body >= atr_mult * atr
            and candle.body_ratio >= body_ratio)


def tf_bucket_start(t: datetime, tf: Timeframe) -> datetime:
    """Timezone-aware open time of the tf bucket containing t."""
    if tf == Timeframe.W1:
        d = t.date() - timedelta(days=t.weekday())  # Monday
        return datetime(d.year, d.month, d.day, tzinfo=t.tzinfo)
    if tf == Timeframe.D1:
        return datetime(t.year, t.month, t.day, tzinfo=t.tzinfo)
    mins = tf.minutes
    total = t.hour * 60 + t.minute
    start = (total // mins) * mins
    return datetime(t.year, t.month, t.day, start // 60, start % 60,
                    tzinfo=t.tzinfo)


def resample(candles: Sequence[Candle], tf: Timeframe,
             completed_only: bool = True,
             now: Optional[datetime] = None) -> List[Candle]:
    """Aggregate base candles into tf candles. A bucket is emitted only when
    it is complete relative to `now` (default: close time of last base
    candle). No future data can leak: buckets are built strictly from base
    candles whose open time falls inside the bucket."""
    if not candles:
        return []
    base_tf_seconds = None
    if len(candles) >= 2:
        base_tf_seconds = int((candles[1].time - candles[0].time).total_seconds())
    if now is None:
        last = candles[-1]
        step = base_tf_seconds or 60
        now = last.time + timedelta(seconds=step)
    out: List[Candle] = []
    cur_start: Optional[datetime] = None
    o = h = l = c = 0.0
    vol = 0.0
    spr = 0.0
    n_in = 0
    for cd in candles:
        b = tf_bucket_start(cd.time, tf)
        if cur_start is None or b != cur_start:
            if cur_start is not None and n_in > 0:
                out.append(Candle(cur_start, o, h, l, c, vol, spr / max(n_in, 1)))
            cur_start, o, h, l, c = b, cd.open, cd.high, cd.low, cd.close
            vol, spr, n_in = cd.volume, cd.spread, 1
        else:
            h = max(h, cd.high)
            l = min(l, cd.low)
            c = cd.close
            vol += cd.volume
            spr += cd.spread
            n_in += 1
    if cur_start is not None and n_in > 0:
        bucket_end = cur_start + timedelta(minutes=tf.minutes)
        if not completed_only or now >= bucket_end:
            out.append(Candle(cur_start, o, h, l, c, vol, spr / max(n_in, 1)))
    return out
