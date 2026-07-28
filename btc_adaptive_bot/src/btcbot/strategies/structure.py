"""Market-structure and liquidity strategies.

Shared hypothesis family: price leaves an objective, measurable footprint —
swing pivots, broken levels, sweeps of obvious stop clusters, unfilled gaps —
and those footprints carry information.

Every pattern here is defined arithmetically from OHLC data. There is no
discretionary "it looks like" logic anywhere: a swing is a confirmed pivot, a
break of structure is a close beyond a specific prior pivot, and a fair-value
gap is a numeric inequality between three specific candles. That is what makes
them reproducible and testable.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from .breakout import find_sr_levels


@dataclass(frozen=True, slots=True)
class SwingPoint:
    index: int          # index into the feature arrays
    price: float
    is_high: bool


def extract_swings(features: FeatureSet, *, lookback: int = 150) -> list[SwingPoint]:
    """Confirmed swing pivots in chronological order.

    Note the inherent lag: a pivot is only *confirmed* once the bars after it
    have formed. That lag is real and is preserved here rather than hidden,
    because a backtest that assumed instant confirmation would be looking ahead.
    """
    highs, lows = features.series("high"), features.series("low")
    high_mask = features.series("swing_high")
    low_mask = features.series("swing_low")
    if highs.size == 0 or high_mask.size != highs.size:
        return []

    window = min(lookback, highs.size)
    offset = highs.size - window
    points: list[SwingPoint] = []
    for i in range(window):
        if high_mask[offset + i] > 0:
            points.append(SwingPoint(offset + i, float(highs[offset + i]), True))
        if low_mask[offset + i] > 0:
            points.append(SwingPoint(offset + i, float(lows[offset + i]), False))
    points.sort(key=lambda p: p.index)
    return points


def last_pivots(swings: list[SwingPoint], *, is_high: bool, count: int = 2) -> list[SwingPoint]:
    return [p for p in swings if p.is_high == is_high][-count:]


class SwingStructureTrend(Strategy):
    """25. Swing structure trend — trade with higher highs / higher lows."""

    id = "swing_structure_15m"
    name = "Swing Structure Trend"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "A sequence of higher highs and higher lows is the definition of an "
        "uptrend; entering on the confirmation of a new higher low trades with it."
    )
    primary_timeframe = "15"
    default_rr = 2.5
    atr_stop_mult = 1.5
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.STRUCTURE_STOP, ExitMechanism.TRAILING_STOP,
         ExitMechanism.BREAK_EVEN}
    )
    preferred_regimes = frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.STRONG_TREND_UP, Regime.STRONG_TREND_DOWN,
         Regime.BREAKOUT}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"rr_target": 2.5, "atr_stop_mult": 1.5, "trail_atr_mult": 2.5,
                "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"rr_target": [2.0, 2.5, 3.0, 3.5], "atr_stop_mult": [1.2, 1.5, 2.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        swings = extract_swings(features)
        highs = last_pivots(swings, is_high=True, count=2)
        lows = last_pivots(swings, is_high=False, count=2)
        if len(highs) < 2 or len(lows) < 2:
            return None

        atr_value = features.last("atr14")
        if not np.isfinite(atr_value) or atr_value <= 0:
            return None

        higher_high = highs[-1].price > highs[-2].price
        higher_low = lows[-1].price > lows[-2].price
        lower_high = highs[-1].price < highs[-2].price
        lower_low = lows[-1].price < lows[-2].price
        close = features.close

        if higher_high and higher_low and close > lows[-1].price:
            direction, pivot = Direction.LONG, lows[-1]
        elif lower_high and lower_low and close < highs[-1].price:
            direction, pivot = Direction.SHORT, highs[-1]
        else:
            return None

        # Only act shortly after the structural pivot confirmed, so we do not
        # re-enter the same structure indefinitely.
        bars_since = len(features.candles) - 1 - pivot.index
        if bars_since > 6 or bars_since < 1:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"swingstruct_{int(pivot.price)}_{direction.value}",
            rationale=(
                f"{'Higher high and higher low' if direction is Direction.LONG else 'Lower high and lower low'} "
                f"confirmed; last pivot {pivot.price:,.2f} ({bars_since} bars ago)."
            ),
            raw_confidence=0.55,
            stop_hint=float(pivot.price - atr_value * 0.3) if direction is Direction.LONG
            else float(pivot.price + atr_value * 0.3),
        )


class BreakOfStructure(Strategy):
    """26. Break of structure — a decisive close beyond the last opposing pivot."""

    id = "break_of_structure_15m"
    name = "Break of Structure"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "Closing beyond the most recent swing pivot invalidates the prior "
        "structure and typically starts a new leg in the breaking direction."
    )
    primary_timeframe = "15"
    default_rr = 2.2
    atr_stop_mult = 1.5
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.STRUCTURE_STOP, ExitMechanism.BREAK_EVEN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"min_break_atr": 0.2, "rr_target": 2.2, "atr_stop_mult": 1.5,
                "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"min_break_atr": [0.1, 0.2, 0.3, 0.5], "rr_target": [1.8, 2.2, 2.8]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        swings = extract_swings(features)
        highs = last_pivots(swings, is_high=True, count=1)
        lows = last_pivots(swings, is_high=False, count=1)
        atr_value = features.last("atr14")
        if not highs or not lows or not np.isfinite(atr_value) or atr_value <= 0:
            return None

        threshold = atr_value * float(self.param("min_break_atr"))
        close = features.close
        previous = features.candle(1)
        if previous is None:
            return None

        if close > highs[-1].price + threshold and previous.close <= highs[-1].price:
            direction, level, opposing = Direction.LONG, highs[-1].price, lows[-1].price
        elif close < lows[-1].price - threshold and previous.close >= lows[-1].price:
            direction, level, opposing = Direction.SHORT, lows[-1].price, highs[-1].price
        else:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"bos_{int(level)}_{direction.value}",
            rationale=f"Broke structure through the swing at {level:,.2f} "
                      f"by {(abs(close - level) / atr_value):.2f} ATR.",
            raw_confidence=0.55,
            stop_hint=float(opposing),
        )


class ChangeOfCharacter(Strategy):
    """27. Change of character — the *first* structural break against a trend.

    Distinct from break-of-structure: this fires only when the break goes
    *against* the established direction, which is the earliest objective sign
    that a trend may be ending rather than continuing.
    """

    id = "change_of_character_5m"
    name = "Change of Character"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "The first break of structure opposing an established trend marks a "
        "possible regime change and offers an early, tightly-defined entry."
    )
    primary_timeframe = "5"
    context_timeframes = ("60",)
    default_rr = 2.5
    atr_stop_mult = 1.2
    min_confidence = 0.4
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.STRUCTURE_STOP, ExitMechanism.PARTIAL_EXIT,
         ExitMechanism.TIME_STOP}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"trend_bars": 30, "rr_target": 2.5, "atr_stop_mult": 1.2,
                "partial_at_r": 1.0, "partial_fraction": 0.5, "time_stop_bars": 60}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"trend_bars": [20, 30, 45, 60], "rr_target": [2.0, 2.5, 3.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        swings = extract_swings(features, lookback=int(self.param("trend_bars")) * 2)
        highs = last_pivots(swings, is_high=True, count=3)
        lows = last_pivots(swings, is_high=False, count=3)
        atr_value = features.last("atr14")
        if len(highs) < 2 or len(lows) < 2 or not np.isfinite(atr_value) or atr_value <= 0:
            return None

        close = features.close
        previous = features.candle(1)
        if previous is None:
            return None

        was_uptrend = highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price
        was_downtrend = highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price

        # CHoCH long: prior downtrend, now first close above the last lower high.
        if was_downtrend and close > highs[-1].price and previous.close <= highs[-1].price:
            direction, level, stop_level = Direction.LONG, highs[-1].price, lows[-1].price
        elif was_uptrend and close < lows[-1].price and previous.close >= lows[-1].price:
            direction, level, stop_level = Direction.SHORT, lows[-1].price, highs[-1].price
        else:
            return None

        # Counter-trend against the 1H is what this strategy is *for*, but it
        # deserves lower confidence when it fights a strong higher timeframe.
        htf = trend_alignment(ctx.tf("60"), direction)
        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"choch_{int(level)}_{direction.value}",
            rationale=f"First structural break against the prior "
                      f"{'downtrend' if direction is Direction.LONG else 'uptrend'} "
                      f"at {level:,.2f}.",
            raw_confidence=0.52 + (0.06 if htf > 0 else -0.06),
            stop_hint=float(stop_level),
        )


class LiquiditySweepReclaim(Strategy):
    """28. Liquidity sweep and reclaim — a stop-run that immediately fails."""

    id = "liquidity_sweep_5m"
    name = "Liquidity Sweep + Reclaim"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "Obvious swing highs/lows hold clustered stop orders. Price that spikes "
        "through such a level and closes back on the original side has absorbed "
        "that liquidity and often reverses sharply."
    )
    primary_timeframe = "5"
    default_rr = 2.5
    atr_stop_mult = 1.0
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.STRUCTURE_STOP, ExitMechanism.PARTIAL_EXIT,
         ExitMechanism.BREAK_EVEN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"sweep_lookback": 40, "min_wick_ratio": 0.5, "rr_target": 2.5,
                "atr_stop_mult": 1.0, "partial_at_r": 1.0, "partial_fraction": 0.5,
                "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"sweep_lookback": [20, 30, 40, 60], "min_wick_ratio": [0.4, 0.5, 0.6, 0.7],
                "rr_target": [2.0, 2.5, 3.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        swings = extract_swings(features, lookback=int(self.param("sweep_lookback")))
        atr_value = features.last("atr14")
        candle = features.candle(0)
        if candle is None or not np.isfinite(atr_value) or atr_value <= 0:
            return None

        bar_range = candle.high - candle.low
        if bar_range <= 0:
            return None
        min_wick = float(self.param("min_wick_ratio"))

        highs = last_pivots(swings, is_high=True, count=3)
        lows = last_pivots(swings, is_high=False, count=3)

        # Bullish sweep: wicked below a prior swing low, closed back above it.
        for pivot in reversed(lows):
            if candle.low < pivot.price <= candle.close:
                lower_wick = (min(candle.open, candle.close) - candle.low) / bar_range
                if lower_wick >= min_wick:
                    return SetupProposal(
                        direction=Direction.LONG,
                        entry_reference=candle.close,
                        setup_key=f"sweep_low_{int(pivot.price)}",
                        rationale=f"Swept the swing low {pivot.price:,.2f} and reclaimed it "
                                  f"with a {lower_wick * 100:.0f}% lower wick.",
                        raw_confidence=0.58 + clamp((lower_wick - min_wick) * 0.3, 0.0, 0.12),
                        stop_hint=float(candle.low - atr_value * 0.15),
                    )

        for pivot in reversed(highs):
            if candle.high > pivot.price >= candle.close:
                upper_wick = (candle.high - max(candle.open, candle.close)) / bar_range
                if upper_wick >= min_wick:
                    return SetupProposal(
                        direction=Direction.SHORT,
                        entry_reference=candle.close,
                        setup_key=f"sweep_high_{int(pivot.price)}",
                        rationale=f"Swept the swing high {pivot.price:,.2f} and rejected it "
                                  f"with a {upper_wick * 100:.0f}% upper wick.",
                        raw_confidence=0.58 + clamp((upper_wick - min_wick) * 0.3, 0.0, 0.12),
                        stop_hint=float(candle.high + atr_value * 0.15),
                    )
        return None


class SupportResistanceRejection(Strategy):
    """29. Rejection from a clustered horizontal level (the level holds)."""

    id = "sr_rejection_15m"
    name = "Support/Resistance Rejection"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "The mirror of the breakout hypothesis: a level with multiple touches "
        "more often holds than breaks, so trading the rejection has a higher "
        "hit rate at the cost of a smaller move."
    )
    primary_timeframe = "15"
    default_rr = 1.8
    atr_stop_mult = 1.0
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.STRUCTURE_STOP, ExitMechanism.TIME_STOP}
    )
    preferred_regimes = frozenset(
        {Regime.RANGING, Regime.LOW_VOLATILITY, Regime.VOLATILITY_CONTRACTION, Regime.UNCERTAIN,
         Regime.HIGH_VOLATILITY}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"lookback": 120, "min_touches": 3, "cluster_atr": 0.5, "rr_target": 1.8,
                "atr_stop_mult": 1.0, "time_stop_bars": 32}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"min_touches": [2, 3, 4, 5], "cluster_atr": [0.3, 0.5, 0.7],
                "rr_target": [1.5, 1.8, 2.2]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        atr_value = features.last("atr14")
        candle = features.candle(0)
        if candle is None or not np.isfinite(atr_value) or atr_value <= 0:
            return None

        levels = find_sr_levels(
            features,
            lookback=int(self.param("lookback")),
            cluster_distance=atr_value * float(self.param("cluster_atr")),
            min_touches=int(self.param("min_touches")),
        )
        if not levels:
            return None

        tolerance = atr_value * 0.4
        for level, touches in levels:
            # Touched from below and rejected → short.
            if candle.high >= level - tolerance and candle.close < level and candle.close < candle.open:
                return SetupProposal(
                    direction=Direction.SHORT,
                    entry_reference=candle.close,
                    setup_key=f"srreject_{int(level)}_short",
                    rationale=f"Rejected resistance at {level:,.2f} ({touches} touches).",
                    raw_confidence=0.5 + clamp(touches * 0.03, 0.0, 0.15),
                    stop_hint=float(max(candle.high, level) + tolerance * 0.5),
                )
            if candle.low <= level + tolerance and candle.close > level and candle.close > candle.open:
                return SetupProposal(
                    direction=Direction.LONG,
                    entry_reference=candle.close,
                    setup_key=f"srreject_{int(level)}_long",
                    rationale=f"Held support at {level:,.2f} ({touches} touches).",
                    raw_confidence=0.5 + clamp(touches * 0.03, 0.0, 0.15),
                    stop_hint=float(min(candle.low, level) - tolerance * 0.5),
                )
        return None


class StructureBreakoutRetest(Strategy):
    """30. Structural break followed by a pivot retest on the 1H."""

    id = "structure_retest_1h"
    name = "Structure Breakout + Retest"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "A broken swing pivot flips polarity: former resistance becomes support. "
        "Entering on that flip gives a defined invalidation and a large target."
    )
    primary_timeframe = "60"
    default_rr = 3.0
    atr_stop_mult = 1.2
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.STRUCTURE_STOP, ExitMechanism.TRAILING_STOP,
         ExitMechanism.BREAK_EVEN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"max_bars_since_break": 10, "retest_atr": 0.5, "rr_target": 3.0,
                "atr_stop_mult": 1.2, "trail_atr_mult": 2.5, "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"max_bars_since_break": [5, 8, 10, 15], "retest_atr": [0.3, 0.5, 0.8],
                "rr_target": [2.5, 3.0, 3.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        swings = extract_swings(features)
        atr_value = features.last("atr14")
        candle = features.candle(0)
        if candle is None or not np.isfinite(atr_value) or atr_value <= 0 or not swings:
            return None

        band = atr_value * float(self.param("retest_atr"))
        max_age = int(self.param("max_bars_since_break"))
        closes = features.series("close")
        current_index = closes.size - 1

        for pivot in reversed(swings):
            age = current_index - pivot.index
            if age < 2 or age > max_age + 10:
                continue

            # Did price close beyond this pivot at some point after it formed?
            after = closes[pivot.index + 1 : current_index]
            if after.size == 0:
                continue

            if pivot.is_high and np.any(after > pivot.price):
                broke_at = int(np.argmax(after > pivot.price))
                if current_index - (pivot.index + 1 + broke_at) > max_age:
                    continue
                if candle.low <= pivot.price + band and candle.close > pivot.price:
                    return SetupProposal(
                        direction=Direction.LONG,
                        entry_reference=candle.close,
                        setup_key=f"structretest_{int(pivot.price)}_long",
                        rationale=f"Broken swing high {pivot.price:,.2f} retested as support.",
                        raw_confidence=0.6,
                        stop_hint=float(min(candle.low, pivot.price) - band),
                    )
            if (not pivot.is_high) and np.any(after < pivot.price):
                broke_at = int(np.argmax(after < pivot.price))
                if current_index - (pivot.index + 1 + broke_at) > max_age:
                    continue
                if candle.high >= pivot.price - band and candle.close < pivot.price:
                    return SetupProposal(
                        direction=Direction.SHORT,
                        entry_reference=candle.close,
                        setup_key=f"structretest_{int(pivot.price)}_short",
                        rationale=f"Broken swing low {pivot.price:,.2f} retested as resistance.",
                        raw_confidence=0.6,
                        stop_hint=float(max(candle.high, pivot.price) + band),
                    )
        return None


class FairValueGapRetracement(Strategy):
    """31. Imbalance (fair-value gap) retracement.

    Objective definition — a three-candle pattern where the middle candle moves
    so fast that candle 1 and candle 3 do not overlap:

    * bullish gap: ``high[i-2] < low[i]`` → unfilled zone ``[high[i-2], low[i]]``
    * bearish gap: ``low[i-2] > high[i]`` → unfilled zone ``[high[i], low[i-2]]``

    The trade is a retracement *into* that zone in the direction of the impulse
    that created it. No interpretation is involved; it is an inequality.
    """

    id = "fvg_retracement_15m"
    name = "Fair-Value Gap Retracement"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "A price range crossed so quickly that adjacent candles do not overlap "
        "represents unfilled interest; price frequently returns to it before "
        "continuing in the impulse direction."
    )
    primary_timeframe = "15"
    default_rr = 2.5
    atr_stop_mult = 1.2
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.STRUCTURE_STOP, ExitMechanism.BREAK_EVEN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"max_gap_age": 20, "min_gap_atr": 0.3, "rr_target": 2.5,
                "atr_stop_mult": 1.2, "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"max_gap_age": [10, 15, 20, 30], "min_gap_atr": [0.2, 0.3, 0.5, 0.8],
                "rr_target": [2.0, 2.5, 3.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        atr_value = features.last("atr14")
        candles = features.candles
        candle = features.candle(0)
        if candle is None or not np.isfinite(atr_value) or atr_value <= 0 or len(candles) < 6:
            return None

        min_gap = atr_value * float(self.param("min_gap_atr"))
        max_age = int(self.param("max_gap_age"))
        newest = len(candles) - 1

        # Scan recent history for an unfilled gap, newest first.
        for i in range(newest - 1, max(1, newest - max_age), -1):
            if i < 2:
                break
            first, third = candles[i - 2], candles[i]

            # Bullish gap
            if third.low > first.high and (third.low - first.high) >= min_gap:
                gap_low, gap_high = first.high, third.low
                # Unfilled: no later candle may have traded fully through it.
                later = candles[i + 1 : newest]
                if any(c.low <= gap_low for c in later):
                    continue
                if candle.low <= gap_high and candle.close > gap_low:
                    return SetupProposal(
                        direction=Direction.LONG,
                        entry_reference=candle.close,
                        setup_key=f"fvg_{int(gap_low)}_{int(gap_high)}_long",
                        rationale=f"Retraced into an unfilled bullish gap "
                                  f"{gap_low:,.2f}–{gap_high:,.2f}.",
                        raw_confidence=0.55,
                        stop_hint=float(gap_low - atr_value * 0.3),
                    )

            # Bearish gap
            if first.low > third.high and (first.low - third.high) >= min_gap:
                gap_low, gap_high = third.high, first.low
                later = candles[i + 1 : newest]
                if any(c.high >= gap_high for c in later):
                    continue
                if candle.high >= gap_low and candle.close < gap_high:
                    return SetupProposal(
                        direction=Direction.SHORT,
                        entry_reference=candle.close,
                        setup_key=f"fvg_{int(gap_low)}_{int(gap_high)}_short",
                        rationale=f"Retraced into an unfilled bearish gap "
                                  f"{gap_low:,.2f}–{gap_high:,.2f}.",
                        raw_confidence=0.55,
                        stop_hint=float(gap_high + atr_value * 0.3),
                    )
        return None


# --------------------------------------------------------------------------
#  Session levels — previous-day and previous-week high/low
#
#  These are the most-watched liquidity references in the market, and they are
#  computed strictly from *completed* periods. The current, still-forming day or
#  week is excluded: including it would let a level move as the session
#  progresses, which is exactly the repainting the brief forbids.
# --------------------------------------------------------------------------

_MS_PER_DAY = 86_400_000
_MS_PER_WEEK = 7 * _MS_PER_DAY


@dataclass(frozen=True, slots=True)
class SessionLevels:
    """High/low of the last *completed* UTC day and week."""

    prev_day_high: float | None = None
    prev_day_low: float | None = None
    prev_week_high: float | None = None
    prev_week_low: float | None = None

    def levels(self) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for label, value in (
            ("previous-day high", self.prev_day_high),
            ("previous-day low", self.prev_day_low),
            ("previous-week high", self.prev_week_high),
            ("previous-week low", self.prev_week_low),
        ):
            if value is not None:
                out.append((label, value))
        return out


def _period_extremes(
    features: FeatureSet, *, period_ms: int, epoch_offset_ms: int = 0
) -> tuple[float | None, float | None]:
    """High/low of the most recently *completed* period of ``period_ms``."""
    candles = features.candles
    if not candles:
        return (None, None)
    current_bucket = (candles[-1].open_ms - epoch_offset_ms) // period_ms
    target_bucket = current_bucket - 1
    highs = [
        c.high for c in candles if (c.open_ms - epoch_offset_ms) // period_ms == target_bucket
    ]
    lows = [
        c.low for c in candles if (c.open_ms - epoch_offset_ms) // period_ms == target_bucket
    ]
    if not highs or not lows:
        return (None, None)
    return (max(highs), min(lows))


def session_levels(features: FeatureSet) -> SessionLevels:
    """Previous completed UTC day and week extremes from a candle series."""
    day_high, day_low = _period_extremes(features, period_ms=_MS_PER_DAY)
    # Unix epoch was a Thursday; offset by 4 days so weeks break on Monday 00:00 UTC.
    week_high, week_low = _period_extremes(
        features, period_ms=_MS_PER_WEEK, epoch_offset_ms=4 * _MS_PER_DAY
    )
    return SessionLevels(day_high, day_low, week_high, week_low)


def premium_discount(features: FeatureSet, *, lookback: int = 100) -> float | None:
    """Where price sits inside the recent dealing range: 0 = low, 1 = high.

    Above 0.5 is "premium" (expensive), below 0.5 is "discount" (cheap) — the
    standard objective definition, computed from completed bars only.
    """
    highs, lows = features.series("high"), features.series("low")
    if highs.size < 10:
        return None
    window = min(lookback, highs.size)
    range_high = float(np.max(highs[-window:]))
    range_low = float(np.min(lows[-window:]))
    span = range_high - range_low
    if span <= 0:
        return None
    return float((features.close - range_low) / span)


class MultiTimeframeSmcContinuation(Strategy):
    """29. Multi-timeframe SMC continuation.

    The full stack the brief describes: 4H sets bias, price must be in the
    favourable half of the dealing range (discount for longs, premium for
    shorts), the entry timeframe must show a break of structure in the bias
    direction, and the entry bar must show displacement.
    """

    id = "mtf_smc_continuation_15m"
    name = "Multi-Timeframe SMC Continuation"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "Continuation entries are best taken in the direction of the higher "
        "timeframe, from the favourable half of the dealing range, after the "
        "entry timeframe confirms with a structural break and displacement."
    )
    primary_timeframe = "15"
    context_timeframes = ("60", "240")
    default_rr = 2.5
    atr_stop_mult = 1.5
    exit_mechanisms = frozenset(
        {ExitMechanism.STRUCTURE_STOP, ExitMechanism.STRUCTURE_TARGET,
         ExitMechanism.FIXED_RR, ExitMechanism.BREAK_EVEN, ExitMechanism.PARTIAL_EXIT}
    )
    preferred_regimes = frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.STRONG_TREND_UP,
         Regime.STRONG_TREND_DOWN, Regime.BREAKOUT}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"max_premium_for_long": 0.5, "min_premium_for_short": 0.5,
                "displacement_atr": 0.8, "range_lookback": 100, "rr_target": 2.5,
                "atr_stop_mult": 1.5, "break_even_at_r": 1.0, "partial_at_r": 1.5,
                "partial_fraction": 0.5}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"displacement_atr": [0.6, 0.8, 1.2], "range_lookback": [60, 100, 150],
                "rr_target": [2.0, 2.5, 3.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        htf = ctx.tf("240")
        if htf is None:
            return None
        if trend_alignment(htf, Direction.LONG) > 0:
            direction = Direction.LONG
        elif trend_alignment(htf, Direction.SHORT) > 0:
            direction = Direction.SHORT
        else:
            return None

        position = premium_discount(features, lookback=int(self.param("range_lookback")))
        if position is None:
            return None
        # Longs only from discount, shorts only from premium.
        if direction is Direction.LONG and position > float(self.param("max_premium_for_long")):
            return None
        if direction is Direction.SHORT and position < float(self.param("min_premium_for_short")):
            return None

        atr = features.last("atr14")
        close = features.close
        if not np.isfinite(atr) or atr <= 0:
            return None

        # Structural break on the entry timeframe, from confirmed pivots only.
        swings = extract_swings(features)
        highs = last_pivots(swings, is_high=True, count=1)
        lows = last_pivots(swings, is_high=False, count=1)
        if not highs or not lows:
            return None
        if direction is Direction.LONG and close <= highs[-1].price:
            return None
        if direction is Direction.SHORT and close >= lows[-1].price:
            return None

        # Displacement: the entry bar must be a decisive move, not a drift.
        body = abs(close - features.open)
        if body < atr * float(self.param("displacement_atr")):
            return None

        confidence = 0.62 + clamp(body / atr * 0.08, 0.0, 0.14)
        stop_hint = lows[-1].price if direction is Direction.LONG else highs[-1].price

        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"mtfsmc_{int(features.bar_open_ms)}_{direction.value}",
            rationale=(
                f"4H bias {direction.value}; price in "
                f"{'discount' if position < 0.5 else 'premium'} ({position:.2f}); "
                f"15m BOS with {body / atr:.2f} ATR displacement."
            ),
            raw_confidence=confidence,
            stop_hint=float(stop_hint),
        )


class OrderBlockMitigation(Strategy):
    """33. Order-block mitigation.

    An order block is defined arithmetically: the last opposite-direction candle
    immediately preceding a displacement move that breaks structure. The trade
    is taken when price returns to that candle's range and rejects it.
    """

    id = "order_block_mitigation_15m"
    name = "Order-Block Mitigation"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "The last opposite candle before a displacement leg marks where the "
        "move originated; price returning there often resumes the original move."
    )
    primary_timeframe = "15"
    context_timeframes = ("60",)
    default_rr = 2.5
    atr_stop_mult = 1.0
    exit_mechanisms = frozenset(
        {ExitMechanism.STRUCTURE_STOP, ExitMechanism.FIXED_RR,
         ExitMechanism.STRUCTURE_TARGET, ExitMechanism.BREAK_EVEN}
    )
    preferred_regimes = frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.STRONG_TREND_UP,
         Regime.STRONG_TREND_DOWN, Regime.RANGING}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"lookback": 40, "displacement_atr": 1.2, "max_age_bars": 25,
                "rr_target": 2.5, "atr_stop_mult": 1.0, "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"displacement_atr": [0.8, 1.2, 1.8], "max_age_bars": [15, 25, 40],
                "rr_target": [2.0, 2.5, 3.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        atr = features.last("atr14")
        close = features.close
        if not np.isfinite(atr) or atr <= 0:
            return None

        candles = features.candles
        lookback = min(int(self.param("lookback")), len(candles) - 2)
        if lookback < 5:
            return None
        max_age = int(self.param("max_age_bars"))
        displacement = atr * float(self.param("displacement_atr"))

        # Scan backwards for the most recent displacement leg and the opposite
        # candle that preceded it. Everything examined is a completed bar.
        for offset in range(2, lookback):
            index = len(candles) - offset
            leg = candles[index]
            leg_body = leg.close - leg.open
            if abs(leg_body) < displacement:
                continue
            origin = candles[index - 1]
            bullish_leg = leg_body > 0
            # The order block is the last *opposite* candle before the leg.
            if bullish_leg and origin.close >= origin.open:
                continue
            if not bullish_leg and origin.close <= origin.open:
                continue
            age = len(candles) - 1 - (index - 1)
            if age > max_age:
                break

            block_high, block_low = origin.high, origin.low
            if bullish_leg:
                # Long: price returned into the block and closed back above it.
                if features.low <= block_high and close > block_low:
                    direction = Direction.LONG
                    stop_hint = block_low - atr * float(self.param("atr_stop_mult"))
                else:
                    continue
            else:
                if features.high >= block_low and close < block_high:
                    direction = Direction.SHORT
                    stop_hint = block_high + atr * float(self.param("atr_stop_mult"))
                else:
                    continue

            htf = trend_alignment(ctx.tf("60"), direction)
            confidence = 0.58 + clamp(abs(leg_body) / atr * 0.05, 0.0, 0.12)
            confidence += 0.06 if htf > 0 else -0.05
            return SetupProposal(
                direction=direction,
                entry_reference=close,
                setup_key=f"ob_{int(block_low)}_{int(block_high)}_{direction.value}",
                rationale=(
                    f"Price mitigated the order block {block_low:,.2f}–{block_high:,.2f} "
                    f"({age} bars old) that preceded a {abs(leg_body) / atr:.1f} ATR "
                    f"{'bullish' if bullish_leg else 'bearish'} displacement."
                ),
                raw_confidence=confidence,
                stop_hint=float(stop_hint),
            )
        return None


class BreakerBlock(Strategy):
    """35. Breaker block — a failed order block that flips polarity.

    When an order block fails (price closes decisively through it), that same
    zone frequently acts as support/resistance in the opposite direction. The
    definition here is strict: a specific block, a specific failing close, and a
    specific retest from the other side.
    """

    id = "breaker_block_15m"
    name = "Breaker Block"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "A demand zone that fails becomes supply (and vice versa); the retest of "
        "a broken block from the other side is a defined entry."
    )
    primary_timeframe = "15"
    context_timeframes = ("60",)
    default_rr = 2.2
    atr_stop_mult = 1.0
    exit_mechanisms = frozenset(
        {ExitMechanism.STRUCTURE_STOP, ExitMechanism.FIXED_RR, ExitMechanism.BREAK_EVEN}
    )
    preferred_regimes = frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.STRONG_TREND_UP,
         Regime.STRONG_TREND_DOWN, Regime.BREAKOUT, Regime.HIGH_VOLATILITY}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"lookback": 50, "break_atr": 0.5, "max_age_bars": 30,
                "rr_target": 2.2, "atr_stop_mult": 1.0, "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"break_atr": [0.3, 0.5, 0.8], "max_age_bars": [20, 30, 45]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        atr = features.last("atr14")
        close = features.close
        if not np.isfinite(atr) or atr <= 0:
            return None

        swings = extract_swings(features)
        candles = features.candles
        if len(swings) < 2 or len(candles) < 10:
            return None

        break_distance = atr * float(self.param("break_atr"))
        max_age = int(self.param("max_age_bars"))

        # A prior swing low that price closed decisively below becomes a breaker
        # for shorts; a prior swing high closed decisively above becomes support.
        for pivot in reversed(swings):
            age = len(candles) - 1 - pivot.index
            if age > max_age:
                break
            if age < 2:
                continue

            after = candles[pivot.index + 1 :]
            if not after:
                continue
            broke_below = any(c.close < pivot.price - break_distance for c in after)
            broke_above = any(c.close > pivot.price + break_distance for c in after)

            if not pivot.is_high and broke_below:
                # Broken demand → now supply. Retest from below, rejected.
                if features.high >= pivot.price and close < pivot.price:
                    return SetupProposal(
                        direction=Direction.SHORT,
                        entry_reference=close,
                        setup_key=f"breaker_{int(pivot.price)}_short",
                        rationale=(
                            f"Swing low {pivot.price:,.2f} was broken and retested "
                            "from below — demand flipped to supply."
                        ),
                        raw_confidence=0.58,
                        stop_hint=float(pivot.price + atr * float(self.param("atr_stop_mult"))),
                    )
            if pivot.is_high and broke_above:
                # Broken supply → now demand. Retest from above, held.
                if features.low <= pivot.price and close > pivot.price:
                    return SetupProposal(
                        direction=Direction.LONG,
                        entry_reference=close,
                        setup_key=f"breaker_{int(pivot.price)}_long",
                        rationale=(
                            f"Swing high {pivot.price:,.2f} was broken and retested "
                            "from above — supply flipped to demand."
                        ),
                        raw_confidence=0.58,
                        stop_hint=float(pivot.price - atr * float(self.param("atr_stop_mult"))),
                    )
        return None


class _SessionLevelSweep(Strategy):
    """Shared implementation for previous-day and previous-week sweep entries.

    The pattern is identical; only the reference level and timeframe differ, so
    the logic lives once and the two concrete strategies below differ in which
    levels they consider. They remain genuinely distinct hypotheses: daily
    liquidity and weekly liquidity behave differently.
    """

    level_kinds: tuple[str, ...] = ()

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"sweep_atr": 0.15, "rr_target": 2.5, "atr_stop_mult": 0.8,
                "break_even_at_r": 1.0, "time_stop_bars": 30}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"sweep_atr": [0.05, 0.15, 0.3], "rr_target": [2.0, 2.5, 3.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        atr = features.last("atr14")
        close = features.close
        if not np.isfinite(atr) or atr <= 0:
            return None

        levels = session_levels(features)
        margin = atr * float(self.param("sweep_atr"))
        stop_pad = atr * float(self.param("atr_stop_mult"))

        for label, level in levels.levels():
            if not any(kind in label for kind in self.level_kinds):
                continue
            is_high_level = "high" in label

            if is_high_level:
                # Swept above the level, then closed back below it → short.
                if features.high > level + margin and close < level:
                    return SetupProposal(
                        direction=Direction.SHORT,
                        entry_reference=close,
                        setup_key=f"sweep_{label.replace(' ', '_')}_{int(level)}_short",
                        rationale=(
                            f"Swept the {label} {level:,.2f} by "
                            f"{(features.high - level) / atr:.2f} ATR and closed back below — "
                            "buy-side liquidity taken."
                        ),
                        raw_confidence=0.6,
                        stop_hint=float(features.high + stop_pad),
                    )
            else:
                if features.low < level - margin and close > level:
                    return SetupProposal(
                        direction=Direction.LONG,
                        entry_reference=close,
                        setup_key=f"sweep_{label.replace(' ', '_')}_{int(level)}_long",
                        rationale=(
                            f"Swept the {label} {level:,.2f} by "
                            f"{(level - features.low) / atr:.2f} ATR and closed back above — "
                            "sell-side liquidity taken."
                        ),
                        raw_confidence=0.6,
                        stop_hint=float(features.low - stop_pad),
                    )
        return None


class PreviousDayLevelSweep(_SessionLevelSweep):
    """36. Previous-day high/low sweep and reclaim."""

    id = "prev_day_sweep_15m"
    name = "Previous-Day High/Low Sweep"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "Stops cluster beyond the previous day's high and low; a sweep that "
        "closes back inside signals the liquidity grab is complete."
    )
    primary_timeframe = "15"
    context_timeframes = ("60",)
    level_kinds = ("previous-day",)
    default_rr = 2.5
    atr_stop_mult = 0.8
    exit_mechanisms = frozenset(
        {ExitMechanism.STRUCTURE_STOP, ExitMechanism.FIXED_RR,
         ExitMechanism.BREAK_EVEN, ExitMechanism.TIME_STOP}
    )
    preferred_regimes = frozenset(
        {Regime.RANGING, Regime.HIGH_VOLATILITY, Regime.VOLATILITY_EXPANSION,
         Regime.UNCERTAIN, Regime.BREAKOUT}
    )


class WeeklyLevelSweep(_SessionLevelSweep):
    """37. Weekly high/low sweep and reclaim."""

    id = "weekly_level_sweep_1h"
    name = "Weekly High/Low Sweep"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "Weekly extremes hold the market's largest resting stop clusters; a "
        "sweep and reclaim of one is a higher-conviction reversal reference "
        "than a daily sweep."
    )
    primary_timeframe = "60"
    context_timeframes = ("240",)
    level_kinds = ("previous-week",)
    default_rr = 3.0
    atr_stop_mult = 0.8
    exit_mechanisms = frozenset(
        {ExitMechanism.STRUCTURE_STOP, ExitMechanism.FIXED_RR,
         ExitMechanism.BREAK_EVEN, ExitMechanism.PARTIAL_EXIT}
    )
    preferred_regimes = frozenset(
        {Regime.RANGING, Regime.HIGH_VOLATILITY, Regime.VOLATILITY_EXPANSION,
         Regime.UNCERTAIN, Regime.BREAKOUT}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"sweep_atr": 0.15, "rr_target": 3.0, "atr_stop_mult": 0.8,
                "break_even_at_r": 1.0, "partial_at_r": 1.5, "partial_fraction": 0.5}


class PremiumDiscountContinuation(Strategy):
    """38. Premium/discount continuation.

    Pure location logic: in an established trend, only continue from the
    favourable half of the dealing range. Buying an uptrend at a premium is the
    single most common way continuation entries produce poor risk/reward.
    """

    id = "premium_discount_continuation_1h"
    name = "Premium/Discount Continuation"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "Trend continuation entries taken from the favourable half of the "
        "dealing range give materially better risk/reward than the same entry "
        "taken from the unfavourable half."
    )
    primary_timeframe = "60"
    context_timeframes = ("240",)
    default_rr = 2.5
    atr_stop_mult = 1.5
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.STRUCTURE_TARGET,
         ExitMechanism.TRAILING_STOP, ExitMechanism.BREAK_EVEN}
    )
    preferred_regimes = frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.STRONG_TREND_UP, Regime.STRONG_TREND_DOWN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"range_lookback": 120, "discount_max": 0.4, "premium_min": 0.6,
                "rr_target": 2.5, "atr_stop_mult": 1.5, "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"range_lookback": [80, 120, 180], "discount_max": [0.3, 0.4, 0.5],
                "premium_min": [0.5, 0.6, 0.7]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        htf = ctx.tf("240")
        if htf is None:
            return None
        if trend_alignment(htf, Direction.LONG) > 0:
            direction = Direction.LONG
        elif trend_alignment(htf, Direction.SHORT) > 0:
            direction = Direction.SHORT
        else:
            return None

        position = premium_discount(features, lookback=int(self.param("range_lookback")))
        atr = features.last("atr14")
        close = features.close
        if position is None or not np.isfinite(atr) or atr <= 0:
            return None

        if direction is Direction.LONG:
            if position > float(self.param("discount_max")):
                return None
            # Confirmation: the discount must be being defended, not sliced.
            if close <= features.open:
                return None
            edge = float(self.param("discount_max")) - position
        else:
            if position < float(self.param("premium_min")):
                return None
            if close >= features.open:
                return None
            edge = position - float(self.param("premium_min"))

        confidence = 0.58 + clamp(edge * 0.4, 0.0, 0.14)
        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"premdisc_{int(features.bar_open_ms)}_{direction.value}",
            rationale=(
                f"4H trend {direction.value} with price at range position "
                f"{position:.2f} "
                f"({'discount' if direction is Direction.LONG else 'premium'}), "
                "and the bar closed in the trend direction."
            ),
            raw_confidence=confidence,
        )


class MultiConfirmationReversal(Strategy):
    """39. Multi-confirmation reversal — several independent signals must agree.

    Reversal trading is where single-signal systems lose the most, so this one
    requires a stack: a sweep of a confirmed pivot, a change of character, a
    displacement close, and a momentum extreme. Fewer trades, higher bar.
    """

    id = "multi_confirmation_reversal_15m"
    name = "Multi-Confirmation Reversal"
    version = "1.0"
    category = StrategyCategory.STRUCTURE
    hypothesis = (
        "Reversals are only worth taking when several independent conditions "
        "agree at once; any single reversal signal alone is unreliable."
    )
    primary_timeframe = "15"
    context_timeframes = ("60",)
    default_rr = 3.0
    atr_stop_mult = 1.0
    # Countertrend by construction — held to a higher confidence bar.
    min_confidence = 0.6
    exit_mechanisms = frozenset(
        {ExitMechanism.STRUCTURE_STOP, ExitMechanism.FIXED_RR, ExitMechanism.PARTIAL_EXIT,
         ExitMechanism.BREAK_EVEN, ExitMechanism.TIME_STOP}
    )
    preferred_regimes = frozenset(
        {Regime.RANGING, Regime.HIGH_VOLATILITY, Regime.VOLATILITY_EXPANSION,
         Regime.UNCERTAIN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"sweep_atr": 0.1, "displacement_atr": 0.7, "rsi_extreme": 28.0,
                "rr_target": 3.0, "atr_stop_mult": 1.0, "break_even_at_r": 1.0,
                "partial_at_r": 1.5, "partial_fraction": 0.5, "time_stop_bars": 32}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"displacement_atr": [0.5, 0.7, 1.0], "rsi_extreme": [22.0, 28.0, 34.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        atr = features.last("atr14")
        rsi = features.last("rsi14")
        close = features.close
        if not all(np.isfinite(v) for v in (atr, rsi, close)) or atr <= 0:
            return None

        swings = extract_swings(features)
        highs = last_pivots(swings, is_high=True, count=1)
        lows = last_pivots(swings, is_high=False, count=1)
        if not highs or not lows:
            return None

        margin = atr * float(self.param("sweep_atr"))
        displacement = atr * float(self.param("displacement_atr"))
        rsi_extreme = float(self.param("rsi_extreme"))
        body = close - features.open
        confirmations: list[str] = []

        # Bullish reversal stack.
        swept_low = features.low < lows[-1].price - margin and close > lows[-1].price
        if swept_low:
            confirmations.append("swept a confirmed swing low and reclaimed it")
            if body > displacement:
                confirmations.append(f"bullish displacement {body / atr:.2f} ATR")
            if rsi <= rsi_extreme:
                confirmations.append(f"RSI {rsi:.0f} oversold")
            if close > features.prev("close"):
                confirmations.append("closed above the prior bar")
            if len(confirmations) >= 3:
                return SetupProposal(
                    direction=Direction.LONG,
                    entry_reference=close,
                    setup_key=f"multirev_{int(features.bar_open_ms)}_long",
                    rationale="Reversal confirmed by: " + "; ".join(confirmations) + ".",
                    raw_confidence=0.6 + 0.05 * (len(confirmations) - 3),
                    stop_hint=float(features.low - atr * float(self.param("atr_stop_mult"))),
                )

        # Bearish reversal stack.
        confirmations = []
        swept_high = features.high > highs[-1].price + margin and close < highs[-1].price
        if swept_high:
            confirmations.append("swept a confirmed swing high and rejected it")
            if body < -displacement:
                confirmations.append(f"bearish displacement {abs(body) / atr:.2f} ATR")
            if rsi >= 100.0 - rsi_extreme:
                confirmations.append(f"RSI {rsi:.0f} overbought")
            if close < features.prev("close"):
                confirmations.append("closed below the prior bar")
            if len(confirmations) >= 3:
                return SetupProposal(
                    direction=Direction.SHORT,
                    entry_reference=close,
                    setup_key=f"multirev_{int(features.bar_open_ms)}_short",
                    rationale="Reversal confirmed by: " + "; ".join(confirmations) + ".",
                    raw_confidence=0.6 + 0.05 * (len(confirmations) - 3),
                    stop_hint=float(features.high + atr * float(self.param("atr_stop_mult"))),
                )
        return None


STRUCTURE_STRATEGIES: tuple[type[Strategy], ...] = (
    SwingStructureTrend,
    BreakOfStructure,
    ChangeOfCharacter,
    LiquiditySweepReclaim,
    SupportResistanceRejection,
    StructureBreakoutRetest,
    FairValueGapRetracement,
    MultiTimeframeSmcContinuation,
    OrderBlockMitigation,
    BreakerBlock,
    PreviousDayLevelSweep,
    WeeklyLevelSweep,
    PremiumDiscountContinuation,
    MultiConfirmationReversal,
)
