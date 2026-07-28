"""Breakout strategies.

Shared hypothesis family: when price leaves a well-defined containment area,
the move continues rather than immediately reverting. They differ in how the
containment area is defined — channel, prior range, volatility envelope,
squeeze, horizontal level, or a completed daily range.

Every strategy here compares price against a level computed from **prior** bars.
Using the current bar's own extreme would make every bar a breakout.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..features.engine import FeatureSet
from ..regime.classifier import Regime
from ..utils.numeric import clamp
from ..utils.timeutil import interval_ms
from .base import (
    Direction,
    ExitMechanism,
    SetupProposal,
    Strategy,
    StrategyCategory,
    StrategyContext,
    trend_alignment,
)


class DonchianBreakout(Strategy):
    """8. Donchian channel breakout."""

    id = "donchian_breakout_15m"
    name = "Donchian Breakout"
    version = "1.0"
    category = StrategyCategory.BREAKOUT
    hypothesis = (
        "A close beyond the highest high / lowest low of the prior N bars signals "
        "that the balance between buyers and sellers has broken."
    )
    primary_timeframe = "15"
    default_rr = 2.5
    atr_stop_mult = 1.5
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TRAILING_STOP,
         ExitMechanism.BREAK_EVEN}
    )
    preferred_regimes = frozenset(
        {Regime.BREAKOUT, Regime.VOLATILITY_EXPANSION, Regime.TREND_UP, Regime.TREND_DOWN,
         Regime.STRONG_TREND_UP, Regime.STRONG_TREND_DOWN, Regime.HIGH_VOLATILITY}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"channel": "donchian", "min_atr_break": 0.15, "rr_target": 2.5,
                "atr_stop_mult": 1.5, "trail_atr_mult": 2.0, "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"min_atr_break": [0.05, 0.1, 0.15, 0.25, 0.4], "rr_target": [2.0, 2.5, 3.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        # Previous bar's channel — the breakout bar must not define its own level.
        upper = features.prev("donchian_upper")
        lower = features.prev("donchian_lower")
        atr_value = features.last("atr14")
        if not all(np.isfinite(v) for v in (upper, lower, atr_value)) or atr_value <= 0:
            return None

        close = features.close
        threshold = atr_value * float(self.param("min_atr_break"))

        if close > upper + threshold:
            direction, level = Direction.LONG, upper
        elif close < lower - threshold:
            direction, level = Direction.SHORT, lower
        else:
            return None

        strength = abs(close - level) / atr_value
        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"donchbreak_{int(level)}_{direction.value}",
            rationale=f"Closed {strength:.2f} ATR beyond the 20-bar channel at {level:,.2f}.",
            raw_confidence=0.5 + clamp(strength * 0.15, 0.0, 0.2),
            stop_hint=float(level - threshold if direction is Direction.LONG else level + threshold),
        )


class PreviousRangeBreakout(Strategy):
    """9. Breakout of the prior consolidation range."""

    id = "previous_range_breakout_1h"
    name = "Previous Range Breakout"
    version = "1.0"
    category = StrategyCategory.BREAKOUT
    hypothesis = (
        "A range that contained price for many bars becomes significant; leaving "
        "it implies the participants who defended it have stepped away."
    )
    primary_timeframe = "60"
    default_rr = 2.0
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.STRUCTURE_STOP, ExitMechanism.BREAK_EVEN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"range_bars": 24, "max_range_atr": 4.0, "rr_target": 2.0, "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"range_bars": [12, 18, 24, 36, 48], "max_range_atr": [3.0, 4.0, 5.0, 6.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        bars = int(self.param("range_bars"))
        highs, lows = features.series("high"), features.series("low")
        atr_value = features.last("atr14")
        if highs.size < bars + 2 or not np.isfinite(atr_value) or atr_value <= 0:
            return None

        # Exclude the current bar from the range definition.
        window_high = float(np.max(highs[-bars - 1 : -1]))
        window_low = float(np.min(lows[-bars - 1 : -1]))
        range_size = window_high - window_low
        if range_size <= 0:
            return None
        # Only a genuinely *tight* range is meaningful; a wide one is just noise.
        if range_size > atr_value * float(self.param("max_range_atr")):
            return None

        close = features.close
        prev_candle = features.candle(1)
        if prev_candle is None:
            return None

        if close > window_high and prev_candle.close <= window_high:
            direction = Direction.LONG
        elif close < window_low and prev_candle.close >= window_low:
            direction = Direction.SHORT
        else:
            return None

        compression = atr_value * bars / range_size
        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"rangebreak_{int(window_high)}_{int(window_low)}_{direction.value}",
            rationale=f"Broke a {bars}-bar range ({window_low:,.2f}–{window_high:,.2f}, "
                      f"{range_size / atr_value:.1f} ATR wide).",
            raw_confidence=0.5 + clamp(compression * 0.02, 0.0, 0.15),
            stop_hint=float(window_low if direction is Direction.LONG else window_high),
        )


class VolatilityBreakout(Strategy):
    """10. Volatility breakout — an ATR-scaled move from the bar's open."""

    id = "volatility_breakout_5m"
    name = "Volatility Breakout"
    version = "1.0"
    category = StrategyCategory.BREAKOUT
    hypothesis = (
        "A single bar travelling far more than recent average range indicates an "
        "impulse whose direction persists for at least a few more bars."
    )
    primary_timeframe = "5"
    default_rr = 1.8
    atr_stop_mult = 1.2
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TIME_STOP}
    )
    preferred_regimes = frozenset(
        {Regime.VOLATILITY_EXPANSION, Regime.BREAKOUT, Regime.HIGH_VOLATILITY,
         Regime.TREND_UP, Regime.TREND_DOWN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"impulse_atr": 1.3, "min_body_ratio": 0.6, "rr_target": 1.8,
                "atr_stop_mult": 1.2, "time_stop_bars": 12}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"impulse_atr": [1.0, 1.3, 1.6, 2.0], "min_body_ratio": [0.5, 0.6, 0.7],
                "rr_target": [1.5, 1.8, 2.2]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        atr_value = features.last("atr14")
        body_ratio = features.last("body_ratio")
        if not (np.isfinite(atr_value) and np.isfinite(body_ratio)) or atr_value <= 0:
            return None

        candle = features.candle(0)
        if candle is None:
            return None

        move = candle.close - candle.open
        if abs(move) < atr_value * float(self.param("impulse_atr")):
            return None
        if body_ratio < float(self.param("min_body_ratio")):
            return None

        direction = Direction.LONG if move > 0 else Direction.SHORT
        return SetupProposal(
            direction=direction,
            entry_reference=candle.close,
            setup_key=f"volbreak_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Impulse bar of {abs(move) / atr_value:.2f} ATR with "
                      f"{body_ratio * 100:.0f}% body.",
            raw_confidence=0.5 + clamp((abs(move) / atr_value - 1.0) * 0.15, 0.0, 0.2),
            stop_hint=float(candle.low if direction is Direction.LONG else candle.high),
        )


class BollingerSqueezeBreakout(Strategy):
    """11. Bollinger squeeze release — contraction precedes expansion."""

    id = "bollinger_squeeze_15m"
    name = "Bollinger Squeeze Breakout"
    version = "1.0"
    category = StrategyCategory.BREAKOUT
    hypothesis = (
        "Volatility is mean-reverting: an unusually narrow band tends to be "
        "followed by expansion, and the direction of the first decisive close "
        "out of the squeeze is worth following."
    )
    primary_timeframe = "15"
    default_rr = 2.5
    atr_stop_mult = 1.5
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TRAILING_STOP,
         ExitMechanism.VOLATILITY_EXIT}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"squeeze_percentile": 20.0, "rr_target": 2.5, "atr_stop_mult": 1.5,
                "trail_atr_mult": 2.0, "volatility_exit_mult": 3.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"squeeze_percentile": [10.0, 15.0, 20.0, 25.0, 30.0],
                "rr_target": [2.0, 2.5, 3.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        bandwidth_pct_prev = features.prev("bb_bandwidth_pct")
        upper_prev = features.prev("bb_upper")
        lower_prev = features.prev("bb_lower")
        kc_upper = features.prev("kc_upper")
        kc_lower = features.prev("kc_lower")
        if not all(np.isfinite(v) for v in (bandwidth_pct_prev, upper_prev, lower_prev)):
            return None

        # Squeeze = bandwidth in its lowest percentile band. The Keltner
        # containment check is the classic confirmation and is used when available.
        squeezed = bandwidth_pct_prev <= float(self.param("squeeze_percentile"))
        if np.isfinite(kc_upper) and np.isfinite(kc_lower):
            squeezed = squeezed or (upper_prev < kc_upper and lower_prev > kc_lower)
        if not squeezed:
            return None

        close = features.close
        if close > upper_prev:
            direction, level = Direction.LONG, upper_prev
        elif close < lower_prev:
            direction, level = Direction.SHORT, lower_prev
        else:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"squeeze_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Released a squeeze (bandwidth percentile "
                      f"{bandwidth_pct_prev:.0f}) through {level:,.2f}.",
            raw_confidence=0.55 + clamp((20.0 - bandwidth_pct_prev) * 0.005, 0.0, 0.1),
        )


