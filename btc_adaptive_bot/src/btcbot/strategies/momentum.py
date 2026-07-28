"""Momentum strategies.

Shared hypothesis family: the *rate of change* of price carries information
beyond price level. These differ from the trend family by acting on
acceleration rather than on established direction, so they typically enter
earlier and exit faster.
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


class RsiMomentum(Strategy):
    """15. RSI momentum — strength breakout, *not* an overbought fade.

    Deliberately the opposite reading of RSI from the mean-reversion family:
    here RSI crossing up through 60 is treated as strength confirming, not as a
    warning. Running both lets the experiment measure which reading pays.
    """

    id = "rsi_momentum_5m"
    name = "RSI Momentum"
    version = "1.0"
    category = StrategyCategory.MOMENTUM
    hypothesis = (
        "RSI pushing through a mid-band threshold marks the start of a directional "
        "impulse; in trending conditions this continues rather than reverts."
    )
    primary_timeframe = "5"
    context_timeframes = ("60",)
    default_rr = 1.8
    atr_stop_mult = 1.3
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TIME_STOP,
         ExitMechanism.BREAK_EVEN}
    )
    preferred_regimes = frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.STRONG_TREND_UP, Regime.STRONG_TREND_DOWN,
         Regime.BREAKOUT, Regime.VOLATILITY_EXPANSION}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"upper": 60.0, "lower": 40.0, "rr_target": 1.8, "atr_stop_mult": 1.3,
                "time_stop_bars": 36, "break_even_at_r": 1.0, "require_htf": True}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"upper": [55.0, 58.0, 60.0, 63.0, 66.0], "lower": [34.0, 37.0, 40.0, 42.0, 45.0],
                "rr_target": [1.5, 1.8, 2.2]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        rsi_now = features.last("rsi14")
        rsi_prev = features.prev("rsi14")
        if not (np.isfinite(rsi_now) and np.isfinite(rsi_prev)):
            return None

        upper, lower = float(self.param("upper")), float(self.param("lower"))
        if rsi_prev <= upper < rsi_now:
            direction = Direction.LONG
        elif rsi_prev >= lower > rsi_now:
            direction = Direction.SHORT
        else:
            return None

        if self.param("require_htf") and trend_alignment(ctx.tf("60"), direction) < 0:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=features.close,
            setup_key=f"rsimom_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"RSI crossed {upper if direction is Direction.LONG else lower:.0f} "
                      f"({rsi_prev:.1f} → {rsi_now:.1f}).",
            raw_confidence=0.5 + clamp(abs(rsi_now - 50) * 0.006, 0.0, 0.15),
        )


class MacdHistogramMomentum(Strategy):
    """16. MACD histogram — acceleration turning while trend is intact."""

    id = "macd_histogram_15m"
    name = "MACD Histogram Momentum"
    version = "1.0"
    category = StrategyCategory.MOMENTUM
    hypothesis = (
        "The histogram measures momentum's second derivative; a turn from "
        "contracting to expanding in the trend direction precedes continuation."
    )
    primary_timeframe = "15"
    default_rr = 2.0
    atr_stop_mult = 1.5
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.OPPOSITE_SIGNAL}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"min_bars_contracting": 2, "rr_target": 2.0, "atr_stop_mult": 1.5}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"min_bars_contracting": [1, 2, 3, 4], "rr_target": [1.5, 2.0, 2.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        hist = features.series("macd_hist")
        ema50 = features.last("ema50")
        need = int(self.param("min_bars_contracting")) + 2
        if hist.size < need + 2 or not np.isfinite(ema50):
            return None

        window = hist[-(need + 1) :]
        if not np.all(np.isfinite(window)):
            return None

        current, previous = window[-1], window[-2]
        close = features.close
        contracting = int(self.param("min_bars_contracting"))

        # Bullish: histogram was shrinking below zero, then turned up, with price
        # above its 50 EMA so we are not fading a downtrend.
        was_falling = all(
            window[-2 - i] < window[-3 - i] for i in range(contracting) if len(window) > 3 + i
        )
        was_rising = all(
            window[-2 - i] > window[-3 - i] for i in range(contracting) if len(window) > 3 + i
        )

        if current > previous and was_falling and close > ema50:
            direction = Direction.LONG
        elif current < previous and was_rising and close < ema50:
            direction = Direction.SHORT
        else:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"macdhist_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"MACD histogram turned {'up' if direction is Direction.LONG else 'down'} "
                      f"after {contracting} contracting bars, price on the trend side of EMA50.",
            raw_confidence=0.5,
        )


class RocMomentum(Strategy):
    """17. Rate-of-change momentum — raw velocity above a volatility-scaled bar."""

    id = "roc_momentum_15m"
    name = "Rate-of-Change Momentum"
    version = "1.0"
    category = StrategyCategory.MOMENTUM
    hypothesis = (
        "Absolute velocity, normalised by prevailing volatility, is a cleaner "
        "momentum measure than oscillators that saturate at their bounds."
    )
    primary_timeframe = "15"
    default_rr = 2.0
    atr_stop_mult = 1.5
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TRAILING_STOP}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"roc_period": "roc10", "vol_multiple": 1.8, "rr_target": 2.0,
                "atr_stop_mult": 1.5, "trail_atr_mult": 2.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"vol_multiple": [1.2, 1.5, 1.8, 2.2, 2.6], "rr_target": [1.5, 2.0, 2.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        roc_value = features.last(self.param("roc_period"))
        realized_vol = features.last("realized_vol")
        if not (np.isfinite(roc_value) and np.isfinite(realized_vol)) or realized_vol <= 0:
            return None

        # Scale the ROC threshold by realised volatility so the same parameter
        # means the same thing in calm and violent markets.
        threshold = realized_vol * float(self.param("vol_multiple")) * np.sqrt(10)
        if abs(roc_value) < threshold:
            return None

        roc_prev = features.prev(self.param("roc_period"))
        if np.isfinite(roc_prev) and abs(roc_prev) >= threshold:
            return None  # already signalled on a previous bar

        direction = Direction.LONG if roc_value > 0 else Direction.SHORT
        return SetupProposal(
            direction=direction,
            entry_reference=features.close,
            setup_key=f"roc_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"ROC {roc_value * 100:.2f}% exceeded the volatility-scaled "
                      f"threshold {threshold * 100:.2f}%.",
            raw_confidence=0.5 + clamp((abs(roc_value) / threshold - 1.0) * 0.2, 0.0, 0.2),
        )


class VolumeConfirmedMomentum(Strategy):
    """18. Momentum that participation agrees with."""

    id = "volume_momentum_5m"
    name = "Volume-Confirmed Momentum"
    version = "1.0"
    category = StrategyCategory.MOMENTUM
    hypothesis = (
        "A directional move on above-average volume reflects genuine "
        "participation; the same move on thin volume is far more likely to fade."
    )
    primary_timeframe = "5"
    default_rr = 1.8
    atr_stop_mult = 1.2
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TIME_STOP}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"volume_z_min": 1.2, "roc_min": 0.0015, "rr_target": 1.8,
                "atr_stop_mult": 1.2, "time_stop_bars": 24}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"volume_z_min": [0.8, 1.0, 1.2, 1.5, 2.0],
                "roc_min": [0.0008, 0.0015, 0.0025, 0.004]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        volume_z = features.last("volume_z")
        roc_value = features.last("roc10")
        ema21 = features.last("ema21")
        if not all(np.isfinite(v) for v in (volume_z, roc_value, ema21)):
            return None
        if volume_z < float(self.param("volume_z_min")):
            return None
        if abs(roc_value) < float(self.param("roc_min")):
            return None

        close = features.close
        if roc_value > 0 and close > ema21:
            direction = Direction.LONG
        elif roc_value < 0 and close < ema21:
            direction = Direction.SHORT
        else:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"volmom_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Momentum {roc_value * 100:.2f}% confirmed by volume "
                      f"z-score {volume_z:.2f}.",
            raw_confidence=0.5 + clamp(volume_z * 0.05, 0.0, 0.2),
        )


class MultiTimeframeContinuation(Strategy):
    """19. Multi-timeframe momentum continuation.

    The 4H sets the macro direction, the 1H must agree, the 15M provides the
    structural pullback, and the 5M triggers. Four horizons must line up, which
    makes this the most selective strategy in the library.
    """

    id = "mtf_continuation_5m"
    name = "Multi-Timeframe Momentum Continuation"
    version = "1.0"
    category = StrategyCategory.MOMENTUM
    hypothesis = (
        "When several horizons agree on direction and the shortest one has just "
        "finished a counter-trend pause, continuation odds are at their best."
    )
    primary_timeframe = "5"
    context_timeframes = ("15", "60", "240")
    default_rr = 2.5
    atr_stop_mult = 1.2
    min_confidence = 0.4
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.BREAK_EVEN,
         ExitMechanism.PARTIAL_EXIT}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"pullback_bars": 3, "rr_target": 2.5, "atr_stop_mult": 1.2,
                "break_even_at_r": 1.0, "partial_at_r": 1.5, "partial_fraction": 0.5}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"pullback_bars": [2, 3, 4, 5], "rr_target": [2.0, 2.5, 3.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        macro = ctx.tf("240")
        mid = ctx.tf("60")
        structure = ctx.tf("15")
        if macro is None or mid is None or structure is None:
            return None

        macro_bias = _ema_bias(macro)
        mid_bias = _ema_bias(mid)
        structure_bias = _ema_bias(structure)
        if macro_bias == 0 or macro_bias != mid_bias or macro_bias != structure_bias:
            return None

        direction = Direction.LONG if macro_bias > 0 else Direction.SHORT

        # The 5M must have just paused against the trend and then resumed.
        pullback_bars = int(self.param("pullback_bars"))
        if len(features.candles) < pullback_bars + 2:
            return None
        recent = features.candles[-(pullback_bars + 1) : -1]

        if direction is Direction.LONG:
            paused = all(c.close <= c.open for c in recent[-2:])
            resumed = features.close > max(c.high for c in recent)
        else:
            paused = all(c.close >= c.open for c in recent[-2:])
            resumed = features.close < min(c.low for c in recent)

        if not (paused and resumed):
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=features.close,
            setup_key=f"mtfcont_{int(features.bar_open_ms)}_{direction.value}",
            rationale="4H, 1H and 15M all aligned; 5M paused against the trend then resumed.",
            raw_confidence=0.65,
            stop_hint=float(min(c.low for c in recent)) if direction is Direction.LONG
            else float(max(c.high for c in recent)),
        )


def _ema_bias(features: FeatureSet) -> int:
    """+1 when EMA50 > EMA200 and price agrees, -1 for the mirror, else 0."""
    ema50 = features.last("ema50")
    ema200 = features.last("ema200")
    close = features.close
    if not (np.isfinite(ema50) and np.isfinite(ema200)):
        return 0
    if ema50 > ema200 and close > ema50:
        return 1
    if ema50 < ema200 and close < ema50:
        return -1
    return 0


class ShallowPullbackContinuation(Strategy):
    """22. Shallow-pullback continuation — strength that barely retraces.

    Distinct from a moving-average pullback: the signal here is the *shallowness*
    itself. After a measured impulse, a retracement that stays inside a small
    fraction of that impulse says buyers never let price back — which is a
    different observation from price touching an average.
    """

    id = "shallow_pullback_15m"
    name = "Shallow-Pullback Continuation"
    version = "1.0"
    category = StrategyCategory.MOMENTUM
    hypothesis = (
        "An impulse that retraces only shallowly before resuming indicates "
        "demand so persistent that patient buyers never get filled lower."
    )
    primary_timeframe = "15"
    context_timeframes = ("60",)
    default_rr = 2.2
    atr_stop_mult = 1.4
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TRAILING_STOP,
         ExitMechanism.BREAK_EVEN, ExitMechanism.PARTIAL_EXIT}
    )
    preferred_regimes = frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.STRONG_TREND_UP,
         Regime.STRONG_TREND_DOWN, Regime.BREAKOUT}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"impulse_bars": 6, "min_impulse_atr": 2.0, "max_retrace": 0.382,
                "rr_target": 2.2, "atr_stop_mult": 1.4, "break_even_at_r": 1.0,
                "partial_at_r": 1.0, "partial_fraction": 0.5}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"impulse_bars": [4, 6, 8], "min_impulse_atr": [1.5, 2.0, 3.0],
                "max_retrace": [0.236, 0.382, 0.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        bars = int(self.param("impulse_bars"))
        highs, lows = features.series("high"), features.series("low")
        closes = features.series("close")
        atr = features.last("atr14")
        close = features.close
        if closes.size < bars + 3 or not np.isfinite(atr) or atr <= 0:
            return None

        # The impulse is measured over bars that closed *before* the pullback.
        impulse_start = closes.size - bars - 1
        leg_low = float(np.min(lows[impulse_start:-1]))
        leg_high = float(np.max(highs[impulse_start:-1]))
        leg_size = leg_high - leg_low
        if leg_size < atr * float(self.param("min_impulse_atr")):
            return None

        max_retrace = float(self.param("max_retrace"))
        up_leg = closes[-2] > closes[impulse_start]
        if up_leg:
            retrace = (leg_high - float(np.min(lows[-2:]))) / leg_size
            direction = Direction.LONG
            resumed = close > closes[-2]
        else:
            retrace = (float(np.max(highs[-2:])) - leg_low) / leg_size
            direction = Direction.SHORT
            resumed = close < closes[-2]

        if retrace > max_retrace or retrace <= 0 or not resumed:
            return None

        htf = trend_alignment(ctx.tf("60"), direction)
        if htf < 0:
            return None

        confidence = 0.6 + clamp((max_retrace - retrace) * 0.4, 0.0, 0.15)
        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"shallowpb_{int(features.bar_open_ms)}_{direction.value}",
            rationale=(
                f"{leg_size / atr:.1f} ATR impulse retraced only "
                f"{retrace * 100:.0f}% before resuming — demand never let price back."
            ),
            raw_confidence=confidence,
            stop_hint=float(leg_low if direction is Direction.LONG else leg_high),
        )


MOMENTUM_STRATEGIES: tuple[type[Strategy], ...] = (
    RsiMomentum,
    MacdHistogramMomentum,
    RocMomentum,
    VolumeConfirmedMomentum,
    MultiTimeframeContinuation,
    ShallowPullbackContinuation,
)
