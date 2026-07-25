"""Look-ahead prevention and candle-completion tests.

These are the tests that matter most for whether any backtest number can be
trusted. Three independent properties are asserted:

1. **Indicator causality** — truncating the input must not change earlier values.
2. **Candle completion** — an unclosed candle is never visible to strategy logic.
3. **Replay bounding** — reading past the cursor raises rather than returning data.
"""

from __future__ import annotations

import numpy as np
import pytest

from btcbot.exchange.models import Candle
from btcbot.features.engine import FeatureEngine
from btcbot.features.indicators import (
    adx,
    atr,
    bollinger,
    donchian,
    ema,
    ichimoku,
    linreg_slope,
    macd,
    realized_volatility,
    roc,
    rsi,
    sma,
    supertrend,
    vwap_rolling,
    zscore,
)
from btcbot.market_data.candles import (
    CandleSeries,
    ReplayCursor,
    ReplayView,
    assert_closed,
    build_closed_only,
)
from btcbot.utils.errors import LookAheadError
from btcbot.utils.timeutil import interval_ms, is_candle_closed, now_ms
from tests.conftest import make_candles


class TestIndicatorCausality:
    """An indicator at index i must depend only on data up to i."""

    @pytest.fixture
    def prices(self) -> np.ndarray:
        rng = np.random.default_rng(42)
        return np.cumsum(rng.normal(0, 50, 500)) + 50_000.0

    @pytest.mark.parametrize(
        "fn",
        [
            lambda x: sma(x, 20),
            lambda x: ema(x, 21),
            lambda x: rsi(x, 14),
            lambda x: roc(x, 10),
            lambda x: zscore(x, 20),
            lambda x: linreg_slope(x, 20),
            lambda x: realized_volatility(x, 48),
            lambda x: macd(x)[0],
            lambda x: macd(x)[2],
            lambda x: bollinger(x, 20)[0],
        ],
    )
    def test_truncation_does_not_change_history(self, prices, fn):
        """Compute on the full series, then on a prefix; the overlap must match."""
        cut = 300
        full = fn(prices)
        partial = fn(prices[:cut])

        overlap_full = full[:cut]
        finite = np.isfinite(overlap_full) & np.isfinite(partial)
        assert finite.sum() > 50, "not enough comparable values to be meaningful"
        np.testing.assert_allclose(
            overlap_full[finite],
            partial[finite],
            rtol=1e-9,
            atol=1e-9,
            err_msg="indicator changed retroactively — it is reading the future",
        )

    @pytest.mark.parametrize(
        "fn",
        [
            lambda h, lo, c: atr(h, lo, c, 14),
            lambda h, lo, c: adx(h, lo, c, 14)[0],
            lambda h, lo, c: donchian(h, lo, 20)[0],
            lambda h, lo, c: supertrend(h, lo, c, 10, 3.0)[1],
            lambda h, lo, c: ichimoku(h, lo)[0],
        ],
    )
    def test_ohlc_indicators_are_causal(self, prices, fn):
        high = prices + 30.0
        low = prices - 30.0
        cut = 300

        full = fn(high, low, prices)
        partial = fn(high[:cut], low[:cut], prices[:cut])

        overlap = full[:cut]
        finite = np.isfinite(overlap) & np.isfinite(partial)
        assert finite.sum() > 30
        np.testing.assert_allclose(
            overlap[finite], partial[finite], rtol=1e-8, atol=1e-8,
            err_msg="OHLC indicator changed retroactively",
        )

    def test_vwap_is_causal(self, prices):
        volume = np.full(len(prices), 100.0)
        cut = 300
        full = vwap_rolling(prices + 10, prices - 10, prices, volume, 48)
        partial = vwap_rolling(
            prices[:cut] + 10, prices[:cut] - 10, prices[:cut], volume[:cut], 48
        )
        finite = np.isfinite(full[:cut]) & np.isfinite(partial)
        np.testing.assert_allclose(full[:cut][finite], partial[finite], rtol=1e-9)

    def test_indicators_pad_with_nan_not_zero(self, prices):
        """Zero-padding would let a strategy act on a half-formed indicator."""
        values = ema(prices, 50)
        assert np.all(np.isnan(values[:49])), "warm-up region must be NaN, not 0.0"
        assert np.isfinite(values[49])


