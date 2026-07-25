"""Vectorised technical indicators.

Pure NumPy — no TA-Lib C dependency, so ``pip install`` never fails on a Mac
without Homebrew. Every function returns an array the same length as its input,
padded with ``NaN`` where there is not yet enough history. That padding matters:
it is what stops a strategy from acting on a half-formed indicator.

All functions are **causal**: the value at index ``i`` uses only data from
indices ``<= i``. This is verified by ``tests/unit/test_lookahead.py``, which
truncates the input and asserts the earlier values do not change.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "sma", "ema", "wilder_smooth", "rsi", "macd", "true_range", "atr", "adx",
    "bollinger", "keltner", "donchian", "roc", "vwap_rolling", "realized_volatility",
    "zscore", "rolling_percentile", "swing_highs", "swing_lows", "supertrend",
    "ichimoku", "linreg_slope", "volume_zscore", "stochastic",
]


def _empty(n: int) -> np.ndarray:
    return np.full(n, np.nan, dtype=float)


def sma(values: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average."""
    n = len(values)
    out = _empty(n)
    if period <= 0 or n < period:
        return out
    cumulative = np.cumsum(np.insert(values.astype(float), 0, 0.0))
    out[period - 1 :] = (cumulative[period:] - cumulative[:-period]) / period
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average, seeded with the first full SMA."""
    n = len(values)
    out = _empty(n)
    if period <= 0 or n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = float(np.mean(values[:period]))
    for i in range(period, n):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing (used by RSI, ATR, ADX)."""
    n = len(values)
    out = _empty(n)
    if period <= 0 or n < period:
        return out
    out[period - 1] = float(np.sum(values[:period]))
    for i in range(period, n):
        out[i] = out[i - 1] - (out[i - 1] / period) + values[i]
    return out


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Relative Strength Index (Wilder)."""
    n = len(close)
    out = _empty(n)
    if n <= period:
        return out

    delta = np.diff(close)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)

    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def macd(
    close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(macd_line, signal_line, histogram)``."""
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    macd_line = fast_ema - slow_ema
    valid = ~np.isnan(macd_line)
    signal_line = _empty(len(close))
    if valid.any():
        start = int(np.argmax(valid))
        smoothed = ema(macd_line[start:], signal)
        signal_line[start:] = smoothed
    return macd_line, signal_line, macd_line - signal_line


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    n = len(close)
    out = _empty(n)
    if n == 0:
        return out
    out[0] = high[0] - low[0]
    if n > 1:
        prev_close = close[:-1]
        out[1:] = np.maximum.reduce(
            [high[1:] - low[1:], np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)]
        )
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Average True Range (Wilder)."""
    n = len(close)
    out = _empty(n)
    if n < period + 1:
        return out
    tr = true_range(high, low, close)
    out[period] = float(np.mean(tr[1 : period + 1]))
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(adx, plus_di, minus_di)`` — Wilder's directional movement system."""
    n = len(close)
    adx_out, plus_di, minus_di = _empty(n), _empty(n), _empty(n)
    if n < 2 * period + 1:
        return adx_out, plus_di, minus_di

    up_move = np.zeros(n)
    down_move = np.zeros(n)
    up_move[1:] = high[1:] - high[:-1]
    down_move[1:] = low[:-1] - low[1:]

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = true_range(high, low, close)

    smooth_tr = float(np.sum(tr[1 : period + 1]))
    smooth_plus = float(np.sum(plus_dm[1 : period + 1]))
    smooth_minus = float(np.sum(minus_dm[1 : period + 1]))

    dx_values: list[float] = []
    for i in range(period, n):
        if i > period:
            smooth_tr = smooth_tr - smooth_tr / period + tr[i]
            smooth_plus = smooth_plus - smooth_plus / period + plus_dm[i]
            smooth_minus = smooth_minus - smooth_minus / period + minus_dm[i]

        if smooth_tr <= 0:
            continue
        pdi = 100.0 * smooth_plus / smooth_tr
        mdi = 100.0 * smooth_minus / smooth_tr
        plus_di[i] = pdi
        minus_di[i] = mdi

        denominator = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / denominator if denominator > 0 else 0.0
        dx_values.append(dx)

        if len(dx_values) == period:
            adx_out[i] = float(np.mean(dx_values))
        elif len(dx_values) > period and not np.isnan(adx_out[i - 1]):
            adx_out[i] = (adx_out[i - 1] * (period - 1) + dx) / period

    return adx_out, plus_di, minus_di


