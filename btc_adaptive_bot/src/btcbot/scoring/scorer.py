"""Multi-factor strategy scoring.

The champion is **not** chosen by win rate and **not** by raw PnL. Twelve
weighted components are combined, each normalised to 0-1, and then uncertainty
penalties are applied on top.

The two failure modes the brief calls out are handled explicitly:

* *95% win rate with one catastrophic loss* — caught by profit factor, drawdown,
  and the single-winner/streak components, none of which that record satisfies.
* *+100% return on 4 trades* — caught by the sample-size credit curve, which
  awards **zero** below the minimum trade count regardless of return.

Every component and penalty is recorded in the breakdown, so any ranking can be
explained line by line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..backtesting.walk_forward import WalkForwardReport
from ..config.schema import ScoringConfig
from ..learning.metrics import StrategyMetrics
from ..utils.numeric import clamp
from ..utils.stats import sample_size_credit


@dataclass(slots=True)
class ScoreBreakdown:
    """A strategy's score with full provenance."""

    strategy_id: str
    strategy_version: str = "1.0"
    components: dict[str, float] = field(default_factory=dict)
    weighted: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    raw_score: float = 0.0
    final_score: float = 0.0

    historical_score: float = 0.0
    walk_forward_score: float = 0.0
    shadow_score: float = 0.0
    demo_score: float = 0.0
    robustness_score: float = 0.0

    total_observations: int = 0
    demo_trades: int = 0
    confidence: str = "LOW"
    notes: list[str] = field(default_factory=list)

    def explain(self) -> list[str]:
        lines = [f"{self.strategy_id} v{self.strategy_version} — final score {self.final_score:.2f}"]
        for name, value in sorted(self.weighted.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {name:<28} {self.components.get(name, 0.0):.3f} → {value:+.2f}")
        for name, value in self.penalties.items():
            lines.append(f"  penalty: {name:<20} ×{value:.2f}")
        lines.extend(f"  note: {note}" for note in self.notes)
        return lines

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "final_score": round(self.final_score, 3),
            "raw_score": round(self.raw_score, 3),
            "historical_score": round(self.historical_score, 3),
            "walk_forward_score": round(self.walk_forward_score, 3),
            "shadow_score": round(self.shadow_score, 3),
            "demo_score": round(self.demo_score, 3),
            "robustness_score": round(self.robustness_score, 3),
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "penalties": {k: round(v, 4) for k, v in self.penalties.items()},
            "total_observations": self.total_observations,
            "demo_trades": self.demo_trades,
            "confidence": self.confidence,
            "notes": self.notes,
        }


@dataclass(slots=True)
class StrategyEvidence:
    """All three evidence layers for one strategy."""

    strategy_id: str
    strategy_version: str = "1.0"
    historical: StrategyMetrics | None = None       # out-of-sample backtest
    walk_forward: WalkForwardReport | None = None
    shadow: StrategyMetrics | None = None
    demo: StrategyMetrics | None = None
    regime_stability: float = 0.0
    # 0.5 means "not measured yet", not "medium". The scorer only credits this
    # component when walk-forward evidence actually exists to measure it from.
    parameter_stability: float = 0.5
    #: The strategy's production parameters, reported as "BEST CONFIGURATION".
    parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def total_observations(self) -> int:
        return sum(
            m.total_trades
            for m in (self.historical, self.shadow, self.demo)
            if m is not None
        )