class TestCandleCompletion:
    def test_unclosed_candle_is_not_closed(self):
        current_bar = (now_ms() // interval_ms("5")) * interval_ms("5")
        candle = Candle(
            open_ms=current_bar, open=1.0, high=2.0, low=0.5, close=1.5,
            volume=1.0, turnover=1.0, timeframe="5", confirmed=False,
        )
        assert not candle.is_closed()

    def test_past_candle_is_closed(self):
        past = now_ms() - 10 * interval_ms("5")
        candle = Candle(
            open_ms=past, open=1.0, high=2.0, low=0.5, close=1.5,
            volume=1.0, turnover=1.0, timeframe="5", confirmed=False,
        )
        assert candle.is_closed()

    def test_websocket_confirm_flag_marks_closure(self):
        current_bar = (now_ms() // interval_ms("5")) * interval_ms("5")
        candle = Candle(
            open_ms=current_bar, open=1.0, high=2.0, low=0.5, close=1.5,
            volume=1.0, turnover=1.0, timeframe="5", confirmed=True,
        )
        assert candle.is_closed()

    def test_is_candle_closed_boundary(self):
        step = interval_ms("5")
        open_ms = 1_000_000 * step
        assert not is_candle_closed(open_ms, "5", now=open_ms)
        assert not is_candle_closed(open_ms, "5", now=open_ms + step - 1)
        assert is_candle_closed(open_ms, "5", now=open_ms + step)

    def test_series_closed_excludes_the_forming_candle(self):
        series = CandleSeries("BTCUSDT", "5")
        step = interval_ms("5")
        base = (now_ms() // step) * step
        for offset in range(5, 0, -1):
            series.upsert(
                Candle(
                    open_ms=base - offset * step, open=1.0, high=1.0, low=1.0, close=1.0,
                    volume=1.0, turnover=1.0, timeframe="5", confirmed=False,
                )
            )
        series.upsert(
            Candle(
                open_ms=base, open=1.0, high=1.0, low=1.0, close=1.0,
                volume=1.0, turnover=1.0, timeframe="5", confirmed=False,
            )
        )
        assert len(series) == 6
        assert len(series.closed()) == 5
        assert series.forming() is not None
        assert series.forming().open_ms == base

    def test_build_closed_only_filters_rest_response(self):
        """Bybit's newest kline element may be the still-forming candle."""
        step = interval_ms("5")
        base = (now_ms() // step) * step
        candles = [
            Candle(open_ms=base - 2 * step, open=1, high=1, low=1, close=1,
                   volume=1, turnover=1, timeframe="5"),
            Candle(open_ms=base - step, open=1, high=1, low=1, close=1,
                   volume=1, turnover=1, timeframe="5"),
            Candle(open_ms=base, open=1, high=1, low=1, close=1,
                   volume=1, turnover=1, timeframe="5"),
        ]
        closed = build_closed_only(candles)
        assert len(closed) == 2
        assert all(c.open_ms < base for c in closed)

    def test_assert_closed_raises_on_open_candle(self):
        step = interval_ms("5")
        base = (now_ms() // step) * step
        candle = Candle(open_ms=base, open=1, high=1, low=1, close=1,
                        volume=1, turnover=1, timeframe="5")
        with pytest.raises(LookAheadError):
            assert_closed(candle)

    def test_missing_ranges_detects_gaps(self):
        series = CandleSeries("BTCUSDT", "5")
        step = interval_ms("5")
        base = now_ms() - 100 * step
        for index in (0, 1, 2, 6, 7):
            series.upsert(
                Candle(open_ms=base + index * step, open=1, high=1, low=1, close=1,
                       volume=1, turnover=1, timeframe="5", confirmed=True)
            )
        gaps = series.missing_ranges()
        assert len(gaps) == 1
        assert gaps[0] == (base + 3 * step, base + 5 * step)

    def test_upsert_deduplicates_by_open_time(self):
        series = CandleSeries("BTCUSDT", "5")
        step = interval_ms("5")
        base = now_ms() - 10 * step
        first = Candle(open_ms=base, open=1, high=1, low=1, close=1,
                       volume=1, turnover=1, timeframe="5", confirmed=True)
        updated = Candle(open_ms=base, open=1, high=5, low=1, close=4,
                         volume=9, turnover=1, timeframe="5", confirmed=True)
        assert series.upsert(first) is True
        assert series.upsert(updated) is False, "same bar must update, not duplicate"
        assert len(series) == 1
        assert series.get(base).close == 4


class TestReplayCursor:
    @pytest.fixture
    def candles(self) -> list[Candle]:
        return make_candles([100.0 + i for i in range(50)], timeframe="5")

    def test_cursor_starts_before_the_first_bar(self, candles):
        cursor = ReplayCursor("BTCUSDT", "5", candles)
        assert cursor.index == -1
        assert cursor.current() is None
        assert cursor.history() == []

    def test_history_never_includes_the_future(self, candles):
        cursor = ReplayCursor("BTCUSDT", "5", candles)
        for expected_index in range(10):
            cursor.advance()
            history = cursor.history()
            assert len(history) == expected_index + 1
            assert history[-1].open_ms == candles[expected_index].open_ms
            assert all(c.open_ms <= candles[expected_index].open_ms for c in history)

    def test_negative_peek_raises_lookahead(self, candles):
        cursor = ReplayCursor("BTCUSDT", "5", candles)
        for _ in range(5):
            cursor.advance()
        with pytest.raises(LookAheadError):
            cursor.peek(-1)

    def test_peek_looks_backwards_correctly(self, candles):
        cursor = ReplayCursor("BTCUSDT", "5", candles)
        for _ in range(5):
            cursor.advance()
        assert cursor.peek(0).open_ms == candles[4].open_ms
        assert cursor.peek(4).open_ms == candles[0].open_ms

    def test_replay_view_exposes_no_future_access(self, candles):
        """The view handed to strategies must have no forward-reading method."""
        cursor = ReplayCursor("BTCUSDT", "5", candles)
        cursor.advance()
        view = ReplayView(cursor)
        assert not hasattr(view, "next_bar")
        assert not hasattr(view, "advance")
        with pytest.raises(LookAheadError):
            view.peek(-1)

    def test_next_bar_is_only_on_the_cursor(self, candles):
        """The simulator needs next_bar for fills; strategies must not have it."""
        cursor = ReplayCursor("BTCUSDT", "5", candles)
        cursor.advance()
        assert cursor.next_bar() is not None
        assert cursor.next_bar().open_ms == candles[1].open_ms

    def test_slice_until_excludes_incomplete_bars(self, candles):
        series = CandleSeries("BTCUSDT", "5", max_size=100)
        series.extend(candles)
        boundary = candles[10].open_ms
        window = series.slice_until(boundary)
        assert all(c.open_ms < boundary for c in window)


class TestFeatureEngineIsolation:
    def test_features_built_from_a_prefix_match_the_full_series(self):
        """The backtest path and the live path must agree bar-for-bar."""
        engine = FeatureEngine()
        candles = make_candles(
            [50_000.0 + i * 12.0 for i in range(400)], timeframe="5", high_pad=15, low_pad=15
        )

        prefix = engine.compute_from_candles("BTCUSDT", "5", candles[:300])
        assert prefix is not None

        engine_full = FeatureEngine()
        full = engine_full.compute_from_candles("BTCUSDT", "5", candles)
        assert full is not None

        # The prefix's last bar is candles[299]; find it in the full arrays.
        assert prefix.bar_open_ms == candles[299].open_ms
        full_index = next(
            i for i, c in enumerate(full.candles) if c.open_ms == candles[299].open_ms
        )
        for name in ("ema21", "rsi14", "atr14", "adx14", "macd_hist"):
            prefix_value = prefix.last(name)
            full_value = float(full.series(name)[full_index])
            if np.isfinite(prefix_value) and np.isfinite(full_value):
                assert abs(prefix_value - full_value) < 1e-6, (
                    f"{name} differs between prefix and full computation"
                )

    def test_engine_returns_none_below_minimum_bars(self):
        engine = FeatureEngine()
        assert engine.compute_from_candles("BTCUSDT", "5", make_candles([1.0] * 50)) is None

    def test_cache_invalidates_on_new_bar(self):
        engine = FeatureEngine()
        series = CandleSeries("BTCUSDT", "5", max_size=1000)
        candles = make_candles([50_000.0 + i for i in range(300)], timeframe="5")
        series.extend(candles)

        first = engine.compute(series)
        assert first is not None
        again = engine.compute(series)
        assert again is first, "same bar must reuse the cached FeatureSet"

        step = interval_ms("5")
        series.upsert(
            Candle(
                open_ms=candles[-1].open_ms + step, open=1, high=1, low=1, close=50_500.0,
                volume=1, turnover=1, timeframe="5", confirmed=True,
            )
        )
        updated = engine.compute(series)
        assert updated is not first, "a new closed bar must invalidate the cache"
