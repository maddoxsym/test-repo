"""Trend-following strategies.

Shared hypothesis family: price that has been moving in one direction tends to
keep moving that way. Each strategy operationalises "trend" differently — moving
average relationships, directional strength, volatility-band trailing, cloud
structure, pullback timing — so their failure modes differ even when their
direction agrees.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..features.engine import FeatureSet
from ..regime.classifier import Regime
from ..utils.numeric import clamp
from .base import (
    Direction,
    ExitMechanism,
    SetupProposal,
    Strategy,
    StrategyCategory,
    StrategyContext,
    trend_alignment,
)


class EmaTrendCross(Strategy):
    """1. EMA crossover — the classic trend entry."""

    id = "ema_trend_cross_15m"
    name = "EMA Trend Crossover"
    version = "1.0"
    category = StrategyCategory.TREND
    hypothesis = (
        "When a fast EMA crosses a slow EMA and price confirms above/below both, "
        "a directional move is more likely to continue than to immediately revert."
    )
    primary_timeframe = "15"
    context_timeframes = ("60",)
    default_rr = 2.0
    atr_stop_mult = 1.8
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TRAILING_STOP,
         ExitMechanism.BREAK_EVEN, ExitMechanism.OPPOSITE_SIGNAL}
    )
    preferred_regimes = frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.STRONG_TREND_UP, Regime.STRONG_TREND_DOWN,
         Regime.BREAKOUT}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"fast": "ema21", "slow": "ema50", "rr_target": 2.0, "atr_stop_mult": 1.8,
                "trail_atr_mult": 2.0, "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"rr_target": [1.5, 2.0, 2.5, 3.0], "atr_stop_mult": [1.2, 1.5, 1.8, 2.2]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        fast_now = features.last(self.param("fast"))
        slow_now = features.last(self.param("slow"))
        fast_prev = features.prev(self.param("fast"))
        slow_prev = features.prev(self.param("slow"))
        if not all(np.isfinite(v) for v in (fast_now, slow_now, fast_prev, slow_prev)):
            return None

        close = features.close
        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now
        if not (crossed_up or crossed_down):
            return None

        direction = Direction.LONG if crossed_up else Direction.SHORT
        # Require the close to confirm the cross; a cross with price on the wrong
        # side is usually noise in a chop.
        if direction is Direction.LONG and close <= fast_now:
            return None
        if direction is Direction.SHORT and close >= fast_now:
            return None

        separation = abs(fast_now - slow_now) / close if close else 0.0
        htf = trend_alignment(ctx.tf("60"), direction)
        confidence = 0.5 + clamp(separation * 60, 0.0, 0.2) + (0.08 if htf > 0 else -0.08)

        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"emacross_{int(features.bar_open_ms)}_{direction.value}",
            rationale=(
                f"EMA fast crossed {'above' if crossed_up else 'below'} slow "
                f"(separation {separation * 100:.2f}%), 1H trend "
                f"{'aligned' if htf > 0 else 'opposed'}."
            ),
            raw_confidence=confidence,
        )


class EmaAdxTrend(Strategy):
    """2. EMA stack gated by ADX — only trade when trend strength is measurable."""

    id = "ema_adx_trend_1h"
    name = "EMA + ADX Trend"
    version = "1.0"
    category = StrategyCategory.TREND
    hypothesis = (
        "A stacked EMA structure is only tradable when ADX confirms genuine "
        "directional strength; without it the same structure appears in chop."
    )
    primary_timeframe = "60"
    default_rr = 2.5
    atr_stop_mult = 2.0
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TRAILING_STOP,
         ExitMechanism.PARTIAL_EXIT}
    )
    preferred_regimes = frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.STRONG_TREND_UP, Regime.STRONG_TREND_DOWN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"adx_min": 25.0, "rr_target": 2.5, "atr_stop_mult": 2.0,
                "partial_at_r": 1.0, "partial_fraction": 0.5, "trail_atr_mult": 2.5}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"adx_min": [20.0, 22.0, 25.0, 28.0, 32.0], "rr_target": [2.0, 2.5, 3.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        adx = features.last("adx14")
        plus_di = features.last("plus_di")
        minus_di = features.last("minus_di")
        ema21 = features.last("ema21")
        ema50 = features.last("ema50")
        ema200 = features.last("ema200")
        if not all(np.isfinite(v) for v in (adx, plus_di, minus_di, ema21, ema50, ema200)):
            return None
        if adx < float(self.param("adx_min")):
            return None

        close = features.close
        bullish_stack = ema21 > ema50 > ema200 and close > ema21 and plus_di > minus_di
        bearish_stack = ema21 < ema50 < ema200 and close < ema21 and minus_di > plus_di
        if not (bullish_stack or bearish_stack):
            return None

        direction = Direction.LONG if bullish_stack else Direction.SHORT
        # Only enter on the bar that *establishes* the stack, so we do not
        # re-enter the same trend on every subsequent bar.
        prev_ema21, prev_ema50 = features.prev("ema21"), features.prev("ema50")
        prev_close = features.candle(1).close if features.candle(1) else close
        if direction is Direction.LONG and prev_close > prev_ema21 and prev_ema21 > prev_ema50:
            return None
        if direction is Direction.SHORT and prev_close < prev_ema21 and prev_ema21 < prev_ema50:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"emaadx_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"EMA stack established with ADX {adx:.1f} and DI spread "
                      f"{abs(plus_di - minus_di):.1f}.",
            raw_confidence=0.5 + clamp((adx - 20) / 100.0, 0.0, 0.25),
        )


class SupertrendFollow(Strategy):
    """3. Supertrend flip — volatility-adaptive trend tracking."""

    id = "supertrend_15m"
    name = "Supertrend Follower"
    version = "1.0"
    category = StrategyCategory.TREND
    hypothesis = (
        "An ATR-scaled trailing band flips direction only after a move exceeds "
        "recent volatility, filtering the small reversals that whipsaw fixed MAs."
    )
    primary_timeframe = "15"
    context_timeframes = ("240",)
    default_rr = 2.5
    exit_mechanisms = frozenset(
        {ExitMechanism.ATR_STOP, ExitMechanism.TRAILING_STOP, ExitMechanism.OPPOSITE_SIGNAL,
         ExitMechanism.FIXED_RR}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"rr_target": 2.5, "trail_atr_mult": 2.0, "require_htf_alignment": True}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"rr_target": [2.0, 2.5, 3.0, 3.5], "trail_atr_mult": [1.5, 2.0, 2.5, 3.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        direction_now = features.last("supertrend_dir")
        direction_prev = features.prev("supertrend_dir")
        line = features.last("supertrend")
        if not all(np.isfinite(v) for v in (direction_now, direction_prev, line)):
            return None
        if direction_now == direction_prev or direction_now == 0:
            return None

        direction = Direction.LONG if direction_now > 0 else Direction.SHORT
        if self.param("require_htf_alignment"):
            if trend_alignment(ctx.tf("240"), direction) < 0:
                return None

        return SetupProposal(
            direction=direction,
            entry_reference=features.close,
            setup_key=f"supertrend_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Supertrend flipped to {direction.value}; band at {line:,.2f}.",
            raw_confidence=0.55,
            # The band itself is the natural stop — that is the whole point of it.
            stop_hint=float(line),
        )


class MacdTrend(Strategy):
    """4. MACD zero-line trend — medium-term momentum regime change."""

    id = "macd_trend_1h"
    name = "MACD Trend"
    version = "1.0"
    category = StrategyCategory.TREND
    hypothesis = (
        "A MACD line crossing zero marks a shift in the medium-term average, "
        "distinct from a signal-line cross which only measures acceleration."
    )
    primary_timeframe = "60"
    default_rr = 2.2
    atr_stop_mult = 2.0
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.OPPOSITE_SIGNAL}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"rr_target": 2.2, "atr_stop_mult": 2.0, "require_signal_agreement": True}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"rr_target": [1.8, 2.2, 2.6, 3.0], "atr_stop_mult": [1.5, 2.0, 2.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        macd_now = features.last("macd")
        macd_prev = features.prev("macd")
        signal_now = features.last("macd_signal")
        if not all(np.isfinite(v) for v in (macd_now, macd_prev, signal_now)):
            return None

        crossed_up = macd_prev <= 0 < macd_now
        crossed_down = macd_prev >= 0 > macd_now
        if not (crossed_up or crossed_down):
            return None

        direction = Direction.LONG if crossed_up else Direction.SHORT
        if self.param("require_signal_agreement"):
            if direction is Direction.LONG and macd_now <= signal_now:
                return None
            if direction is Direction.SHORT and macd_now >= signal_now:
                return None

        return SetupProposal(
            direction=direction,
            entry_reference=features.close,
            setup_key=f"macdzero_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"MACD crossed the zero line {'up' if crossed_up else 'down'} "
                      f"with signal-line agreement.",
            raw_confidence=0.5,
        )


class IchimokuTrend(Strategy):
    """5. Ichimoku cloud — multi-component trend confirmation."""

    id = "ichimoku_trend_4h"
    name = "Ichimoku Trend"
    version = "1.0"
    category = StrategyCategory.TREND
    hypothesis = (
        "Price clearing the cloud with conversion/base alignment represents "
        "agreement across several lookback horizons at once."
    )
    primary_timeframe = "240"
    default_rr = 3.0
    atr_stop_mult = 2.0
    min_bars = 210
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.STRUCTURE_STOP, ExitMechanism.TRAILING_STOP}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"rr_target": 3.0, "trail_atr_mult": 3.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"rr_target": [2.5, 3.0, 3.5, 4.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        tenkan = features.last("tenkan")
        kijun = features.last("kijun")
        span_a = features.last("senkou_a")
        span_b = features.last("senkou_b")
        if not all(np.isfinite(v) for v in (tenkan, kijun, span_a, span_b)):
            return None

        close = features.close
        prev_close = features.candle(1).close if features.candle(1) else close
        cloud_top = max(span_a, span_b)
        cloud_bottom = min(span_a, span_b)

        broke_above = prev_close <= cloud_top < close and tenkan > kijun
        broke_below = prev_close >= cloud_bottom > close and tenkan < kijun
        if not (broke_above or broke_below):
            return None

        direction = Direction.LONG if broke_above else Direction.SHORT
        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"ichimoku_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Price cleared the cloud "
                      f"({'above' if broke_above else 'below'}) with Tenkan/Kijun agreement.",
            raw_confidence=0.55,
            stop_hint=float(cloud_bottom if broke_above else cloud_top),
        )


class MaPullback(Strategy):
    """6. Pullback to a moving average within an established trend."""

    id = "ma_pullback_15m"
    name = "Moving-Average Pullback"
    version = "1.0"
    category = StrategyCategory.TREND
    hypothesis = (
        "In a trend, a retracement into a well-respected moving average offers a "
        "better entry than chasing the extension — same direction, tighter risk."
    )
    primary_timeframe = "15"
    context_timeframes = ("60",)
    default_rr = 2.5
    atr_stop_mult = 1.2
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.BREAK_EVEN,
         ExitMechanism.PARTIAL_EXIT}
    )
    preferred_regimes = frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.STRONG_TREND_UP, Regime.STRONG_TREND_DOWN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"ma": "ema50", "touch_atr": 0.4, "rr_target": 2.5, "atr_stop_mult": 1.2,
                "break_even_at_r": 1.0, "partial_at_r": 1.5, "partial_fraction": 0.5}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"touch_atr": [0.25, 0.4, 0.55, 0.7], "rr_target": [2.0, 2.5, 3.0],
                "atr_stop_mult": [1.0, 1.2, 1.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        ma_value = features.last(self.param("ma"))
        ema200 = features.last("ema200")
        atr_value = features.last("atr14")
        if not all(np.isfinite(v) for v in (ma_value, ema200, atr_value)) or atr_value <= 0:
            return None

        close = features.close
        low = features.low
        high = features.high
        touch_band = atr_value * float(self.param("touch_atr"))

        uptrend = ma_value > ema200 and close > ema200
        downtrend = ma_value < ema200 and close < ema200

        # Long: the bar dipped into the MA band but closed back above it.
        if uptrend and low <= ma_value + touch_band and close > ma_value:
            direction = Direction.LONG
        elif downtrend and high >= ma_value - touch_band and close < ma_value:
            direction = Direction.SHORT
        else:
            return None

        if trend_alignment(ctx.tf("60"), direction) < 0:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"mapullback_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Pullback into {self.param('ma')} at {ma_value:,.2f} held "
                      f"within a confirmed trend.",
            raw_confidence=0.55,
            stop_hint=float(low - touch_band) if direction is Direction.LONG
            else float(high + touch_band),
        )


class DonchianTrendFollow(Strategy):
    """7. Donchian trend-following — stay with the channel's direction."""

    id = "donchian_trend_1h"
    name = "Donchian Trend Following"
    version = "1.0"
    category = StrategyCategory.TREND
    hypothesis = (
        "Classic turtle-style logic: hold direction while price stays on one side "
        "of the channel midline, entering when it reclaims that side after a dip."
    )
    primary_timeframe = "60"
    default_rr = 3.0
    atr_stop_mult = 2.5
    exit_mechanisms = frozenset(
        {ExitMechanism.TRAILING_STOP, ExitMechanism.ATR_STOP, ExitMechanism.FIXED_RR,
         ExitMechanism.TIME_STOP}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"rr_target": 3.0, "atr_stop_mult": 2.5, "trail_atr_mult": 3.0,
                "time_stop_bars": 96}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"rr_target": [2.5, 3.0, 3.5], "atr_stop_mult": [2.0, 2.5, 3.0],
                "time_stop_bars": [48, 96, 144]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        middle = features.last("donchian_middle")
        middle_prev = features.prev("donchian_middle")
        upper = features.last("donchian55_upper")
        lower = features.last("donchian55_lower")
        if not all(np.isfinite(v) for v in (middle, middle_prev, upper, lower)):
            return None

        close = features.close
        prev_candle = features.candle(1)
        if prev_candle is None:
            return None
        prev_close = prev_candle.close

        reclaimed_up = prev_close <= middle_prev and close > middle and middle > middle_prev
        reclaimed_down = prev_close >= middle_prev and close < middle and middle < middle_prev
        if not (reclaimed_up or reclaimed_down):
            return None

        direction = Direction.LONG if reclaimed_up else Direction.SHORT
        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"donchtrend_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Reclaimed the Donchian midline at {middle:,.2f} with the channel "
                      f"sloping {'up' if reclaimed_up else 'down'}.",
            raw_confidence=0.5,
            stop_hint=float(lower if reclaimed_up else upper),
        )


TREND_STRATEGIES: tuple[type[Strategy], ...] = (
    EmaTrendCross,
    EmaAdxTrend,
    SupertrendFollow,
    MacdTrend,
    IchimokuTrend,
    MaPullback,
    DonchianTrendFollow,
)