class StrategyScorer:
    """Turns evidence into a comparable score."""

    def __init__(self, config: ScoringConfig) -> None:
        self.config = config
        weights = config.weights.as_dict()
        total = sum(weights.values())
        # Normalised so the maximum achievable raw score is 100 regardless of
        # how the operator has tuned the individual weights.
        self.weights = {k: v / total for k, v in weights.items()}

    def score(self, evidence: StrategyEvidence) -> ScoreBreakdown:
        breakdown = ScoreBreakdown(
            strategy_id=evidence.strategy_id,
            strategy_version=evidence.strategy_version,
            total_observations=evidence.total_observations,
            demo_trades=evidence.demo.total_trades if evidence.demo else 0,
        )
        components: dict[str, float] = {}

        historical = evidence.historical
        shadow = evidence.shadow
        demo = evidence.demo
        walk_forward = evidence.walk_forward

        # 1. Out-of-sample expectancy — the single most important input.
        components["oos_expectancy"] = self._expectancy_component(historical)
        # 2. Walk-forward consistency.
        components["walk_forward_consistency"] = (
            clamp(walk_forward.consistency, 0.0, 1.0) if walk_forward else 0.0
        )
        # 3. Live shadow expectancy.
        components["shadow_expectancy"] = self._expectancy_component(shadow)
        # 4. Actual OKX demo performance.
        components["demo_performance"] = self._expectancy_component(demo)
        # 5. Profit factor (blended across available layers).
        components["profit_factor"] = self._profit_factor_component(historical, shadow, demo)
        # 6. Risk-adjusted return.
        components["risk_adjusted_return"] = self._risk_adjusted_component(historical, shadow, demo)
        # 7. Drawdown (lower is better).
        components["max_drawdown"] = self._drawdown_component(historical, shadow, demo)
        # 8. Independent observations.
        components["observations"] = sample_size_credit(
            evidence.total_observations,
            full=self.config.min_trades_full_credit,
            minimum=self.config.min_trades_any_credit,
        )
        # 9. Regime stability.
        components["regime_stability"] = clamp(evidence.regime_stability, 0.0, 1.0)
        # 10. Parameter stability — credited only when it was actually measured.
        # An unevaluated strategy must not collect "benefit of the doubt" points.
        components["parameter_stability"] = (
            clamp(evidence.parameter_stability, 0.0, 1.0) if walk_forward is not None else 0.0
        )
        # 11. Fee/slippage robustness.
        components["cost_robustness"] = (
            clamp(walk_forward.cost_robustness, 0.0, 1.0) if walk_forward else 0.0
        )
        # 12. Performance consistency across layers.
        components["consistency"] = self._cross_layer_consistency(historical, shadow, demo)

        weighted = {name: components[name] * self.weights[name] * 100.0 for name in self.weights}
        breakdown.components = components
        breakdown.weighted = weighted
        breakdown.raw_score = float(sum(weighted.values()))

        penalties = self._penalties(evidence, breakdown)
        breakdown.penalties = penalties
        multiplier = float(np.prod(list(penalties.values()))) if penalties else 1.0
        breakdown.final_score = clamp(breakdown.raw_score * multiplier, 0.0, 100.0)

        breakdown.historical_score = components["oos_expectancy"] * 100
        breakdown.walk_forward_score = components["walk_forward_consistency"] * 100
        breakdown.shadow_score = components["shadow_expectancy"] * 100
        breakdown.demo_score = components["demo_performance"] * 100
        breakdown.robustness_score = (
            components["cost_robustness"] * 0.4
            + components["parameter_stability"] * 0.3
            + components["regime_stability"] * 0.3
        ) * 100
        breakdown.confidence = self._confidence(evidence, breakdown)
        return breakdown

    # --- components -------------------------------------------------------

    def _expectancy_component(self, metrics: StrategyMetrics | None) -> float:
        """Expectancy in R, mapped to 0-1 and discounted for uncertainty.

        Uses the **lower** bound of the bootstrap interval, not the point
        estimate: a strategy whose interval straddles zero has not demonstrated
        an edge, whatever its average says.
        """
        if metrics is None or metrics.total_trades == 0:
            return 0.0
        credit = sample_size_credit(
            metrics.total_trades,
            full=self.config.min_trades_full_credit,
            minimum=self.config.min_trades_any_credit,
        )
        if credit <= 0:
            return 0.0
        lower_bound = metrics.expectancy_r_ci_low
        # +0.5R lower bound is an excellent result; map that to full marks.
        normalised = clamp((lower_bound + 0.2) / 0.7, 0.0, 1.0)
        return normalised * credit

    def _profit_factor_component(self, *layers: StrategyMetrics | None) -> float:
        values = [m.profit_factor for m in layers if m is not None and m.total_trades >= 5]
        if not values:
            return 0.0
        # PF 1.0 = breakeven → 0; PF 2.0+ → full marks.
        return float(np.mean([clamp((pf - 1.0), 0.0, 1.0) for pf in values]))

    def _risk_adjusted_component(self, *layers: StrategyMetrics | None) -> float:
        values = [m.sortino_ratio for m in layers if m is not None and m.total_trades >= 5]
        if not values:
            return 0.0
        return float(np.mean([clamp(v / 3.0, 0.0, 1.0) for v in values]))

    def _drawdown_component(self, *layers: StrategyMetrics | None) -> float:
        values = [m.max_drawdown_pct for m in layers if m is not None and m.total_trades >= 5]
        if not values:
            return 0.0
        worst = max(values)
        # 0% drawdown → 1.0; 30%+ → 0.0.
        return clamp(1.0 - worst / 0.30, 0.0, 1.0)

    def _cross_layer_consistency(self, *layers: StrategyMetrics | None) -> float:
        """Do the layers agree? Disagreement means the result does not transfer."""
        expectancies = [
            m.expectancy_r for m in layers if m is not None and m.total_trades >= 5
        ]
        if len(expectancies) < 2:
            return 0.0
        if all(e > 0 for e in expectancies):
            spread = float(np.std(expectancies))
            return clamp(1.0 - spread, 0.3, 1.0)
        if all(e <= 0 for e in expectancies):
            return 0.0
        return 0.2  # layers disagree in sign

    # --- penalties --------------------------------------------------------

    def _penalties(
        self, evidence: StrategyEvidence, breakdown: ScoreBreakdown
    ) -> dict[str, float]:
        """Multiplicative penalties for the specific pathologies the brief names."""
        penalties: dict[str, float] = {}

        # Tiny sample — the "+100% on 4 trades" guard.
        if evidence.total_observations < self.config.min_trades_any_credit:
            penalties["tiny_sample"] = 0.1
            breakdown.notes.append(
                f"only {evidence.total_observations} total observations — score heavily discounted"
            )
        elif evidence.total_observations < self.config.min_trades_full_credit:
            penalties["small_sample"] = clamp(
                0.5 + 0.5 * evidence.total_observations / self.config.min_trades_full_credit, 0.5, 1.0
            )

        # One giant winner carrying the record.
        for label, metrics in (("historical", evidence.historical), ("shadow", evidence.shadow)):
            if metrics is None or metrics.total_trades < 10:
                continue
            if metrics.single_winner_concentration > self.config.single_winner_concentration_threshold:
                penalties[f"{label}_single_winner"] = 0.7
                breakdown.notes.append(
                    f"{label}: one trade produced "
                    f"{metrics.single_winner_concentration * 100:.0f}% of gross profit"
                )

        # Profit concentrated in one historical period.
        if (
            evidence.walk_forward
            and evidence.walk_forward.period_concentration
            > self.config.period_concentration_threshold
        ):
            penalties["period_concentration"] = 0.75
            breakdown.notes.append(
                f"{evidence.walk_forward.period_concentration * 100:.0f}% of walk-forward profit "
                "came from a single window"
            )

        # Out-of-sample collapse relative to shadow/demo.
        if evidence.historical and evidence.historical.total_trades >= 10:
            if evidence.historical.expectancy_r < -0.1:
                penalties["oos_negative"] = 0.6
                breakdown.notes.append("negative out-of-sample expectancy")

        # Fragile to costs.
        if evidence.walk_forward and evidence.walk_forward.total_trades >= 10:
            if evidence.walk_forward.cost_robustness < 0.25:
                penalties["cost_fragile"] = 0.7
                breakdown.notes.append("edge does not survive higher fees/slippage")

        # Parameter spike rather than plateau.
        if evidence.parameter_stability < 0.4:
            penalties["parameter_spike"] = 0.8
            breakdown.notes.append(
                f"parameter stability {evidence.parameter_stability:.2f} — results depend on "
                "precise parameter values"
            )

        # A catastrophic single loss, even alongside a high win rate.
        for label, metrics in (("shadow", evidence.shadow), ("demo", evidence.demo)):
            if metrics is None or metrics.total_trades < 5:
                continue
            if metrics.largest_loss < 0 and metrics.gross_profit > 0:
                if abs(metrics.largest_loss) > metrics.gross_profit * 0.8:
                    penalties[f"{label}_catastrophic_loss"] = 0.55
                    breakdown.notes.append(
                        f"{label}: a single loss of ${abs(metrics.largest_loss):,.2f} nearly "
                        f"erased ${metrics.gross_profit:,.2f} of gross profit"
                    )

        return penalties

    def _confidence(self, evidence: StrategyEvidence, breakdown: ScoreBreakdown) -> str:
        """LOW / MEDIUM / HIGH — how much this ranking should be trusted."""
        observations = evidence.total_observations
        demo_trades = evidence.demo.total_trades if evidence.demo else 0
        layers_positive = sum(
            1
            for m in (evidence.historical, evidence.shadow, evidence.demo)
            if m is not None and m.total_trades >= 5 and m.expectancy_r > 0
        )
        interval_clear = bool(
            evidence.shadow and evidence.shadow.expectancy_r_ci_low > 0
        ) or bool(evidence.historical and evidence.historical.expectancy_r_ci_low > 0)

        if (
            observations >= self.config.min_trades_full_credit
            and demo_trades >= 10
            and layers_positive >= 2
            and interval_clear
            and not breakdown.penalties
        ):
            return "HIGH"
        if observations >= self.config.min_trades_any_credit * 2 and layers_positive >= 2:
            return "MEDIUM"
        return "LOW"


def rank_strategies(breakdowns: list[ScoreBreakdown]) -> list[ScoreBreakdown]:
    """Rank by final score, breaking ties with the better-evidenced strategy."""
    return sorted(
        breakdowns,
        key=lambda b: (b.final_score, b.total_observations, b.demo_trades),
        reverse=True,
    )
