"""Regime-adaptive and ensemble meta-strategies.

These do not define new market hypotheses. They are hypotheses about *strategy
selection*: that choosing among strategies by regime, or requiring several to
agree, produces better risk-adjusted results than any single member.

They compete in the ranking on exactly the same terms as everything else, and
the Day-14 champion may well be one of them — the brief explicitly allows the
champion to be a validated ensemble.

Member weights are supplied by the learning layer from measured performance;
they are never hand-tuned constants.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..features.engine import FeatureSet
from ..utils.numeric import clamp, safe_div
from .base import (
    Direction,
    ExitMechanism,
    SetupProposal,
    Strategy,
    StrategyCategory,
    StrategyContext,
    StrategySignal,
)


class EnsembleBase(Strategy):
    """Shared plumbing for meta-strategies that consult member strategies."""

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        self._members: list[Strategy] = []
        # strategy_id -> weight, refreshed by the learning layer
        self._weights: dict[str, float] = {}
        # strategy_id -> {regime -> expectancy}, also from measured results
        self._regime_scores: dict[str, dict[str, float]] = {}

    def set_members(self, members: list[Strategy]) -> None:
        """Attach member strategies (called by the registry at build time)."""
        self._members = [m for m in members if not isinstance(m, EnsembleBase)]

    def update_weights(self, weights: dict[str, float]) -> None:
        self._weights = dict(weights)

    def update_regime_scores(self, scores: dict[str, dict[str, float]]) -> None:
        self._regime_scores = {k: dict(v) for k, v in scores.items()}

    @property
    def members(self) -> list[Strategy]:
        return list(self._members)

    def member_weight(self, strategy_id: str) -> float:
        """Weight for a member; 1.0 until evidence says otherwise."""
        return float(self._weights.get(strategy_id, 1.0))

    def _collect(self, ctx: StrategyContext) -> list[StrategySignal]:
        """Ask every member for a signal, ignoring individual failures.

        One misbehaving member must not silence the ensemble, so errors are
        contained per member and surfaced as a skipped vote.
        """
        signals: list[StrategySignal] = []
        for member in self._members:
            try:
                signal = member.generate_signal(ctx)
            except Exception:  # noqa: BLE001 - a broken member must not break the ensemble
                continue
            if signal is not None:
                signals.append(signal)
        return signals


class RegimeAdaptiveSelector(EnsembleBase):
    """37. Regime-adaptive selector.

    Hypothesis: no single strategy is best everywhere, but the *mapping* from
    regime to best strategy is learnable and stable enough to exploit. On each
    bar it takes signals only from members with the strongest measured record in
    the current regime.
    """

    id = "regime_adaptive_selector"
    name = "Regime-Adaptive Strategy Selector"
    version = "1.0"
    category = StrategyCategory.ENSEMBLE
    hypothesis = (
        "Strategy edge is regime-conditional. Selecting the member with the best "
        "measured expectancy in the currently-classified regime should beat any "
        "fixed choice across the whole period."
    )
    primary_timeframe = "15"
    min_confidence = 0.45
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.STRUCTURE_STOP,
         ExitMechanism.BREAK_EVEN}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"min_regime_confidence": 0.45, "top_n": 3, "fallback_to_confidence": True}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"top_n": [1, 2, 3, 5], "min_regime_confidence": [0.35, 0.45, 0.55]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        if ctx.regime.confidence < float(self.param("min_regime_confidence")):
            return None
        signals = self._collect(ctx)
        if not signals:
            return None

        regime_key = ctx.regime.regime.value
        top_n = int(self.param("top_n"))

        # Rank members by their measured expectancy in *this* regime.
        ranked = sorted(
            signals,
            key=lambda s: (
                self._regime_scores.get(s.strategy_id, {}).get(regime_key, 0.0),
                s.confidence,
            ),
            reverse=True,
        )
        eligible = [
            s
            for s in ranked[:top_n]
            if self._regime_scores.get(s.strategy_id, {}).get(regime_key, 0.0) > 0
        ]
        if not eligible:
            # No regime evidence yet (early in the experiment). Fall back to raw
            # confidence rather than sitting out and gathering nothing.
            if not self.param("fallback_to_confidence"):
                return None
            eligible = ranked[:1]

        chosen = eligible[0]
        expectancy = self._regime_scores.get(chosen.strategy_id, {}).get(regime_key, 0.0)

        return SetupProposal(
            direction=chosen.direction,
            entry_reference=chosen.entry_reference,
            setup_key=f"regimesel_{chosen.setup_key}",
            rationale=(
                f"Selected {chosen.strategy_id} for regime {regime_key} "
                f"(measured expectancy {expectancy:+.3f}R). Underlying: {chosen.rationale}"
            ),
            raw_confidence=clamp(chosen.confidence + clamp(expectancy * 0.1, -0.1, 0.1), 0.0, 1.0),
            stop_hint=chosen.stop_price,
            target_hint=chosen.target_price,
            metadata={"selected_strategy": chosen.strategy_id, "regime": regime_key},
        )


class WeightedEnsemble(EnsembleBase):
    """38. Weighted multi-strategy ensemble — trade only on agreement.

    Hypothesis: independent strategies making the same call at the same moment
    is stronger evidence than any of them alone. Requires a weighted majority in
    one direction before acting, which trades far less often but should show
    better consistency.
    """

    id = "weighted_ensemble"
    name = "Weighted Multi-Strategy Ensemble"
    version = "1.0"
    category = StrategyCategory.ENSEMBLE
    hypothesis = (
        "Agreement between strategies with different underlying logic is a "
        "stronger signal than any individual strategy, because their errors are "
        "less correlated than their successes."
    )
    primary_timeframe = "15"
    min_confidence = 0.5
    exit_mechanisms = frozenset(
        {ExitMechanism.FIXED_RR, ExitMechanism.ATR_STOP, ExitMechanism.BREAK_EVEN,
         ExitMechanism.PARTIAL_EXIT}
    )

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"min_votes": 2, "min_weight_ratio": 0.65, "rr_target": 2.0,
                "break_even_at_r": 1.0, "partial_at_r": 1.5, "partial_fraction": 0.5}

    @classmethod
    def parameter_space(cls) -> dict[str, list[Any]]:
        return {"min_votes": [2, 3, 4], "min_weight_ratio": [0.55, 0.65, 0.75, 0.85],
                "rr_target": [1.6, 2.0, 2.5]}

    def detect(self, ctx: StrategyContext, features: FeatureSet) -> SetupProposal | None:
        signals = self._collect(ctx)
        if len(signals) < int(self.param("min_votes")):
            return None

        tallies: dict[Direction, float] = defaultdict(float)
        voters: dict[Direction, list[StrategySignal]] = defaultdict(list)
        for signal in signals:
            weight = self.member_weight(signal.strategy_id) * signal.confidence
            if weight <= 0:
                continue
            tallies[signal.direction] += weight
            voters[signal.direction].append(signal)

        if not tallies:
            return None

        total = sum(tallies.values())
        direction, weight = max(tallies.items(), key=lambda kv: kv[1])
        supporters = voters[direction]

        if len(supporters) < int(self.param("min_votes")):
            return None
        # Require a decisive majority of weight, not a bare plurality.
        if safe_div(weight, total) < float(self.param("min_weight_ratio")):
            return None

        # Blend the members' levels, weighting each by its own conviction.
        weights = [self.member_weight(s.strategy_id) * s.confidence for s in supporters]
        weight_total = sum(weights) or 1.0
        entry = sum(s.entry_reference * w for s, w in zip(supporters, weights, strict=True)) / weight_total
        stop = sum(s.stop_price * w for s, w in zip(supporters, weights, strict=True)) / weight_total

        targets = [(s.target_price, w) for s, w in zip(supporters, weights, strict=True)
                   if s.target_price is not None]
        target = (
            sum(price * w for price, w in targets) / sum(w for _, w in targets)
            if targets
            else None
        )

        # The blended stop must still be on the correct side of the blended entry.
        if direction is Direction.LONG and stop >= entry:
            return None
        if direction is Direction.SHORT and stop <= entry:
            return None

        names = ", ".join(sorted(s.strategy_id for s in supporters))
        return SetupProposal(
            direction=direction,
            entry_reference=entry,
            setup_key=f"ensemble_{int(features.bar_open_ms)}_{direction.value}",
            rationale=(
                f"{len(supporters)} strategies agreed on {direction.value} "
                f"({safe_div(weight, total) * 100:.0f}% of weighted vote): {names}."
            ),
            raw_confidence=clamp(0.5 + 0.08 * len(supporters), 0.0, 0.9),
            stop_hint=stop,
            target_hint=target,
            metadata={"voters": [s.strategy_id for s in supporters]},
        )


ENSEMBLE_STRATEGIES: tuple[type[Strategy], ...] = (
    RegimeAdaptiveSelector,
    WeightedEnsemble,
)