def bollinger(
    close: np.ndarray, period: int = 20, std_mult: float = 2.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """``(upper, middle, lower, bandwidth)``."""
    n = len(close)
    middle = sma(close, period)
    upper, lower, bandwidth = _empty(n), _empty(n), _empty(n)
    if n < period:
        return upper, middle, lower, bandwidth
    for i in range(period - 1, n):
        deviation = float(np.std(close[i - period + 1 : i + 1]))
        upper[i] = middle[i] + std_mult * deviation
        lower[i] = middle[i] - std_mult * deviation
        if middle[i] != 0:
            bandwidth[i] = (upper[i] - lower[i]) / middle[i]
    return upper, middle, lower, bandwidth


def keltner(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 20,
    atr_period: int = 10,
    atr_mult: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(upper, middle, lower)`` — EMA centre with ATR-scaled channels."""
    middle = ema(close, period)
    atr_values = atr(high, low, close, atr_period)
    return middle + atr_mult * atr_values, middle, middle - atr_mult * atr_values


def donchian(
    high: np.ndarray, low: np.ndarray, period: int = 20
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(upper, middle, lower)``.

    The channel at index ``i`` covers bars ``[i-period+1 … i]`` **inclusive**.
    Breakout strategies must therefore compare against the *previous* bar's
    channel, which they do — otherwise the breakout bar would define its own
    level and every bar would look like a breakout.
    """
    n = len(high)
    upper, lower, middle = _empty(n), _empty(n), _empty(n)
    if n < period:
        return upper, middle, lower
    for i in range(period - 1, n):
        window_high = float(np.max(high[i - period + 1 : i + 1]))
        window_low = float(np.min(low[i - period + 1 : i + 1]))
        upper[i], lower[i] = window_high, window_low
        middle[i] = (window_high + window_low) / 2.0
    return upper, middle, lower


def roc(close: np.ndarray, period: int = 10) -> np.ndarray:
    """Rate of change, as a fraction."""
    n = len(close)
    out = _empty(n)
    if n <= period:
        return out
    previous = close[:-period]
    with np.errstate(divide="ignore", invalid="ignore"):
        out[period:] = np.where(previous != 0, (close[period:] - previous) / previous, np.nan)
    return out


def vwap_rolling(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray, period: int = 48
) -> np.ndarray:
    """Rolling VWAP over ``period`` bars.

    A rolling window rather than a session anchor: crypto has no session close,
    so an "anchored" VWAP would need an arbitrary daily boundary.
    """
    n = len(close)
    out = _empty(n)
    if n < period:
        return out
    typical = (high + low + close) / 3.0
    pv = typical * volume
    cum_pv = np.cumsum(np.insert(pv, 0, 0.0))
    cum_vol = np.cumsum(np.insert(volume, 0, 0.0))
    window_pv = cum_pv[period:] - cum_pv[:-period]
    window_vol = cum_vol[period:] - cum_vol[:-period]
    with np.errstate(divide="ignore", invalid="ignore"):
        out[period - 1 :] = np.where(window_vol > 0, window_pv / window_vol, np.nan)
    return out


def realized_volatility(close: np.ndarray, period: int = 48, annualize: bool = False) -> np.ndarray:
    """Standard deviation of log returns over a rolling window."""
    n = len(close)
    out = _empty(n)
    if n < period + 1:
        return out
    with np.errstate(divide="ignore", invalid="ignore"):
        log_returns = np.diff(np.log(np.where(close > 0, close, np.nan)))
    for i in range(period, n):
        window = log_returns[i - period : i]
        finite = window[np.isfinite(window)]
        if finite.size >= 2:
            value = float(np.std(finite, ddof=1))
            out[i] = value * np.sqrt(365 * 24) if annualize else value
    return out


def zscore(values: np.ndarray, period: int = 20) -> np.ndarray:
    """Rolling z-score of a series."""
    n = len(values)
    out = _empty(n)
    if n < period:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        finite = window[np.isfinite(window)]
        if finite.size < 2:
            continue
        deviation = float(np.std(finite, ddof=1))
        if deviation > 0:
            out[i] = (values[i] - float(np.mean(finite))) / deviation
        else:
            out[i] = 0.0
    return out


def rolling_percentile(values: np.ndarray, period: int = 100) -> np.ndarray:
    """Percentile rank (0-100) of each value within its trailing window."""
    n = len(values)
    out = _empty(n)
    if n < period:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        finite = window[np.isfinite(window)]
        if finite.size < 2 or not np.isfinite(values[i]):
            continue
        out[i] = 100.0 * float(np.sum(finite <= values[i])) / finite.size
    return out


def swing_highs(high: np.ndarray, left: int = 2, right: int = 2) -> np.ndarray:
    """Boolean mask of confirmed swing highs.

    A swing high is only marked once ``right`` bars have formed after it, so the
    mask at index ``i`` reflects a pivot at ``i - right``. Strategies must
    account for that lag — which is real, not a modelling artefact.
    """
    n = len(high)
    mask = np.zeros(n, dtype=bool)
    for i in range(left, n - right):
        window_left = high[i - left : i]
        window_right = high[i + 1 : i + right + 1]
        if high[i] > np.max(window_left) and high[i] >= np.max(window_right):
            mask[i] = True
    return mask


def swing_lows(low: np.ndarray, left: int = 2, right: int = 2) -> np.ndarray:
    n = len(low)
    mask = np.zeros(n, dtype=bool)
    for i in range(left, n - right):
        window_left = low[i - left : i]
        window_right = low[i + 1 : i + right + 1]
        if low[i] < np.min(window_left) and low[i] <= np.min(window_right):
            mask[i] = True
    return mask


def supertrend(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 10, multiplier: float = 3.0
) -> tuple[np.ndarray, np.ndarray]:
    """``(supertrend_line, direction)`` where direction is +1 up / -1 down."""
    n = len(close)
    line = _empty(n)
    direction = np.zeros(n, dtype=float)
    atr_values = atr(high, low, close, period)
    if n < period + 2:
        return line, direction

    hl2 = (high + low) / 2.0
    upper_band = hl2 + multiplier * atr_values
    lower_band = hl2 - multiplier * atr_values

    final_upper = _empty(n)
    final_lower = _empty(n)
    start = period + 1
    final_upper[start - 1] = upper_band[start - 1]
    final_lower[start - 1] = lower_band[start - 1]
    direction[start - 1] = 1.0

    for i in range(start, n):
        if np.isnan(upper_band[i]) or np.isnan(final_upper[i - 1]):
            continue
        final_upper[i] = (
            upper_band[i]
            if upper_band[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]
            else final_upper[i - 1]
        )
        final_lower[i] = (
            lower_band[i]
            if lower_band[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]
            else final_lower[i - 1]
        )
        if close[i] > final_upper[i]:
            direction[i] = 1.0
        elif close[i] < final_lower[i]:
            direction[i] = -1.0
        else:
            direction[i] = direction[i - 1] if direction[i - 1] != 0 else 1.0
        line[i] = final_lower[i] if direction[i] > 0 else final_upper[i]

    return line, direction


def ichimoku(
    high: np.ndarray,
    low: np.ndarray,
    conversion: int = 9,
    base: int = 26,
    span_b: int = 52,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """``(tenkan, kijun, senkou_a, senkou_b)``.

    Senkou spans are returned **un-shifted**, i.e. aligned to the bar that
    produced them. Displacing them forward would place future-derived values on
    past bars, which is exactly the leak this system forbids. Strategies compare
    price against the current cloud values.
    """
    n = len(high)

    def midpoint(period: int) -> np.ndarray:
        out = _empty(n)
        if n < period:
            return out
        for i in range(period - 1, n):
            out[i] = (
                float(np.max(high[i - period + 1 : i + 1]))
                + float(np.min(low[i - period + 1 : i + 1]))
            ) / 2.0
        return out

    tenkan = midpoint(conversion)
    kijun = midpoint(base)
    return tenkan, kijun, (tenkan + kijun) / 2.0, midpoint(span_b)


def linreg_slope(values: np.ndarray, period: int = 20) -> np.ndarray:
    """Least-squares slope over a rolling window, normalised by price level.

    Normalising makes the slope comparable across price regimes — a $50/bar
    trend means something very different at $10k than at $100k.
    """
    n = len(values)
    out = _empty(n)
    if n < period:
        return out
    x = np.arange(period, dtype=float)
    x_centered = x - x.mean()
    denominator = float(np.sum(x_centered**2))
    if denominator == 0:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        if not np.all(np.isfinite(window)):
            continue
        slope = float(np.sum(x_centered * (window - window.mean()))) / denominator
        level = float(np.mean(window))
        out[i] = slope / level if level != 0 else 0.0
    return out


def volume_zscore(volume: np.ndarray, period: int = 50) -> np.ndarray:
    """Z-score of volume — the basis of the abnormal-volume strategies."""
    return zscore(volume, period)


def stochastic(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14, smooth: int = 3
) -> tuple[np.ndarray, np.ndarray]:
    """``(%K, %D)``."""
    n = len(close)
    k = _empty(n)
    if n < period:
        return k, _empty(n)
    for i in range(period - 1, n):
        window_high = float(np.max(high[i - period + 1 : i + 1]))
        window_low = float(np.min(low[i - period + 1 : i + 1]))
        span = window_high - window_low
        k[i] = 50.0 if span == 0 else 100.0 * (close[i] - window_low) / span
    valid = ~np.isnan(k)
    d = _empty(n)
    if valid.any():
        start = int(np.argmax(valid))
        d[start:] = sma(k[start:], smooth)
    return k, d
