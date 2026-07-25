"""Day-14 champion selection, challenger promotion, and decay detection.

Three related decisions live here:

* **Selection** at the end of the research period — a single strategy, or a
  validated regime-aware ensemble, or an explicit "no champion" when the
  evidence does not justify one.
* **Promotion** afterwards — a challenger replaces the champion only on
  statistically meaningful, out-of-sample evidence, never on a hot streak.
* **Decay** — detecting that the current champion is degrading, using criteria
  strong enough that ordinary losing runs do not trigger them.

Every decision is written to ``champion_history`` with its reason and evidence.
Nothing changes silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..config.schema import ChampionConfig
from ..database.repositories import ChampionRepository
from ..learning.metrics import StrategyMetrics
from ..utils.logging import get_logger
from ..utils.numeric import clamp, safe_div
from ..utils.stats import welch_t_statistic
from ..utils.timeutil import iso, now_utc
from .scorer import ScoreBreakdown, StrategyEvidence

log = get_logger(__name__)


@dataclass(slots=True)
class ChampionSelection:
    """The outcome of a champion decision."""

    champion_type: str                      # single | ensemble | none
    champion_id: str | None
    champion_version: str = "1.0"
    score: float = 0.0
    confidence: str = "LOW"
    why_it_won: str = ""
    strongest_regimes: list[str] = field(default_factory=list)
    weakest_regimes: list[str] = field(default_factory=list)
    best_configuration: dict[str, Any] = field(default_factory=dict)
    challengers: list[tuple[str, float]] = field(default_factory=list)
    ensemble_members: dict[str, str] = field(default_factory=dict)   # regime -> strategy_id
    evidence: dict[str, Any] = field(default_factory=dict)
    rationale_lines: list[str] = field(default_factory=list)

    def banner(self) -> list[str]:
        """The Day-14 declaration block."""
        lines = ["14-DAY RESEARCH COMPLETE", ""]
        if self.champion_type == "none":
            lines.append("CHAMPION:        NONE — evidence does not justify one")
            lines.append(f"REASON:          {self.why_it_won}")
        elif self.champion_type == "ensemble":
            lines.append("CHAMPION:        REGIME-AWARE ENSEMBLE")
            lines.append(f"VERSION:         {self.champion_version}")
            lines.append("BEST CONFIGURATION:")
            for regime, strategy_id in sorted(self.ensemble_members.items()):
                lines.append(f"    {regime:<26} → {strategy_id}")
            lines.append(f"WHY IT WON:      {self.why_it_won}")
        else:
            lines.append(f"CHAMPION:        {self.champion_id}")
            lines.append(f"VERSION:         {self.champion_version}")
            config = ", ".join(f"{k}={v}" for k, v in self.best_configuration.items())
            lines.append(f"BEST CONFIGURATION: {config or 'defaults'}")
            lines.append(f"WHY IT WON:      {self.why_it_won}")

        lines.append(f"STRONGEST REGIMES: {', '.join(self.strongest_regimes) or 'n/a'}")
        lines.append(f"WEAKEST REGIMES:   {', '.join(self.weakest_regimes) or 'n/a'}")
        lines.append(f"CONFIDENCE:      {self.confidence}")
        lines.append("")
        lines.append("CHALLENGERS:")
        for index, (strategy_id, score) in enumerate(self.challengers[:3], start=1):
            lines.append(f"    {index}. {strategy_id} (score {score:.2f})")
        return lines

    def as_dict(self) -> dict[str, Any]:
        return {
            "champion_type": self.champion_type,
            "champion_id": self.champion_id,
            "champion_version": self.champion_version,
            "score": self.score,
            "confidence": self.confidence,
            "why_it_won": self.why_it_won,
            "strongest_regimes": self.strongest_regimes,
            "weakest_regimes": self.weakest_regimes,
            "ensemble_members": self.ensemble_members,
            "challengers": [{"strategy_id": s, "score": v} for s, v in self.challengers],
            "evidence": self.evidence,
        }


class ChampionSelector:
    """Selects the champion and manages challenger promotion afterwards."""

    def __init__(self, config: ChampionConfig, repository: ChampionRepository) -> None:
        self.config = config
        self.repo = repository

    # --- Day-14 selection -------------------------------------------------

    def select(
        self,
        ranked: list[ScoreBreakdown],
        evidence_map: dict[str, StrategyEvidence],
        *,
        experiment_id: str,
        regime_performance: dict[str, dict[str, float]] | None = None,
    ) -> ChampionSelection:
        """Choose a champion from the final ranking."""
        if not ranked:
            return self._no_champion("no strategies produced any scoreable evidence")

        leader = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        leader_evidence = evidence_map.get(leader.strategy_id)

        # Gate 1: is there enough evidence for *any* champion?
        insufficient = self._insufficient_evidence(leader, leader_evidence)
        if insufficient:
            selection = self._no_champion(insufficient)
            selection.challengers = [(b.strategy_id, b.final_score) for b in ranked[:3]]
            self._record(selection, experiment_id=experiment_id, previous=None)
            return selection

        # Gate 2: would a regime-aware ensemble be materially more robust?
        ensemble = self._evaluate_ensemble(ranked, evidence_map, regime_performance or {})
        if ensemble is not None and ensemble["advantage"] >= self.config.ensemble_advantage_threshold:
            selection = ChampionSelection(
                champion_type="ensemble",
                champion_id="regime_adaptive_selector",
                score=leader.final_score + ensemble["advantage"],
                confidence=leader.confidence,
                why_it_won=(
                    f"No single strategy dominates across regimes. A regime-aware ensemble "
                    f"covering {len(ensemble['members'])} regimes scores "
                    f"{ensemble['advantage']:.1f} points higher than the best single strategy "
                    f"({leader.strategy_id}, {leader.final_score:.1f}), because each member is "
                    f"used only where it has demonstrated positive expectancy."
                ),
                ensemble_members=ensemble["members"],
                strongest_regimes=sorted(ensemble["members"]),
                weakest_regimes=ensemble["uncovered"],
                challengers=[(b.strategy_id, b.final_score) for b in ranked[:3]],
                evidence={"ensemble": ensemble, "leader": leader.as_dict()},
            )
            self._record(selection, experiment_id=experiment_id, previous=None)
            return selection

        # Gate 3: single champion.
        gap = leader.final_score - (runner_up.final_score if runner_up else 0.0)
        strongest, weakest = self._regime_profile(leader_evidence)
        selection = ChampionSelection(
            champion_type="single",
            champion_id=leader.strategy_id,
            champion_version=leader.strategy_version,
            score=leader.final_score,
            confidence=leader.confidence,
            why_it_won=self._explain_win(leader, leader_evidence, gap, runner_up),
            strongest_regimes=strongest,
            weakest_regimes=weakest,
            best_configuration=dict(leader_evidence.parameters),
            challengers=[(b.strategy_id, b.final_score) for b in ranked[1:4]],
            evidence={"breakdown": leader.as_dict()},
            rationale_lines=leader.explain(),
        )
        self._record(selection, experiment_id=experiment_id, previous=None)
        return selection

    def _insufficient_evidence(
        self, leader: ScoreBreakdown, evidence: StrategyEvidence | None
    ) -> str | None:
        if evidence is None:
            return "the leading strategy has no evidence record"
        if leader.final_score <= 0:
            return f"the best score was {leader.final_score:.2f} — no strategy demonstrated an edge"
        if evidence.total_observations < self.config.min_total_observations:
            return (
                f"the leading strategy has {evidence.total_observations} total observations, "
                f"below the {self.config.min_total_observations} required to name a champion"
            )
        positive_layers = sum(
            1
            for m in (evidence.historical, evidence.shadow, evidence.demo)
            if m is not None and m.total_trades >= 5 and m.expectancy_r > 0
        )
        if positive_layers < 2:
            return (
                "the leading strategy shows positive expectancy on fewer than two independent "
                "evidence layers"
            )
        return None

    def _evaluate_ensemble(
        self,
        ranked: list[ScoreBreakdown],
        evidence_map: dict[str, StrategyEvidence],
        regime_performance: dict[str, dict[str, float]],
    ) -> dict[str, Any] | None:
        """Build the best regime→strategy mapping and score its advantage.

        An ensemble only wins if different strategies genuinely own different
        regimes *and* the combination beats the best single strategy by a clear
        margin. Otherwise the simpler answer is preferred.
        """
        if not regime_performance or len(ranked) < 2:
            return None

        members: dict[str, str] = {}
        member_expectancies: list[float] = []
        all_regimes: set[str] = set()
        for stats in regime_performance.values():
            all_regimes.update(stats)

        for regime in all_regimes:
            best_strategy, best_value = None, 0.0
            for strategy_id, stats in regime_performance.items():
                value = stats.get(regime, 0.0)
                if value > best_value:
                    best_strategy, best_value = strategy_id, value
            if best_strategy is not None and best_value > 0.05:
                members[regime] = best_strategy
                member_expectancies.append(best_value)

        if len(members) < self.config.ensemble_min_regimes:
            return None
        # Genuine diversity is required: one strategy winning everywhere is not
        # an ensemble, it is that strategy.
        if len(set(members.values())) < 2:
            return None

        leader = ranked[0]
        leader_regimes = regime_performance.get(leader.strategy_id, {})
        leader_mean = float(np.mean(list(leader_regimes.values()))) if leader_regimes else 0.0
        ensemble_mean = float(np.mean(member_expectancies))
        # Advantage in score points, scaled the same way as expectancy scoring.
        advantage = clamp((ensemble_mean - leader_mean) * 20.0, 0.0, 25.0)

        return {
            "members": members,
            "uncovered": sorted(all_regimes - set(members)),
            "ensemble_mean_expectancy": ensemble_mean,
            "leader_mean_expectancy": leader_mean,
            "advantage": advantage,
            "distinct_strategies": len(set(members.values())),
        }

    def _explain_win(
        self,
        leader: ScoreBreakdown,
        evidence: StrategyEvidence | None,
        gap: float,
        runner_up: ScoreBreakdown | None,
    ) -> str:
        parts = [
            f"Scored {leader.final_score:.1f}/100"
            + (f", {gap:.1f} points clear of {runner_up.strategy_id}" if runner_up else "")
            + "."
        ]
        top = sorted(leader.weighted.items(), key=lambda kv: kv[1], reverse=True)[:3]
        parts.append(
            "Strongest components: " + ", ".join(f"{name} ({value:+.1f})" for name, value in top) + "."
        )
        if evidence:
            layer_parts = []
            if evidence.historical and evidence.historical.total_trades:
                layer_parts.append(
                    f"out-of-sample {evidence.historical.expectancy_r:+.3f}R over "
                    f"{evidence.historical.total_trades} trades"
                )
            if evidence.shadow and evidence.shadow.total_trades:
                layer_parts.append(
                    f"shadow {evidence.shadow.expectancy_r:+.3f}R over "
                    f"{evidence.shadow.total_trades} trades"
                )
            if evidence.demo and evidence.demo.total_trades:
                layer_parts.append(
                    f"live demo {evidence.demo.expectancy_r:+.3f}R over "
                    f"{evidence.demo.total_trades} trades"
                )
            if layer_parts:
                parts.append("Evidence: " + "; ".join(layer_parts) + ".")
            if evidence.walk_forward:
                parts.append(
                    f"Profitable in {evidence.walk_forward.profitable_windows}/"
                    f"{len(evidence.walk_forward.windows)} walk-forward windows, cost robustness "
                    f"{evidence.walk_forward.cost_robustness:.2f}."
                )
        if leader.penalties:
            parts.append(
                "Penalties applied: " + ", ".join(leader.penalties) + "."
            )
        return " ".join(parts)

    @staticmethod
    def _regime_profile(evidence: StrategyEvidence | None) -> tuple[list[str], list[str]]:
        if evidence is None:
            return ([], [])
        source = evidence.shadow or evidence.historical
        if source is None or not source.by_regime:
            return ([], [])
        eligible = {k: v for k, v in source.by_regime.items() if v.get("trades", 0) >= 3}
        if not eligible:
            return ([], [])
        ordered = sorted(eligible.items(), key=lambda kv: kv[1].get("expectancy_r", 0.0), reverse=True)
        strongest = [name for name, stats in ordered if stats.get("expectancy_r", 0.0) > 0][:3]
        weakest = [name for name, stats in reversed(ordered) if stats.get("expectancy_r", 0.0) <= 0][:3]
        return (strongest, weakest)

    def _no_champion(self, reason: str) -> ChampionSelection:
        log.warning("CHAMPION", f"No champion selected: {reason}")
        return ChampionSelection(
            champion_type="none", champion_id=None, why_it_won=reason, confidence="LOW"
        )

    def _record(
        self, selection: ChampionSelection, *, experiment_id: str, previous: str | None
    ) -> None:
        self.repo.record(
            {
                "experiment_id": experiment_id,
                "ts_utc": iso(now_utc()),
                "previous_champion": previous,
                "new_champion": selection.champion_id or "NONE",
                "champion_type": selection.champion_type,
                "reason": selection.why_it_won,
                "evidence": selection.evidence,
                "metrics": {
                    "score": selection.score,
                    "challengers": [
                        {"strategy_id": s, "score": v} for s, v in selection.challengers
                    ],
                },
                "confidence": selection.confidence,
                "score": selection.score,
            }
        )

    # --- challenger promotion --------------------------------------------

    def evaluate_promotion(
        self,
        *,
        champion_id: str,
        champion_metrics: StrategyMetrics,
        challenger_id: str,
        challenger_metrics: StrategyMetrics,
        champion_r: list[float],
        challenger_r: list[float],
        experiment_id: str,
    ) -> tuple[bool, str]:
        """Decide whether a challenger should replace the champion.

        Explicit rules, all of which must hold. Three good trades is nowhere
        near enough — that is the failure mode this guards against.
        """
        reasons: list[str] = []

        if challenger_metrics.total_trades < self.config.challenger_min_new_trades:
            return (
                False,
                f"challenger has {challenger_metrics.total_trades} new trades, below the "
                f"{self.config.challenger_min_new_trades} required",
            )

        improvement = challenger_metrics.expectancy_r - champion_metrics.expectancy_r
        if improvement < self.config.challenger_min_expectancy_improvement:
            return (
                False,
                f"expectancy improvement {improvement:+.3f}R is below the required "
                f"{self.config.challenger_min_expectancy_improvement:.3f}R",
            )
        reasons.append(f"expectancy improved by {improvement:+.3f}R")

        if champion_metrics.max_drawdown_pct > 0:
            ratio = safe_div(
                challenger_metrics.max_drawdown_pct, champion_metrics.max_drawdown_pct
            )
            if ratio > 1.25:
                return (False, f"challenger drawdown is {ratio:.2f}× the champion's")
            reasons.append(f"drawdown ratio {ratio:.2f}×")

        if challenger_metrics.profit_factor < 1.1:
            return (False, f"challenger profit factor {challenger_metrics.profit_factor:.2f} < 1.10")
        reasons.append(f"profit factor {challenger_metrics.profit_factor:.2f}")

        t_stat = welch_t_statistic(challenger_r, champion_r)
        if t_stat < 1.96:
            return (
                False,
                f"the difference is not statistically meaningful (Welch t={t_stat:.2f}, need ≥1.96)",
            )
        reasons.append(f"Welch t={t_stat:.2f}")

        if challenger_metrics.expectancy_r_ci_low <= 0:
            return (
                False,
                f"challenger expectancy interval includes zero "
                f"[{challenger_metrics.expectancy_r_ci_low:+.3f}, "
                f"{challenger_metrics.expectancy_r_ci_high:+.3f}]",
            )
        reasons.append(
            f"expectancy CI [{challenger_metrics.expectancy_r_ci_low:+.3f}, "
            f"{challenger_metrics.expectancy_r_ci_high:+.3f}] excludes zero"
        )

        positive_regimes = sum(
            1
            for stats in challenger_metrics.by_regime.values()
            if stats.get("trades", 0) >= 3 and stats.get("expectancy_r", 0.0) > 0
        )
        if positive_regimes < 2:
            return (False, "challenger is positive in fewer than two regimes")
        reasons.append(f"positive in {positive_regimes} regimes")

        reason = (
            f"{challenger_id} promoted over {champion_id}: " + "; ".join(reasons)
        )
        self.repo.record(
            {
                "experiment_id": experiment_id,
                "ts_utc": iso(now_utc()),
                "previous_champion": champion_id,
                "new_champion": challenger_id,
                "champion_type": "single",
                "reason": reason,
                "evidence": {
                    "champion": champion_metrics.as_dict(),
                    "challenger": challenger_metrics.as_dict(),
                    "welch_t": t_stat,
                },
                "metrics": {"improvement_r": improvement},
                "confidence": "MEDIUM" if challenger_metrics.total_trades < 50 else "HIGH",
                "score": challenger_metrics.expectancy_r,
            }
        )
        log.info("CHAMPION", f"CHAMPION CHANGED — {reason}")
        return (True, reason)

    # --- decay ------------------------------------------------------------

    def detect_decay(
        self, champion_id: str, r_multiples: list[float], metrics: StrategyMetrics
    ) -> tuple[bool, str]:
        """Detect meaningful champion deterioration (not an ordinary losing run)."""
        window = self.config.decay_rolling_window
        if len(r_multiples) < max(self.config.decay_min_trades, window):
            return (False, "insufficient trades to assess decay")

        recent = r_multiples[-window:]
        earlier = r_multiples[:-window]
        if len(earlier) < 10:
            return (False, "insufficient history to compare against")

        recent_mean = float(np.mean(recent))
        earlier_mean = float(np.mean(earlier))
        drop = earlier_mean - recent_mean

        if earlier_mean <= 0:
            return (False, "no prior positive baseline to decay from")

        relative_drop = safe_div(drop, abs(earlier_mean))
        if relative_drop < self.config.decay_expectancy_drop_threshold:
            return (False, f"expectancy drop {relative_drop * 100:.0f}% is within normal variance")

        t_stat = welch_t_statistic(earlier, recent)
        if t_stat < 1.96:
            return (
                False,
                f"apparent drop is not statistically meaningful (t={t_stat:.2f})",
            )

        reason = (
            f"{champion_id} expectancy fell from {earlier_mean:+.3f}R to {recent_mean:+.3f}R "
            f"over the last {window} trades ({relative_drop * 100:.0f}% drop, Welch t={t_stat:.2f})"
        )
        log.warning("CHAMPION", f"Performance decay detected: {reason}")
        return (True, reason)

    def current_champion(self, experiment_id: str | None = None) -> dict[str, Any] | None:
        return self.repo.current(experiment_id)
