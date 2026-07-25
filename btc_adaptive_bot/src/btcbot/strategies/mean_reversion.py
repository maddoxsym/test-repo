"""Mean-reversion strategies.

Shared hypothesis family: price that has stretched far from a reference value
tends to snap back. They are the deliberate counterweight to the trend and
momentum families — the experiment is partly a test of *when* each family is
right, which is why regime attribution matters so much.

None of these use trailing stops: trailing a reversion trade converts it into a
trend trade and destroys the premise.
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
)

# Reversion is dangerous in a strong trend — that is where "cheap" gets cheaper.
REVERSION_REGIMES = frozenset(
    {Regime.RANGING, Regime.LOW_VOLATILITY, Regime.VOLATILITY_CONTRACTION,
     Regime.HIGH_VOLATILITY, Regime.UNCERTAIN}
)


class BollingerRsiReversion(Strategy):
    """20. Bollinger band touch with RSI confirmation."""

    id = "bollinger_rsi_reversion_5m"
    name = "Bollinger + RSI Reversion"
    version = "1.0"
    category = StrategyCategory.MEAN_REVERSION
    hypothesis = (
        "A close outside a volatility band together with an RSI extreme marks a "
        "statistically stretched state that reverts toward the band's centre."
    )
    primary_timeframe = "5"
    default_rr = 1.5
    atr_stop_mult = 1.2
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TIME_STOP}
    )
    preferred_regimes = REVERSION_REGIMES

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"rsi_low": 28.0, "rsi_high": 72.0, "rr_target": 1.5, "atr_stop_mult": 1.2,
                "time_stop_bars": 36, "target_middle": True}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"rsi_low": [22.0, 25.0, 28.0, 32.0], "rsi_high": [68.0, 72.0, 75.0, 78.0],
                "rr_target": [1.2, 1.5, 2.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        upper = features.last("bb_upper")
        lower = features.last("bb_lower")
        middle = features.last("bb_middle")
        rsi_value = features.last("rsi14")
        if not all(np.isfinite(v) for v in (upper, lower, middle, rsi_value)):
            return None

        close = features.close
        candle = features.candle(0)
        if candle is None:
            return None

        if close < lower and rsi_value <= float(self.param("rsi_low")):
            direction, band = Direction.LONG, lower
        elif close > upper and rsi_value >= float(self.param("rsi_high")):
            direction, band = Direction.SHORT, upper
        else:
            return None

        target = float(middle) if self.param("target_middle") else None
        return SetupProposal(
            direction=direction,
            entry_reference=close,
            setup_key=f"bbrsi_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Closed outside the Bollinger band ({band:,.2f}) with RSI {rsi_value:.1f}.",
            raw_confidence=0.5 + clamp(abs(rsi_value - 50) * 0.005, 0.0, 0.15),
            stop_hint=float(candle.low - abs(close - band) * 0.5) if direction is Direction.LONG
            else float(candle.high + abs(close - band) * 0.5),
            target_hint=target,
        )


class ZScoreReversion(Strategy):
    """21. Statistical z-score reversion — purely distributional."""

    id = "zscore_reversion_15m"
    name = "Statistical Z-Score Reversion"
    version = "1.0"
    category = StrategyCategory.MEAN_REVERSION
    hypothesis = (
        "Price displacement measured in standard deviations from its own rolling "
        "mean is mean-reverting when no trend is present."
    )
    primary_timeframe = "15"
    default_rr = 1.5
    atr_stop_mult = 1.5
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TIME_STOP}
    )
    preferred_regimes = REVERSION_REGIMES

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"entry_z": 2.2, "max_adx": 25.0, "rr_target": 1.5, "atr_stop_mult": 1.5,
                "time_stop_bars": 24}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"entry_z": [1.8, 2.0, 2.2, 2.5, 3.0], "max_adx": [20.0, 25.0, 30.0],
                "rr_target": [1.2, 1.5, 2.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        z_score = features.last("close_zscore_50")
        adx = features.last("adx14")
        sma50 = features.last("sma50")
        if not all(np.isfinite(v) for v in (z_score, adx, sma50)):
            return None
        # A strong trend invalidates the stationarity assumption entirely.
        if adx > float(self.param("max_adx")):
            return None

        entry_z = float(self.param("entry_z"))
        if z_score <= -entry_z:
            direction = Direction.LONG
        elif z_score >= entry_z:
            direction = Direction.SHORT
        else:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=features.close,
            setup_key=f"zscore_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Price {z_score:.2f} standard deviations from its 50-bar mean "
                      f"with ADX only {adx:.1f}.",
            raw_confidence=0.5 + clamp((abs(z_score) - entry_z) * 0.1, 0.0, 0.18),
            target_hint=float(sma50),
        )


class VwapReversion(Strategy):
    """22. VWAP reversion — fade displacement from the volume-weighted price."""

    id = "vwap_reversion_5m"
    name = "VWAP Mean Reversion"
    version = "1.0"
    category = StrategyCategory.MEAN_REVERSION
    hypothesis = (
        "VWAP is where the bulk of volume actually traded; large displacements "
        "from it attract flow back toward it."
    )
    primary_timeframe = "5"
    default_rr = 1.4
    atr_stop_mult = 1.3
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TIME_STOP}
    )
    preferred_regimes = REVERSION_REGIMES

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"entry_z": 2.0, "rr_target": 1.4, "atr_stop_mult": 1.3, "time_stop_bars": 48}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"entry_z": [1.5, 1.8, 2.0, 2.5, 3.0], "rr_target": [1.2, 1.4, 1.8]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        vwap = features.last("vwap48")
        deviation_z = features.last("vwap_dev_z")
        if not (np.isfinite(vwap) and np.isfinite(deviation_z)):
            return None

        entry_z = float(self.param("entry_z"))
        if deviation_z <= -entry_z:
            direction = Direction.LONG
        elif deviation_z >= entry_z:
            direction = Direction.SHORT
        else:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=features.close,
            setup_key=f"vwaprev_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Displacement from VWAP at {deviation_z:.2f} z "
                      f"(VWAP {vwap:,.2f}).",
            raw_confidence=0.5 + clamp((abs(deviation_z) - entry_z) * 0.08, 0.0, 0.15),
            target_hint=float(vwap),
        )


class KeltnerReversion(Strategy):
    """23. Keltner channel reversion — ATR-based rather than std-dev based.

    Distinct from the Bollinger variant because Keltner uses average true range
    rather than closing-price standard deviation: it reacts to gaps and wicks
    that Bollinger ignores.
    """

    id = "keltner_reversion_15m"
    name = "Keltner Channel Reversion"
    version = "1.0"
    category = StrategyCategory.MEAN_REVERSION
    hypothesis = (
        "An ATR-scaled envelope captures range excursions that a close-only "
        "standard deviation misses, giving a different reversion trigger."
    )
    primary_timeframe = "15"
    default_rr = 1.6
    atr_stop_mult = 1.2
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TIME_STOP}
    )
    preferred_regimes = REVERSION_REGIMES

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"require_wick_rejection": True, "rr_target": 1.6, "atr_stop_mult": 1.2,
                "time_stop_bars": 24}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"rr_target": [1.3, 1.6, 2.0, 2.4], "atr_stop_mult": [1.0, 1.2, 1.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        upper = features.last("kc_upper")
        lower = features.last("kc_lower")
        middle = features.last("kc_middle")
        if not all(np.isfinite(v) for v in (upper, lower, middle)):
            return None

        candle = features.candle(0)
        if candle is None:
            return None

        # Pierce the channel intrabar but close back inside — a rejection.
        pierced_low = candle.low < lower and candle.close > lower
        pierced_high = candle.high > upper and candle.close < upper

        if self.param("require_wick_rejection"):
            if pierced_low:
                direction, band = Direction.LONG, lower
            elif pierced_high:
                direction, band = Direction.SHORT, upper
            else:
                return None
        else:
            if candle.close < lower:
                direction, band = Direction.LONG, lower
            elif candle.close > upper:
                direction, band = Direction.SHORT, upper
            else:
                return None

        return SetupProposal(
            direction=direction,
            entry_reference=candle.close,
            setup_key=f"keltner_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Pierced the Keltner band at {band:,.2f} and closed back inside.",
            raw_confidence=0.55,
            stop_hint=float(candle.low) if direction is Direction.LONG else float(candle.high),
            target_hint=float(middle),
        )


class ExtremeDeviationReversion(Strategy):
    """24. Extreme short-term deviation — capitulation/blow-off fade."""

    id = "extreme_deviation_1m"
    name = "Extreme Deviation Reversion"
    version = "1.0"
    category = StrategyCategory.MEAN_REVERSION
    hypothesis = (
        "Very large, very fast moves on the 1-minute chart overshoot; the first "
        "pause after such a move usually retraces part of it."
    )
    primary_timeframe = "1"
    context_timeframes = ("15",)
    default_rr = 1.2
    atr_stop_mult = 1.0
    min_confidence = 0.4
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TIME_STOP}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"impulse_atr": 3.0, "lookback": 5, "rr_target": 1.2, "atr_stop_mult": 1.0,
                "time_stop_bars": 30}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"impulse_atr": [2.5, 3.0, 3.5, 4.0, 5.0], "lookback": [3, 5, 8],
                "rr_target": [1.0, 1.2, 1.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        atr_value = features.last("atr14")
        if not np.isfinite(atr_value) or atr_value <= 0:
            return None

        lookback = int(self.param("lookback"))
        if len(features.candles) < lookback + 2:
            return None

        window = features.candles[-lookback:]
        move = window[-1].close - window[0].open
        if abs(move) < atr_value * float(self.param("impulse_atr")):
            return None

        candle = features.candle(0)
        if candle is None:
            return None

        # Require the impulse to have *paused*: the newest bar must not extend it.
        if move > 0:
            if candle.close >= candle.open:
                return None
            direction = Direction.SHORT
        else:
            if candle.close <= candle.open:
                return None
            direction = Direction.LONG

        return SetupProposal(
            direction=direction,
            entry_reference=candle.close,
            setup_key=f"extdev_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"{abs(move) / atr_value:.1f} ATR move over {lookback} bars "
                      f"followed by a reversal bar.",
            raw_confidence=0.48 + clamp((abs(move) / atr_value - 3.0) * 0.05, 0.0, 0.15),
            stop_hint=float(max(c.high for c in window)) if direction is Direction.SHORT
            else float(min(c.low for c in window)),
        )


MEAN_REVERSION_STRATEGIES: tuple[type[Strategy], ...] = (
    BollingerRsiReversion,
    ZScoreReversion,
    VwapReversion,
    KeltnerReversion,
    ExtremeDeviationReversion,
)