class SupportResistanceBreakout(Strategy):
    """12. Horizontal level breakout — levels earn significance by being retested."""

    id = "sr_breakout_1h"
    name = "Support/Resistance Breakout"
    version = "1.0"
    category = StrategyCategory.BREAKOUT
    hypothesis = (
        "A price level touched repeatedly without breaking accumulates resting "
        "orders; clearing it releases them and produces follow-through."
    )
    primary_timeframe = "60"
    default_rr = 2.2
    atr_stop_mult = 1.5
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.STRUCTURE_STOP, ExitMechanism.BREAK_EVEN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"lookback": 100, "min_touches": 2, "cluster_atr": 0.5, "rr_target": 2.2,
                "atr_stop_mult": 1.5, "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"min_touches": [2, 3, 4], "cluster_atr": [0.3, 0.5, 0.8],
                "lookback": [60, 100, 150]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        atr_value = features.last("atr14")
        if not np.isfinite(atr_value) or atr_value <= 0:
            return None

        levels = find_sr_levels(
            features,
            lookback=int(self.param("lookback")),
            cluster_distance=atr_value * float(self.param("cluster_atr")),
            min_touches=int(self.param("min_touches")),
        )
        if not levels:
            return None

        close = features.close
        prev_candle = features.candle(1)
        if prev_candle is None:
            return None

        for level, touches in levels:
            if prev_candle.close <= level < close:
                return SetupProposal(
                    direction=Direction.LONG,
                    entry_reference=close,
                    setup_key=f"srbreak_{int(level)}_long",
                    rationale=f"Broke resistance at {level:,.2f} ({touches} prior touches).",
                    raw_confidence=0.5 + clamp(touches * 0.04, 0.0, 0.16),
                    stop_hint=float(level - atr_value * 0.5),
                )
            if prev_candle.close >= level > close:
                return SetupProposal(
                    direction=Direction.SHORT,
                    entry_reference=close,
                    setup_key=f"srbreak_{int(level)}_short",
                    rationale=f"Broke support at {level:,.2f} ({touches} prior touches).",
                    raw_confidence=0.5 + clamp(touches * 0.04, 0.0, 0.16),
                    stop_hint=float(level + atr_value * 0.5),
                )
        return None


