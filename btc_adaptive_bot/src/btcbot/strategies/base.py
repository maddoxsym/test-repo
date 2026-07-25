"""Strategy interface.

Every strategy implements one method — :meth:`Strategy.detect` — which answers a
single question: *does my hypothesis fire on the bar that just closed?* All the
shared machinery (stop placement, target placement, confidence blending,
validation, signal construction) lives in the base class so that 38 strategies
do not reimplement it 38 times.

The public entry point is :meth:`Strategy.generate_signal`, which runs the full
pipeline and returns a validated :class:`StrategySignal` or ``None``.

Strategies only ever receive closed-candle features. They have no reference to
the market store, no clock, and no I/O, which is what makes them reproducible in
both live and backtest contexts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

import numpy as np

from ..features.engine import FeatureSet, MultiTimeframeFeatures
from ..regime.classifier import Regime, RegimeSnapshot
from ..utils.numeric import clamp, safe_div


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> Direction:
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class StrategyCategory(str, Enum):
    TREND = "trend"
    BREAKOUT = "breakout"
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    STRUCTURE = "structure"
    VOLUME = "volume"
    ENSEMBLE = "ensemble"


class ExitMechanism(str, Enum):
    """Exit techniques a strategy explicitly opts into.

    Declared per strategy rather than applied globally: trailing a mean-reversion
    trade, for instance, usually destroys its edge.
    """

    FIXED_RR = "fixed_rr"
    ATR_STOP = "atr_stop"
    ATR_TARGET = "atr_target"
    STRUCTURE_STOP = "structure_stop"
    STRUCTURE_TARGET = "structure_target"
    TRAILING_STOP = "trailing_stop"
    BREAK_EVEN = "break_even"
    PARTIAL_EXIT = "partial_exit"
    TIME_STOP = "time_stop"
    VOLATILITY_EXIT = "volatility_exit"
    OPPOSITE_SIGNAL = "opposite_signal"


@dataclass(frozen=True, slots=True)
class ExitPolicy:
    """Concrete exit parameters attached to a signal."""

    mechanisms: frozenset[ExitMechanism]
    rr_target: float = 2.0
    trail_atr_mult: float = 2.0
    break_even_at_r: float = 1.0
    partial_at_r: float = 1.0
    partial_fraction: float = 0.5
    time_stop_bars: int = 0
    volatility_exit_mult: float = 2.5

    def supports(self, mechanism: ExitMechanism) -> bool:
        return mechanism in self.mechanisms


@dataclass(frozen=True, slots=True)
class SetupProposal:
    """What a strategy returns from :meth:`Strategy.detect`.

    Deliberately minimal — the strategy says *what it saw*, and the base class
    turns that into a fully-specified, validated trade plan.
    """

    direction: Direction
    entry_reference: float
    setup_key: str
    rationale: str
    raw_confidence: float = 0.5
    stop_hint: float | None = None
    target_hint: float | None = None
    rr_override: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StrategySignal:
    """A complete, validated trade proposal."""

    strategy_id: str
    strategy_version: str
    direction: Direction
    symbol: str
    timeframe: str
    bar_open_ms: int
    entry_reference: float
    stop_price: float
    target_price: float | None
    confidence: float
    setup_key: str
    rationale: str
    exit_policy: ExitPolicy
    regime: Regime
    regime_confidence: float
    features_snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_reference - self.stop_price)

    @property
    def stop_distance_pct(self) -> float:
        return safe_div(self.stop_distance, self.entry_reference)

    @property
    def rr_ratio(self) -> float | None:
        if self.target_price is None:
            return None
        reward = abs(self.target_price - self.entry_reference)
        return safe_div(reward, self.stop_distance) or None

    def with_confidence(self, confidence: float) -> StrategySignal:
        return replace(self, confidence=clamp(confidence, 0.0, 1.0))


@dataclass(slots=True)
class StrategyContext:
    """Everything a strategy is allowed to see when deciding."""

    symbol: str
    features: MultiTimeframeFeatures
    regime: RegimeSnapshot
    news_risk: float = 0.0          # 0 = calm, 1 = major event window
    news_blocks_entry: bool = False
    news_direction_bias: float = 0.0
    spread_bps: float = 0.0
    equity: float = 10_000.0

    def tf(self, timeframe: str) -> FeatureSet | None:
        return self.features.get(timeframe)


class Strategy(ABC):
    """Base class for every strategy.

    Subclasses set the class attributes, implement :meth:`detect`, and optionally
    override the stop/target/confidence hooks.
    """

    # --- identity (set by subclasses) ---
    id: str = "unnamed"
    name: str = "Unnamed strategy"
    version: str = "1.0"
    category: StrategyCategory = StrategyCategory.TREND
    hypothesis: str = ""

    # --- data requirements ---
    primary_timeframe: str = "15"
    context_timeframes: tuple[str, ...] = ()
    min_bars: int = 210

    # --- behaviour ---
    supports_short: bool = True
    exit_mechanisms: frozenset[ExitMechanism] = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP}
    )
    default_rr: float = 2.0
    atr_stop_mult: float = 1.5
    min_confidence: float = 0.35
    # Regimes this strategy claims to work in. Empty = all regimes.
    preferred_regimes: frozenset[Regime] = frozenset()

    def __init__(self, **params: Any) -> None:
        self.params: dict[str, Any] = dict(self.default_params())
        unknown = set(params) - set(self.params)
        if unknown:
            raise ValueError(
                f"{self.id}: unknown parameter(s) {sorted(unknown)}; "
                f"valid: {sorted(self.params)}"
            )
        self.params.update(params)

    # --- parameters -------------------------------------------------------

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        """Tunable parameters. Overridden by strategies that have any."""
        return {}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        """Candidate values the optimiser may explore.

        Used both for search and for parameter-stability analysis: neighbouring
        values must perform similarly or the result is treated as overfit.
        """
        return {}

    def param(self, name: str, default: Any = None) -> Any:
        return self.params.get(name, default)

    # --- required interface ----------------------------------------------

    @abstractmethod
    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        """Return a proposal when the hypothesis fires on the last closed bar."""

    # --- overridable hooks ------------------------------------------------

    def allowed_regimes(self) -> frozenset[Regime]:
        """Regimes in which this strategy is permitted to trade."""
        return self.preferred_regimes

    def calculate_stop(
        self, ctx: StrategyContext, features: FeatureSet, proposal: SetupProposal
    ) -> float | None:
        """Stop price. Defaults to an ATR stop, or the strategy's own hint."""
        if proposal.stop_hint is not None:
            return proposal.stop_hint
        atr_value = features.last("atr14")
        if not np.isfinite(atr_value) or atr_value <= 0:
            return None
        distance = atr_value * float(self.param("atr_stop_mult", self.atr_stop_mult))
        return proposal.entry_reference - proposal.direction.sign * distance

    def calculate_target(
        self,
        ctx: StrategyContext,
        features: FeatureSet,
        proposal: SetupProposal,
        stop_price: float,
    ) -> float | None:
        """Target price. Defaults to a fixed multiple of the stop distance."""
        if proposal.target_hint is not None:
            return proposal.target_hint
        rr = proposal.rr_override or float(self.param("rr_target", self.default_rr))
        risk = abs(proposal.entry_reference - stop_price)
        if risk <= 0:
            return None
        return proposal.entry_reference + proposal.direction.sign * risk * rr

    def calculate_confidence(
        self, ctx: StrategyContext, features: FeatureSet, proposal: SetupProposal
    ) -> float:
        """Blend the strategy's own conviction with contextual quality signals.

        Confidence is a *calibrated input to sizing*, not a claim about the
        future. The learning layer measures whether high-confidence signals
        actually win more often and recalibrates if they do not.
        """
        confidence = clamp(proposal.raw_confidence, 0.0, 1.0)

        # Regime agreement.
        allowed = self.allowed_regimes()
        if allowed and ctx.regime.regime in allowed:
            confidence += 0.08 * ctx.regime.confidence
        elif allowed:
            confidence -= 0.10

        # Directional agreement with the prevailing trend.
        bias = ctx.regime.direction_bias
        if bias != 0:
            aligned = bias == proposal.direction.sign
            confidence += 0.05 if aligned else -0.05

        # Wide spreads make every edge smaller.
        if ctx.spread_bps > 5.0:
            confidence -= min(0.15, (ctx.spread_bps - 5.0) * 0.01)

        # Elevated news risk reduces conviction (never flips direction).
        confidence -= 0.20 * clamp(ctx.news_risk, 0.0, 1.0)

        return clamp(confidence, 0.0, 1.0)

    def position_size_inputs(
        self, ctx: StrategyContext, features: FeatureSet, signal: StrategySignal
    ) -> dict[str, Any]:
        """Inputs the risk engine uses to size this trade."""
        return {
            "stop_distance": signal.stop_distance,
            "stop_distance_pct": signal.stop_distance_pct,
            "atr": features.last("atr14"),
            "atr_pct": features.atr_pct,
            "confidence": signal.confidence,
            "regime": ctx.regime.regime.value,
            "regime_confidence": ctx.regime.confidence,
            "realized_vol": features.last("realized_vol"),
            "spread_bps": ctx.spread_bps,
            "news_risk": ctx.news_risk,
        }

    def explain_signal(self, signal: StrategySignal) -> str:
        """Human-readable account of why this trade was proposed."""
        rr = signal.rr_ratio
        return (
            f"{self.name} [{self.id} v{self.version}] {signal.direction.value.upper()} "
            f"{signal.symbol} on {signal.timeframe}m. {signal.rationale} "
            f"Entry ~{signal.entry_reference:,.2f}, stop {signal.stop_price:,.2f} "
            f"({signal.stop_distance_pct * 100:.2f}%), "
            f"target {signal.target_price:,.2f} " if signal.target_price else ""
        ) + (
            f"(R:R {rr:.2f}). " if rr else ""
        ) + (
            f"Regime {signal.regime.value} @ {signal.regime_confidence:.2f}, "
            f"confidence {signal.confidence:.2f}."
        )

    # --- pipeline ---------------------------------------------------------

    def generate_signal(self, ctx: StrategyContext) -> StrategySignal | None:
        """Full pipeline: gate → detect → stop → target → validate → confidence."""
        features = ctx.tf(self.primary_timeframe)
        if features is None or features.bar_count < self.min_bars:
            return None
        if self.context_timeframes and not ctx.features.require(*self.context_timeframes):
            return None

        allowed = self.allowed_regimes()
        if allowed and ctx.regime.regime not in allowed:
            return None

        proposal = self.detect(ctx, features)
        if proposal is None:
            return None

        if not np.isfinite(proposal.entry_reference) or proposal.entry_reference <= 0:
            return None
        if proposal.direction is Direction.SHORT and not self.supports_short:
            return None

        stop_price = self.calculate_stop(ctx, features, proposal)
        if stop_price is None or not np.isfinite(stop_price) or stop_price <= 0:
            return None

        # The stop must be on the correct side of entry. A strategy returning an
        # inverted stop is a bug, and sizing on it would be catastrophic.
        if proposal.direction is Direction.LONG and stop_price >= proposal.entry_reference:
            return None
        if proposal.direction is Direction.SHORT and stop_price <= proposal.entry_reference:
            return None

        target_price = self.calculate_target(ctx, features, proposal, stop_price)
        if target_price is not None:
            if not np.isfinite(target_price) or target_price <= 0 or proposal.direction is Direction.LONG and target_price <= proposal.entry_reference or proposal.direction is Direction.SHORT and target_price >= proposal.entry_reference:
                target_price = None

        confidence = self.calculate_confidence(ctx, features, proposal)
        if confidence < self.min_confidence:
            return None

        return StrategySignal(
            strategy_id=self.id,
            strategy_version=self.version,
            direction=proposal.direction,
            symbol=ctx.symbol,
            timeframe=self.primary_timeframe,
            bar_open_ms=features.bar_open_ms,
            entry_reference=float(proposal.entry_reference),
            stop_price=float(stop_price),
            target_price=float(target_price) if target_price is not None else None,
            confidence=confidence,
            setup_key=proposal.setup_key,
            rationale=proposal.rationale,
            exit_policy=self.exit_policy(),
            regime=ctx.regime.regime,
            regime_confidence=ctx.regime.confidence,
            features_snapshot=features.snapshot(),
        )

    def exit_policy(self) -> ExitPolicy:
        return ExitPolicy(
            mechanisms=self.exit_mechanisms,
            rr_target=float(self.param("rr_target", self.default_rr)),
            trail_atr_mult=float(self.param("trail_atr_mult", 2.0)),
            break_even_at_r=float(self.param("break_even_at_r", 1.0)),
            partial_at_r=float(self.param("partial_at_r", 1.0)),
            partial_fraction=float(self.param("partial_fraction", 0.5)),
            time_stop_bars=int(self.param("time_stop_bars", 0)),
            volatility_exit_mult=float(self.param("volatility_exit_mult", 2.5)),
        )

    def describe(self) -> dict[str, Any]:
        """Registry metadata, persisted to the ``strategies`` table."""
        return {
            "strategy_id": self.id,
            "name": self.name,
            "category": self.category.value,
            "hypothesis": self.hypothesis,
            "primary_timeframe": self.primary_timeframe,
            "supports_short": self.supports_short,
            "exit_mechanisms": sorted(m.value for m in self.exit_mechanisms),
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id} v{self.version}>"


