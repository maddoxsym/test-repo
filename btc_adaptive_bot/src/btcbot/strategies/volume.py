"""Volume and microstructure strategies.

Shared hypothesis family: *participation* carries information that price alone
does not. Two of these read live order-book and trade-flow state rather than
candles, which makes them the only strategies whose live behaviour cannot be
perfectly reproduced in a candle-based backtest.

That limitation is handled honestly: :meth:`Strategy.detect` returns ``None``
when the microstructure input is unavailable or unreliable, so in backtests they
simply produce no trades rather than fabricating them from proxies. Their
evidence therefore comes from the shadow and live demo layers, and the scorer's
sample-size penalty accounts for the thinner historical record.
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


class AbnormalVolumeContinuation(Strategy):
    """32. Abnormal volume in the trend direction → continuation."""

    id = "volume_continuation_5m"
    name = "Abnormal Volume Continuation"
    version = "1.0"
    category = StrategyCategory.VOLUME
    hypothesis = (
        "A volume spike on a bar that closes strongly in the trend direction "
        "reflects aggressive participation that tends to carry further."
    )
    primary_timeframe = "5"
    default_rr = 2.0
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
        return {"volume_z_min": 2.0, "min_body_ratio": 0.6, "rr_target": 2.0,
                "atr_stop_mult": 1.3, "time_stop_bars": 24, "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"volume_z_min": [1.5, 2.0, 2.5, 3.0], "min_body_ratio": [0.5, 0.6, 0.7, 0.8],
                "rr_target": [1.6, 2.0, 2.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        volume_z = features.last("volume_z")
        body_ratio = features.last("body_ratio")
        ema50 = features.last("ema50")
        candle = features.candle(0)
        if candle is None or not all(np.isfinite(v) for v in (volume_z, body_ratio, ema50)):
            return None
        if volume_z < float(self.param("volume_z_min")):
            return None
        if body_ratio < float(self.param("min_body_ratio")):
            return None

        if candle.close > candle.open and candle.close > ema50:
            direction = Direction.LONG
        elif candle.close < candle.open and candle.close < ema50:
            direction = Direction.SHORT
        else:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=candle.close,
            setup_key=f"volcont_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Volume z-score {volume_z:.2f} on a {body_ratio * 100:.0f}%-body bar "
                      f"closing with the trend.",
            raw_confidence=0.5 + clamp((volume_z - 2.0) * 0.06, 0.0, 0.18),
            stop_hint=float(candle.low) if direction is Direction.LONG else float(candle.high),
        )


class AbnormalVolumeExhaustion(Strategy):
    """33. Abnormal volume with rejection → exhaustion.

    The deliberate opposite of strategy 32 using the same input. Volume spikes
    can mean *continuation* or *capitulation*; which one is signalled by whether
    the bar closes strong or gets rejected. Running both lets the experiment
    measure how well that distinction actually separates outcomes.
    """

    id = "volume_exhaustion_5m"
    name = "Abnormal Volume Exhaustion"
    version = "1.0"
    category = StrategyCategory.VOLUME
    hypothesis = (
        "A volume spike whose bar is rejected — huge volume, small body, long "
        "wick — marks the point where the aggressive side runs out of buyers."
    )
    primary_timeframe = "5"
    default_rr = 1.8
    atr_stop_mult = 1.0
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TIME_STOP}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"volume_z_min": 2.5, "max_body_ratio": 0.35, "min_wick_ratio": 0.5,
                "rr_target": 1.8, "atr_stop_mult": 1.0, "time_stop_bars": 24}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"volume_z_min": [2.0, 2.5, 3.0, 3.5], "max_body_ratio": [0.25, 0.35, 0.45],
                "min_wick_ratio": [0.4, 0.5, 0.6]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        volume_z = features.last("volume_z")
        candle = features.candle(0)
        if candle is None or not np.isfinite(volume_z):
            return None
        if volume_z < float(self.param("volume_z_min")):
            return None

        bar_range = candle.high - candle.low
        if bar_range <= 0:
            return None
        body_ratio = candle.body / bar_range
        if body_ratio > float(self.param("max_body_ratio")):
            return None

        upper_wick = (candle.high - max(candle.open, candle.close)) / bar_range
        lower_wick = (min(candle.open, candle.close) - candle.low) / bar_range
        min_wick = float(self.param("min_wick_ratio"))

        if upper_wick >= min_wick and upper_wick > lower_wick:
            direction, stop = Direction.SHORT, candle.high
        elif lower_wick >= min_wick and lower_wick > upper_wick:
            direction, stop = Direction.LONG, candle.low
        else:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=candle.close,
            setup_key=f"volexh_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Volume z-score {volume_z:.2f} with a rejected "
                      f"{body_ratio * 100:.0f}%-body bar.",
            raw_confidence=0.5 + clamp((volume_z - 2.5) * 0.05, 0.0, 0.15),
            stop_hint=float(stop),
        )


class OrderBookImbalance(Strategy):
    """34. Order-book imbalance — resting liquidity skew.

    Live-only: returns ``None`` whenever the local book is not valid (during a
    reconnect, or in any backtest), rather than substituting a proxy.
    """

    id = "orderbook_imbalance_1m"
    name = "Order-Book Imbalance"
    version = "1.0"
    category = StrategyCategory.VOLUME
    hypothesis = (
        "A persistent skew between resting bid and ask size predicts short-term "
        "direction because the thinner side is easier to push through."
    )
    primary_timeframe = "1"
    default_rr = 1.5
    atr_stop_mult = 1.0
    min_confidence = 0.4
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TIME_STOP}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"min_imbalance": 0.35, "max_spread_bps": 4.0, "rr_target": 1.5,
                "atr_stop_mult": 1.0, "time_stop_bars": 20}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"min_imbalance": [0.25, 0.35, 0.45, 0.6], "max_spread_bps": [2.0, 4.0, 6.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        if not ctx.features.orderbook_valid:
            return None  # no book (backtest or mid-reconnect): produce nothing
        imbalance = ctx.features.orderbook_imbalance
        if not np.isfinite(imbalance):
            return None
        if ctx.spread_bps > float(self.param("max_spread_bps")):
            return None

        threshold = float(self.param("min_imbalance"))
        if imbalance >= threshold:
            direction = Direction.LONG
        elif imbalance <= -threshold:
            direction = Direction.SHORT
        else:
            return None

        # Require the last closed bar to agree, so we are not fighting momentum.
        candle = features.candle(0)
        if candle is None:
            return None
        if direction is Direction.LONG and candle.close < candle.open:
            return None
        if direction is Direction.SHORT and candle.close > candle.open:
            return None

        return SetupProposal(
            direction=direction,
            entry_reference=features.close,
            setup_key=f"obimb_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Order-book imbalance {imbalance:+.2f} with spread "
                      f"{ctx.spread_bps:.1f} bps.",
            raw_confidence=0.45 + clamp(abs(imbalance) * 0.3, 0.0, 0.2),
        )


class TradeFlowImbalance(Strategy):
    """35. Trade-flow imbalance — *executed* aggression, not resting orders.

    Distinct from strategy 34: the order book shows intent that can be pulled;
    trade flow shows aggression that has already happened.
    """

    id = "trade_flow_imbalance_1m"
    name = "Trade-Flow Imbalance"
    version = "1.0"
    category = StrategyCategory.VOLUME
    hypothesis = (
        "Sustained buy-side versus sell-side aggression in executed trades "
        "signals real directional pressure over the next few minutes."
    )
    primary_timeframe = "1"
    default_rr = 1.5
    atr_stop_mult = 1.0
    min_confidence = 0.4
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.TIME_STOP}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"min_imbalance": 0.3, "rr_target": 1.5, "atr_stop_mult": 1.0,
                "time_stop_bars": 20}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"min_imbalance": [0.2, 0.3, 0.4, 0.5], "rr_target": [1.2, 1.5, 2.0]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        flow = ctx.features.trade_flow_imbalance
        if flow == 0.0 or not np.isfinite(flow):
            return None  # no trade tape available

        threshold = float(self.param("min_imbalance"))
        if flow >= threshold:
            direction = Direction.LONG
        elif flow <= -threshold:
            direction = Direction.SHORT
        else:
            return None

        ema21 = features.last("ema21")
        if np.isfinite(ema21):
            if direction is Direction.LONG and features.close < ema21:
                return None
            if direction is Direction.SHORT and features.close > ema21:
                return None

        return SetupProposal(
            direction=direction,
            entry_reference=features.close,
            setup_key=f"tfimb_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Trade-flow imbalance {flow:+.2f} agreeing with short-term trend.",
            raw_confidence=0.45 + clamp(abs(flow) * 0.3, 0.0, 0.2),
        )


class VwapVolumeConfirmation(Strategy):
    """36. VWAP reclaim confirmed by volume — a value-area shift."""

    id = "vwap_volume_15m"
    name = "VWAP + Volume Confirmation"
    version = "1.0"
    category = StrategyCategory.VOLUME
    hypothesis = (
        "Reclaiming VWAP on above-average volume signals that the market has "
        "genuinely accepted a new value area, not merely wicked through it."
    )
    primary_timeframe = "15"
    default_rr = 2.0
    atr_stop_mult = 1.4
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.BREAK_EVEN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"volume_z_min": 0.8, "rr_target": 2.0, "atr_stop_mult": 1.4,
                "break_even_at_r": 1.0}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"volume_z_min": [0.4, 0.8, 1.2, 1.6], "rr_target": [1.6, 2.0, 2.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        vwap_now = features.last("vwap48")
        vwap_prev = features.prev("vwap48")
        volume_z = features.last("volume_z")
        candle = features.candle(0)
        previous = features.candle(1)
        if candle is None or previous is None:
            return None
        if not all(np.isfinite(v) for v in (vwap_now, vwap_prev, volume_z)):
            return None
        if volume_z < float(self.param("volume_z_min")):
            return None

        reclaimed_up = previous.close <= vwap_prev and candle.close > vwap_now
        reclaimed_down = previous.close >= vwap_prev and candle.close < vwap_now
        if not (reclaimed_up or reclaimed_down):
            return None

        direction = Direction.LONG if reclaimed_up else Direction.SHORT
        return SetupProposal(
            direction=direction,
            entry_reference=candle.close,
            setup_key=f"vwapvol_{int(features.bar_open_ms)}_{direction.value}",
            rationale=f"Reclaimed VWAP {vwap_now:,.2f} on volume z-score {volume_z:.2f}.",
            raw_confidence=0.5 + clamp(volume_z * 0.06, 0.0, 0.15),
            stop_hint=float(candle.low) if direction is Direction.LONG else float(candle.high),
        )


VOLUME_STRATEGIES: tuple[type[Strategy], ...] = (
    AbnormalVolumeContinuation,
    AbnormalVolumeExhaustion,
    OrderBookImbalance,
    TradeFlowImbalance,
    VwapVolumeConfirmation,
)