class BreakoutRetest(Strategy):
    """13. Breakout then retest — trade the confirmation, not the break."""

    id = "breakout_retest_15m"
    name = "Breakout + Retest"
    version = "1.0"
    category = StrategyCategory.BREAKOUT
    hypothesis = (
        "Breakouts that return to the broken level and hold it are more reliable "
        "than first-touch breakouts, and offer a much tighter stop."
    )
    primary_timeframe = "15"
    default_rr = 3.0
    atr_stop_mult = 1.0
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.STRUCTURE_STOP, ExitMechanism.BREAK_EVEN,
         ExitMechanism.PARTIAL_EXIT}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"breakout_lookback": 12, "retest_atr": 0.4, "rr_target": 3.0,
                "atr_stop_mult": 1.0, "partial_at_r": 1.5, "partial_fraction": 0.5}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"breakout_lookback": [6, 9, 12, 18], "retest_atr": [0.25, 0.4, 0.6],
                "rr_target": [2.5, 3.0, 3.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        atr_value = features.last("atr14")
        if not np.isfinite(atr_value) or atr_value <= 0:
            return None

        lookback = int(self.param("breakout_lookback"))
        upper = features.series("donchian_upper")
        lower = features.series("donchian_lower")
        closes = features.series("close")
        if closes.size < lookback + 25:
            return None

        band = atr_value * float(self.param("retest_atr"))
        close = features.close
        candle = features.candle(0)
        if candle is None:
            return None

        # Find a recent breakout, then require price to have come back to it.
        for offset in range(2, lookback + 1):
            idx = -1 - offset
            if abs(idx) > closes.size or abs(idx - 1) > upper.size:
                break
            level_up = upper[idx - 1]
            level_down = lower[idx - 1]

            if np.isfinite(level_up) and closes[idx] > level_up:
                # Bullish break `offset` bars ago: has price retested and held?
                if candle.low <= level_up + band and close > level_up:
                    return SetupProposal(
                        direction=Direction.LONG,
                        entry_reference=close,
                        setup_key=f"retest_{int(level_up)}_long",
                        rationale=f"Retested broken resistance {level_up:,.2f} "
                                  f"{offset} bars after the break and held.",
                        raw_confidence=0.6,
                        stop_hint=float(min(candle.low, level_up) - band),
                    )
            if np.isfinite(level_down) and closes[idx] < level_down:
                if candle.high >= level_down - band and close < level_down:
                    return SetupProposal(
                        direction=Direction.SHORT,
                        entry_reference=close,
                        setup_key=f"retest_{int(level_down)}_short",
                        rationale=f"Retested broken support {level_down:,.2f} "
                                  f"{offset} bars after the break and rejected.",
                        raw_confidence=0.6,
                        stop_hint=float(max(candle.high, level_down) + band),
                    )
        return None


class DailyRangeExpansion(Strategy):
    """14. Range expansion — the 24/7 equivalent of an opening-range breakout.

    Crypto has no opening bell, so the "opening range" is defined as the first
    ``range_hours`` of the UTC day. Breaking that range later in the same UTC day
    is the analogue of an equities opening-range break.
    """

    id = "range_expansion_5m"
    name = "Daily Range Expansion"
    version = "1.0"
    category = StrategyCategory.BREAKOUT
    hypothesis = (
        "The range established in the first hours of the UTC day frames the "
        "session; expansion beyond it tends to extend rather than revert."
    )
    primary_timeframe = "5"
    default_rr = 2.0
    atr_stop_mult = 1.3
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TIME_STOP}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"range_hours": 4, "min_break_atr": 0.2, "rr_target": 2.0,
                "atr_stop_mult": 1.3, "time_stop_bars": 144}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"range_hours": [2, 3, 4, 6], "min_break_atr": [0.1, 0.2, 0.35],
                "rr_target": [1.8, 2.0, 2.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        atr_value = features.last("atr14")
        if not np.isfinite(atr_value) or atr_value <= 0:
            return None

        step = interval_ms(features.timeframe)
        day_start = (features.bar_open_ms // 86_400_000) * 86_400_000
        range_end = day_start + int(self.param("range_hours")) * 3_600_000

        # Only trade after the defining range has completed.
        if features.bar_open_ms < range_end:
            return None
        # ...and only for the remainder of that same UTC day.
        if features.bar_open_ms >= day_start + 86_400_000:
            return None

        opening = [c for c in features.candles if day_start <= c.open_ms < range_end]
        if len(opening) < max(3, int(self.param("range_hours")) * 3_600_000 // step // 2):
            return None

        range_high = max(c.high for c in opening)
        range_low = min(c.low for c in opening)
        threshold = atr_value * float(self.param("min_break_atr"))
        close = features.close

        # The break must be new: no earlier bar today may already have broken it.
        after = [c for c in features.candles if range_end <= c.open_ms < features.bar_open_ms]
        if any(c.close > range_high or c.close < range_low for c in after):
            return None

        if close > range_high + threshold:
            direction = Direction.LONG
        elif close < range_low - threshold:
            direction = Direction.SHORT
        else:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"rangeexp_{day_start}_{direction.value}",
            rationale=f"Expanded beyond the {self.param('range_hours')}h UTC opening range "
                      f"({range_low:,.2f}–{range_high:,.2f}).",
            raw_confidence=0.52,
            stop_hint=float(range_low if direction is Direction.LONG else range_high),
        )


def find_sr_levels(
    features: FeatureSet, *, lookback: int, cluster_distance: float, min_touches: int
) -> list[tuple[float, int]]:
    """Cluster swing points into horizontal levels, strongest first.

    A "touch" is a confirmed swing pivot within ``cluster_distance`` of the
    level. Shared by the breakout and rejection strategies.
    """
    highs, lows = features.series("high"), features.series("low")
    swing_high_mask = features.series("swing_high")
    swing_low_mask = features.series("swing_low")
    if highs.size < 10 or swing_high_mask.size != highs.size or cluster_distance <= 0:
        return []

    window = min(lookback, highs.size)
    pivots: list[float] = []
    pivots.extend(float(v) for v in highs[-window:][swing_high_mask[-window:] > 0])
    pivots.extend(float(v) for v in lows[-window:][swing_low_mask[-window:] > 0])
    if not pivots:
        return []

    pivots.sort()
    clusters: list[list[float]] = [[pivots[0]]]
    for value in pivots[1:]:
        if value - clusters[-1][-1] <= cluster_distance:
            clusters[-1].append(value)
        else:
            clusters.append([value])

    levels = [
        (float(np.mean(cluster)), len(cluster))
        for cluster in clusters
        if len(cluster) >= min_touches
    ]
    levels.sort(key=lambda item: item[1], reverse=True)
    return levels


class VolumeConfirmedBreakout(Strategy):
    """15. Volume-confirmed breakout — participation is the filter.

    A breakout on ordinary volume is usually noise. This strategy requires the
    breakout bar itself to carry statistically abnormal volume *and* a decisive
    body, which is what distinguishes real participation from a wick through a
    level.
    """

    id = "volume_breakout_15m"
    name = "Volume-Confirmed Breakout"
    version = "1.0"
    category = StrategyCategory.BREAKOUT
    hypothesis = (
        "Breakouts that carry abnormal volume represent genuine repositioning; "
        "breakouts on average volume are mostly liquidity probes."
    )
    primary_timeframe = "15"
    context_timeframes = ("60",)
    default_rr = 2.2
    atr_stop_mult = 1.6
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TRAILING_STOP,
         ExitMechanism.BREAK_EVEN, ExitMechanism.VOLATILITY_EXIT}
    )
    preferred_regimes = frozenset(
        {Regime.BREAKOUT, Regime.VOLATILITY_EXPANSION, Regime.TREND_UP, Regime.TREND_DOWN,
         Regime.STRONG_TREND_UP, Regime.STRONG_TREND_DOWN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"lookback": 20, "volume_z_min": 1.5, "min_body_ratio": 0.55,
                "rr_target": 2.2, "atr_stop_mult": 1.6, "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"lookback": [14, 20, 30], "volume_z_min": [1.0, 1.5, 2.0, 2.5],
                "min_body_ratio": [0.45, 0.55, 0.65]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        volume_z = features.last("volume_z")
        body_ratio = features.last("body_ratio")
        atr = features.last("atr14")
        close = features.close
        if not all(np.isfinite(v) for v in (volume_z, body_ratio, atr, close)) or atr <= 0:
            return None
        if volume_z < float(self.param("volume_z_min")):
            return None
        if body_ratio < float(self.param("min_body_ratio")):
            return None

        lookback = int(self.param("lookback"))
        highs, lows = features.series("high"), features.series("low")
        if highs.size < lookback + 2:
            return None
        # Prior range excludes the breakout bar itself — no look-ahead.
        prior_high = float(np.max(highs[-lookback - 1 : -1]))
        prior_low = float(np.min(lows[-lookback - 1 : -1]))

        if close > prior_high:
            direction = Direction.LONG
            level = prior_high
        elif close < prior_low:
            direction = Direction.SHORT
            level = prior_low
        else:
            return None

        htf = trend_alignment(ctx.tf("60"), direction)
        confidence = 0.55 + clamp((volume_z - 1.5) * 0.06, 0.0, 0.18) + (0.06 if htf > 0 else -0.04)

        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"volbreak_{int(features.bar_open_ms)}_{direction.value}",
            rationale=(
                f"Closed {'above' if direction is Direction.LONG else 'below'} the "
                f"{lookback}-bar extreme {level:,.2f} on volume z={volume_z:.2f} "
                f"with a {body_ratio * 100:.0f}% body."
            ),
            raw_confidence=confidence,
        )


class FailedBreakoutTrap(Strategy):
    """16. Failed-breakout trap — fade the break that could not hold.

    The mirror image of the breakout strategies: price closed beyond a level,
    then closed back inside within a bounded number of bars. Those trapped
    breakout entries become fuel for the move in the opposite direction.
    """

    id = "failed_breakout_trap_15m"
    name = "Failed-Breakout Trap"
    version = "1.0"
    category = StrategyCategory.BREAKOUT
    hypothesis = (
        "A breakout that closes back inside its range within a few bars has "
        "trapped participants whose forced exits drive the opposite move."
    )
    primary_timeframe = "15"
    context_timeframes = ("60",)
    supports_short = True
    default_rr = 2.0
    atr_stop_mult = 1.2
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.STRUCTURE_TARGET,
         ExitMechanism.BREAK_EVEN, ExitMechanism.TIME_STOP}
    )
    preferred_regimes = frozenset(
        {Regime.RANGING, Regime.HIGH_VOLATILITY, Regime.VOLATILITY_EXPANSION, Regime.UNCERTAIN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"lookback": 20, "max_bars_outside": 3, "rr_target": 2.0,
                "atr_stop_mult": 1.2, "time_stop_bars": 24}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"lookback": [14, 20, 30], "max_bars_outside": [2, 3, 5],
                "rr_target": [1.5, 2.0, 2.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        lookback = int(self.param("lookback"))
        max_outside = int(self.param("max_bars_outside"))
        highs, lows = features.series("high"), features.series("low")
        closes = features.series("close")
        atr = features.last("atr14")
        close = features.close
        if closes.size < lookback + max_outside + 2 or not np.isfinite(atr) or atr <= 0:
            return None

        # The range is measured strictly before the breakout attempt.
        base_end = closes.size - max_outside - 1
        range_high = float(np.max(highs[base_end - lookback : base_end]))
        range_low = float(np.min(lows[base_end - lookback : base_end]))

        recent_closes = closes[-max_outside - 1 : -1]
        broke_up = bool(np.any(recent_closes > range_high))
        broke_down = bool(np.any(recent_closes < range_low))

        # The trap: broke out, then closed back inside on this bar.
        if broke_up and close < range_high:
            direction = Direction.SHORT
            level = range_high
        elif broke_down and close > range_low:
            direction = Direction.LONG
            level = range_low
        else:
            return None

        excursion = (
            float(np.max(recent_closes)) - range_high
            if direction is Direction.SHORT
            else range_low - float(np.min(recent_closes))
        )
        confidence = 0.56 + clamp(excursion / atr * 0.12, 0.0, 0.16)
        # Stop goes just beyond the failed extreme — the level that invalidates it.
        stop_hint = (
            float(np.max(highs[-max_outside - 1 :])) + atr * 0.25
            if direction is Direction.SHORT
            else float(np.min(lows[-max_outside - 1 :])) - atr * 0.25
        )

        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"failbreak_{int(features.bar_open_ms)}_{direction.value}",
            rationale=(
                f"Break {'above' if direction is Direction.SHORT else 'below'} "
                f"{level:,.2f} failed and closed back inside the range within "
                f"{max_outside} bars — trapped breakout entries."
            ),
            raw_confidence=confidence,
            stop_hint=stop_hint,
            target_hint=range_low if direction is Direction.SHORT else range_high,
        )


BREAKOUT_STRATEGIES: tuple[type[Strategy], ...] = (
    DonchianBreakout,
    PreviousRangeBreakout,
    VolatilityBreakout,
    BollingerSqueezeBreakout,
    SupportResistanceBreakout,
    BreakoutRetest,
    DailyRangeExpansion,
    VolumeConfirmedBreakout,
    FailedBreakoutTrap,
)
