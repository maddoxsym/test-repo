"""Feature engine — computes indicators once, shares them with every strategy.

Thirty-eight strategies asking for EMA(21) on the 15-minute series should cause
one calculation, not thirty-eight. :class:`FeatureEngine` caches a
:class:`FeatureSet` per ``(symbol, timeframe, last_closed_bar)`` and invalidates
it only when a new bar closes.

The cache key includes the last closed bar's open time, which is also the
leakage guard: a FeatureSet is a snapshot of a specific instant and cannot
silently pick up newer data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..exchange.models import Candle
from ..market_data.candles import CandleSeries
from ..utils.numeric import safe_div
from . import indicators as ind


@dataclass(frozen=True, slots=True)
class FeatureSet:
    """Indicator arrays for one symbol/timeframe as of one closed bar.

    Every array is aligned to ``candles``; index ``-1`` is the most recent closed
    bar. Scalar convenience properties read that last element.
    """

    symbol: str
    timeframe: str
    bar_open_ms: int
    candles: tuple[Candle, ...]
    arrays: dict[str, np.ndarray] = field(default_factory=dict)

    # --- scalar accessors ------------------------------------------------

    def last(self, name: str, default: float = float("nan")) -> float:
        array = self.arrays.get(name)
        if array is None or array.size == 0:
            return default
        value = float(array[-1])
        return default if not np.isfinite(value) else value

    def prev(self, name: str, offset: int = 1, default: float = float("nan")) -> float:
        array = self.arrays.get(name)
        if array is None or array.size <= offset:
            return default
        value = float(array[-1 - offset])
        return default if not np.isfinite(value) else value

    def series(self, name: str) -> np.ndarray:
        return self.arrays.get(name, np.array([], dtype=float))

    def has(self, *names: str) -> bool:
        """True when every named feature has a finite latest value."""
        return all(np.isfinite(self.last(name)) for name in names)

    @property
    def bar_count(self) -> int:
        return len(self.candles)

    @property
    def close(self) -> float:
        return self.candles[-1].close if self.candles else 0.0

    @property
    def high(self) -> float:
        return self.candles[-1].high if self.candles else 0.0

    @property
    def low(self) -> float:
        return self.candles[-1].low if self.candles else 0.0

    @property
    def open(self) -> float:
        return self.candles[-1].open if self.candles else 0.0

    @property
    def volume(self) -> float:
        return self.candles[-1].volume if self.candles else 0.0

    @property
    def atr(self) -> float:
        return self.last("atr14")

    @property
    def atr_pct(self) -> float:
        return safe_div(self.last("atr14"), self.close)

    def candle(self, offset: int = 0) -> Candle | None:
        """Candle ``offset`` bars back from the most recent closed bar."""
        if offset < 0 or offset >= len(self.candles):
            return None
        return self.candles[-1 - offset]

    def snapshot(self) -> dict[str, Any]:
        """Compact scalar view, journaled with every signal."""
        keys = (
            "ema9", "ema21", "ema50", "ema200", "rsi14", "adx14", "plus_di", "minus_di",
            "atr14", "bb_upper", "bb_lower", "bb_bandwidth", "vwap48", "volume_z",
            "realized_vol", "macd_hist", "donchian_upper", "donchian_lower", "ma_slope",
            "range_percentile", "supertrend_dir",
        )
        payload: dict[str, Any] = {
            "close": self.close,
            "bar_open_ms": self.bar_open_ms,
            "timeframe": self.timeframe,
        }
        for key in keys:
            value = self.last(key)
            if np.isfinite(value):
                payload[key] = round(value, 8)
        return payload


class FeatureEngine:
    """Computes and caches feature sets."""

    # Minimum closed bars before any feature set is produced. Below this the
    # slow indicators are all NaN and strategies would be trading on noise.
    MIN_BARS = 210

    def __init__(self, *, cache_size: int = 32) -> None:
        self._cache: dict[tuple[str, str], FeatureSet] = {}
        self._cache_size = cache_size

    def invalidate(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._cache.clear()
            return
        for key in [k for k in self._cache if k[0] == symbol]:
            del self._cache[key]

    def compute(
        self, series: CandleSeries, *, now_ms_value: int | None = None, limit: int = 600
    ) -> FeatureSet | None:
        """Feature set for the latest closed bar, or ``None`` if not enough data."""
        closed = series.closed(now_ms_value=now_ms_value, limit=limit)
        if len(closed) < self.MIN_BARS:
            return None

        key = (series.symbol, series.timeframe)
        cached = self._cache.get(key)
        last_bar_ms = closed[-1].open_ms
        if cached is not None and cached.bar_open_ms == last_bar_ms:
            return cached

        feature_set = self._build(series.symbol, series.timeframe, closed)
        self._cache[key] = feature_set
        if len(self._cache) > self._cache_size:
            self._cache.pop(next(iter(self._cache)))
        return feature_set

    def compute_from_candles(
        self, symbol: str, timeframe: str, candles: list[Candle]
    ) -> FeatureSet | None:
        """Feature set from an explicit candle list (backtesting path).

        The backtester passes only bars at or before the replay cursor, so this
        entry point inherits the same leak-free guarantee.
        """
        if len(candles) < self.MIN_BARS:
            return None
        return self._build(symbol, timeframe, candles)

    def _build(self, symbol: str, timeframe: str, candles: list[Candle]) -> FeatureSet:
        count = len(candles)
        close = np.fromiter((c.close for c in candles), dtype=float, count=count)
        high = np.fromiter((c.high for c in candles), dtype=float, count=count)
        low = np.fromiter((c.low for c in candles), dtype=float, count=count)
        open_ = np.fromiter((c.open for c in candles), dtype=float, count=count)
        volume = np.fromiter((c.volume for c in candles), dtype=float, count=count)

        macd_line, macd_signal, macd_hist = ind.macd(close)
        adx14, plus_di, minus_di = ind.adx(high, low, close, 14)
        bb_upper, bb_middle, bb_lower, bb_bandwidth = ind.bollinger(close, 20, 2.0)
        kc_upper, kc_middle, kc_lower = ind.keltner(high, low, close, 20, 10, 2.0)
        dc_upper, dc_middle, dc_lower = ind.donchian(high, low, 20)
        dc55_upper, _, dc55_lower = ind.donchian(high, low, 55)
        st_line, st_dir = ind.supertrend(high, low, close, 10, 3.0)
        tenkan, kijun, senkou_a, senkou_b = ind.ichimoku(high, low)
        stoch_k, stoch_d = ind.stochastic(high, low, close)
        atr14 = ind.atr(high, low, close, 14)
        vwap48 = ind.vwap_rolling(high, low, close, volume, 48)

        bar_range = high - low
        with np.errstate(divide="ignore", invalid="ignore"):
            atr_pct = np.where(close > 0, atr14 / close, np.nan)
            vwap_deviation = np.where(vwap48 > 0, (close - vwap48) / vwap48, np.nan)
            body_ratio = np.where(bar_range > 0, np.abs(close - open_) / bar_range, 0.0)

        arrays: dict[str, np.ndarray] = {
            "close": close, "high": high, "low": low, "open": open_, "volume": volume,
            # trend
            "sma20": ind.sma(close, 20),
            "sma50": ind.sma(close, 50),
            "sma200": ind.sma(close, 200),
            "ema9": ind.ema(close, 9),
            "ema21": ind.ema(close, 21),
            "ema50": ind.ema(close, 50),
            "ema100": ind.ema(close, 100),
            "ema200": ind.ema(close, 200),
            "ma_slope": ind.linreg_slope(close, 20),
            "ma_slope_50": ind.linreg_slope(close, 50),
            # momentum
            "rsi14": ind.rsi(close, 14),
            "rsi7": ind.rsi(close, 7),
            "macd": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist,
            "roc10": ind.roc(close, 10),
            "roc20": ind.roc(close, 20),
            "stoch_k": stoch_k, "stoch_d": stoch_d,
            # volatility
            "atr14": atr14, "atr_pct": atr_pct,
            "atr50": ind.atr(high, low, close, 50),
            "realized_vol": ind.realized_volatility(close, 48),
            "realized_vol_long": ind.realized_volatility(close, 192),
            "bb_upper": bb_upper, "bb_middle": bb_middle, "bb_lower": bb_lower,
            "bb_bandwidth": bb_bandwidth,
            "bb_bandwidth_pct": ind.rolling_percentile(bb_bandwidth, 100),
            "kc_upper": kc_upper, "kc_middle": kc_middle, "kc_lower": kc_lower,
            # channels / structure
            "donchian_upper": dc_upper, "donchian_middle": dc_middle, "donchian_lower": dc_lower,
            "donchian55_upper": dc55_upper, "donchian55_lower": dc55_lower,
            "supertrend": st_line, "supertrend_dir": st_dir,
            "tenkan": tenkan, "kijun": kijun, "senkou_a": senkou_a, "senkou_b": senkou_b,
            # directional strength
            "adx14": adx14, "plus_di": plus_di, "minus_di": minus_di,
            # volume
            "volume_sma20": ind.sma(volume, 20),
            "volume_z": ind.volume_zscore(volume, 50),
            "vwap48": vwap48, "vwap_deviation": vwap_deviation,
            "vwap_dev_z": ind.zscore(np.nan_to_num(vwap_deviation, nan=0.0), 50),
            # bar geometry
            "bar_range": bar_range,
            "range_percentile": ind.rolling_percentile(bar_range, 100),
            "body_ratio": body_ratio,
            "close_zscore": ind.zscore(close, 20),
            "close_zscore_50": ind.zscore(close, 50),
        }

        # Swing masks are boolean; stored as float so every array shares a dtype.
        arrays["swing_high"] = ind.swing_highs(high, 2, 2).astype(float)
        arrays["swing_low"] = ind.swing_lows(low, 2, 2).astype(float)

        return FeatureSet(
            symbol=symbol,
            timeframe=timeframe,
            bar_open_ms=candles[-1].open_ms,
            candles=tuple(candles),
            arrays=arrays,
        )


@dataclass(frozen=True, slots=True)
class MultiTimeframeFeatures:
    """Feature sets across timeframes, plus live microstructure.

    Handed to every strategy so multi-timeframe logic (4H regime → 15M structure
    → 5M setup) works without each strategy re-deriving anything.
    """

    symbol: str
    by_timeframe: dict[str, FeatureSet]
    orderbook_imbalance: float = 0.0
    trade_flow_imbalance: float = 0.0
    spread_bps: float = 0.0
    orderbook_valid: bool = False

    def get(self, timeframe: str) -> FeatureSet | None:
        return self.by_timeframe.get(timeframe)

    def require(self, *timeframes: str) -> bool:
        return all(tf in self.by_timeframe for tf in timeframes)

    @property
    def timeframes(self) -> list[str]:
        return list(self.by_timeframe)