# --- helpers shared by strategy implementations --------------------------


def swing_levels(features: FeatureSet, lookback: int = 60) -> tuple[float | None, float | None]:
    """Most recent confirmed swing high and swing low."""
    highs = features.series("high")
    lows = features.series("low")
    swing_high_mask = features.series("swing_high")
    swing_low_mask = features.series("swing_low")
    if highs.size == 0 or swing_high_mask.size != highs.size:
        return (None, None)

    window = min(lookback, highs.size)
    high_idx = np.flatnonzero(swing_high_mask[-window:] > 0)
    low_idx = np.flatnonzero(swing_low_mask[-window:] > 0)
    last_high = float(highs[-window:][high_idx[-1]]) if high_idx.size else None
    last_low = float(lows[-window:][low_idx[-1]]) if low_idx.size else None
    return (last_high, last_low)


def structure_stop(
    features: FeatureSet, direction: Direction, *, lookback: int = 40, buffer_atr: float = 0.25
) -> float | None:
    """Stop just beyond the most recent opposing swing, padded by ATR."""
    high_level, low_level = swing_levels(features, lookback)
    atr_value = features.last("atr14")
    pad = atr_value * buffer_atr if np.isfinite(atr_value) else 0.0
    if direction is Direction.LONG and low_level is not None:
        return low_level - pad
    if direction is Direction.SHORT and high_level is not None:
        return high_level + pad
    return None


def recent_extreme(features: FeatureSet, direction: Direction, lookback: int = 20) -> float | None:
    """Highest high / lowest low over ``lookback`` closed bars."""
    highs = features.series("high")
    lows = features.series("low")
    if highs.size < lookback:
        return None
    if direction is Direction.LONG:
        return float(np.max(highs[-lookback:]))
    return float(np.min(lows[-lookback:]))


def trend_alignment(context: FeatureSet | None, direction: Direction) -> float:
    """+1 when a higher timeframe agrees with ``direction``, -1 when it opposes."""
    if context is None:
        return 0.0
    ema_fast = context.last("ema50")
    ema_slow = context.last("ema200")
    if not (np.isfinite(ema_fast) and np.isfinite(ema_slow)):
        return 0.0
    bullish = ema_fast > ema_slow
    if direction is Direction.LONG:
        return 1.0 if bullish else -1.0
    return -1.0 if bullish else 1.0
