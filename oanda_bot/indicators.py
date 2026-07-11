"""
Pure-python technical indicators operating on plain lists of floats.

Every function returns a list the same length as its input, padded with
``None`` during the warm-up period, so index i of any output always refers
to candle i of the input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

Series = list[Optional[float]]


@dataclass
class Candle:
    time: float          # unix epoch seconds (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    complete: bool = True


def closes(candles: list[Candle]) -> list[float]:
    return [c.close for c in candles]


def highs(candles: list[Candle]) -> list[float]:
    return [c.high for c in candles]


def lows(candles: list[Candle]) -> list[float]:
    return [c.low for c in candles]


# --------------------------------------------------------------------------


def sma(values: list[float], n: int) -> Series:
    out: Series = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    total = sum(values[:n])
    out[n - 1] = total / n
    for i in range(n, len(values)):
        total += values[i] - values[i - n]
        out[i] = total / n
    return out


def ema(values: list[float], n: int) -> Series:
    out: Series = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], n: int = 14) -> Series:
    """Wilder's RSI."""
    out: Series = [None] * len(values)
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


def true_ranges(candles: list[Candle]) -> list[float]:
    tr = [candles[0].high - candles[0].low] if candles else []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        tr.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    return tr


def atr(candles: list[Candle], n: int = 14) -> Series:
    """Wilder's ATR."""
    tr = true_ranges(candles)
    out: Series = [None] * len(candles)
    if len(tr) < n:
        return out
    prev = sum(tr[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(tr)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26,
         signal_n: int = 9) -> tuple[Series, Series, Series]:
    """Returns (macd_line, signal_line, histogram)."""
    ef, es = ema(values, fast), ema(values, slow)
    line: Series = [None] * len(values)
    for i in range(len(values)):
        if ef[i] is not None and es[i] is not None:
            line[i] = ef[i] - es[i]
    valid = [(i, v) for i, v in enumerate(line) if v is not None]
    sig: Series = [None] * len(values)
    if len(valid) >= signal_n:
        vals = [v for _, v in valid]
        s = ema(vals, signal_n)
        for (i, _), sv in zip(valid, s):
            sig[i] = sv
    hist: Series = [None] * len(values)
    for i in range(len(values)):
        if line[i] is not None and sig[i] is not None:
            hist[i] = line[i] - sig[i]
    return line, sig, hist


def bollinger(values: list[float], n: int = 20,
              k: float = 2.0) -> tuple[Series, Series, Series]:
    """Returns (middle, upper, lower)."""
    mid = sma(values, n)
    up: Series = [None] * len(values)
    lo: Series = [None] * len(values)
    for i in range(n - 1, len(values)):
        m = mid[i]
        var = sum((values[j] - m) ** 2 for j in range(i - n + 1, i + 1)) / n
        sd = var ** 0.5
        up[i] = m + k * sd
        lo[i] = m - k * sd
    return mid, up, lo


def adx(candles: list[Candle], n: int = 14) -> Series:
    """Wilder's ADX."""
    out: Series = [None] * len(candles)
    if len(candles) < 2 * n + 1:
        return out
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        dn = candles[i - 1].low - candles[i].low
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
    tr = true_ranges(candles)

    # Wilder smoothing (running sums)
    s_tr = sum(tr[1:n + 1])
    s_p = sum(plus_dm[1:n + 1])
    s_m = sum(minus_dm[1:n + 1])
    dxs: list[float] = []
    for i in range(n + 1, len(candles)):
        s_tr = s_tr - s_tr / n + tr[i]
        s_p = s_p - s_p / n + plus_dm[i]
        s_m = s_m - s_m / n + minus_dm[i]
        p_di = 100.0 * s_p / s_tr if s_tr else 0.0
        m_di = 100.0 * s_m / s_tr if s_tr else 0.0
        dx = 100.0 * abs(p_di - m_di) / (p_di + m_di) if (p_di + m_di) else 0.0
        dxs.append(dx)
        if len(dxs) == n:
            out[i] = sum(dxs) / n
        elif len(dxs) > n:
            out[i] = (out[i - 1] * (n - 1) + dx) / n
    return out


def donchian(candles: list[Candle], n: int = 20) -> tuple[Series, Series]:
    """Highest high / lowest low of the *previous* n candles (excluding current)."""
    up: Series = [None] * len(candles)
    lo: Series = [None] * len(candles)
    for i in range(n, len(candles)):
        window = candles[i - n:i]
        up[i] = max(c.high for c in window)
        lo[i] = min(c.low for c in window)
    return up, lo


def slope(series: Series, lookback: int = 10) -> Optional[float]:
    """Simple slope of the last `lookback` points of an indicator series."""
    vals = [v for v in series if v is not None]
    if len(vals) < lookback:
        return None
    window = vals[-lookback:]
    return (window[-1] - window[0]) / lookback
