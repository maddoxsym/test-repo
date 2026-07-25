"""Controlled parameter learning.

The system adapts through **data**, never by rewriting its own source. Learning
is confined to:

* strategy and regime weighting (allocator)
* confidence calibration
* position-size calibration
* validated parameter changes, versioned and reversible

A candidate is proposed from one dataset and **must** be validated on a
different one. The rule that makes this meaningful: the window that *suggested*
a parameter can never be the window that *approves* it.

Search is a coarse grid over each strategy's declared parameter space, plus a
parameter-stability requirement. Grid search is used rather than Bayesian
optimisation deliberately — with the sample sizes available in 14 days, a more
aggressive optimiser mostly finds better ways to overfit.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..config.schema import BacktestingConfig, LearningConfig, RegimeConfig
from ..database.repositories import CandidateRepository, StrategyRepository
from ..exchange.models import Candle
from ..strategies.base import Strategy
from ..utils.ids import new_uuid
from ..utils.logging import get_logger
from ..utils.numeric import safe_div
from ..utils.timeutil import iso, now_utc
from .metrics import StrategyMetrics, compute_metrics

log = get_logger(__name__)

MAX_GRID_COMBINATIONS = 24


@dataclass(slots=True)
class ParameterCandidate:
    """A proposed parameter change awaiting validation."""

    candidate_id: str
    strategy_id: str
    base_version: str
    candidate_version: str
    parameters: dict[str, Any]
    proposal_basis: str
    stability_score: float = 0.0
    validation_metrics: dict[str, Any] = field(default_factory=dict)
    production_metrics: dict[str, Any] = field(default_factory=dict)
    status: str = "proposed"
    decision_reason: str = ""


@dataclass(slots=True)
class StabilityReport:
    """How sensitive a parameter set is to small changes.

    A configuration that only works at one exact value is an artefact of the
    sample, not a property of the market. Broad plateaus survive; spikes do not.
    """

    parameter: str
    best_value: Any
    best_score: float
    neighbour_scores: dict[str, float] = field(default_factory=dict)
    stability: float = 0.0
    is_plateau: bool = False

    def describe(self) -> str:
        verdict = "plateau (robust)" if self.is_plateau else "SPIKE — suspicious"
        return (
            f"{self.parameter}={self.best_value} scored {self.best_score:.3f}; "
            f"neighbours {self.neighbour_scores} → stability {self.stability:.2f} [{verdict}]"
        )


class CandidateGenerator:
    """Proposes and validates parameter candidates."""

    def __init__(
        self,
        config: LearningConfig,
        backtest_config: BacktestingConfig,
        regime_config: RegimeConfig,
        repository: CandidateRepository,
        strategy_repo: StrategyRepository,
        *,
        experiment_id: str,
    ) -> None:
        self.config = config
        self.backtest_config = backtest_config
        self.regime_config = regime_config
        self.repo = repository
        self.strategy_repo = strategy_repo
        self.experiment_id = experiment_id

    # --- proposal ---------------------------------------------------------

    def propose(
        self,
        strategy: Strategy,
        train_candles: dict[str, list[Candle]],
        *,
        symbol: str = "BTCUSDT",
    ) -> list[ParameterCandidate]:
        """Grid-search ``strategy``'s space on the training window."""
        space = strategy.parameter_space()
        if not space:
            return []
        if self.repo.count_for_strategy(strategy.id, self.experiment_id) >= self.config.max_candidates_per_strategy:
            return []

        combinations = self._build_grid(space)
        if not combinations:
            return []

        from ..backtesting.engine import Backtester
        from ..backtesting.execution_model import ExecutionCosts

        backtester = Backtester(
            costs=ExecutionCosts(
                fee_rate_taker=self.backtest_config.fee_rate_taker,
                fee_rate_maker=self.backtest_config.fee_rate_maker,
                slippage_bps=self.backtest_config.slippage_bps,
                spread_bps=self.backtest_config.spread_bps,
            ),
            regime_config=self.regime_config,
        )

        scored: list[tuple[dict[str, Any], float, StrategyMetrics]] = []
        for params in combinations:
            try:
                variant = type(strategy)(**{**strategy.params, **params})
            except (ValueError, TypeError) as exc:
                log.debug("LEARNING", f"Skipping invalid parameter set for {strategy.id}: {exc}")
                continue
            result = backtester.run(variant, train_candles, symbol=symbol)
            metrics = compute_metrics(
                result.trade_dicts(), strategy_id=strategy.id, layer="candidate_train"
            )
            if metrics.total_trades < 5:
                continue
            scored.append((params, self._objective(metrics), metrics))

        if not scored:
            return []

        scored.sort(key=lambda item: item[1], reverse=True)
        best_params, best_score, _ = scored[0]

        # Reject a winner that sits on a spike rather than a plateau.
        stability = self._stability(best_params, scored, space)
        mean_stability = (
            float(np.mean([r.stability for r in stability])) if stability else 0.0
        )
        if stability and mean_stability < self.config.parameter_stability_min_ratio:
            log.info(
                "LEARNING",
                f"Rejected candidate for {strategy.id} before validation: parameter "
                f"stability {mean_stability:.2f} below {self.config.parameter_stability_min_ratio}",
            )
            for report in stability:
                log.debug("LEARNING", f"  {report.describe()}")
            return []

        production = self.strategy_repo.production_version(strategy.id)
        base_version = production["version"] if production else strategy.version
        candidate = ParameterCandidate(
            candidate_id=f"cand_{new_uuid()}",
            strategy_id=strategy.id,
            base_version=base_version,
            candidate_version=_next_version(base_version),
            parameters=best_params,
            proposal_basis=(
                f"grid search over {len(scored)} configurations on the training window; "
                f"objective {best_score:.3f}, parameter stability {mean_stability:.2f}"
            ),
            stability_score=mean_stability,
        )
        self.repo.propose(
            {
                "candidate_id": candidate.candidate_id,
                "experiment_id": self.experiment_id,
                "strategy_id": candidate.strategy_id,
                "base_version": candidate.base_version,
                "candidate_version": candidate.candidate_version,
                "parameters": candidate.parameters,
                "proposed_ts_utc": iso(now_utc()),
                "proposal_basis": candidate.proposal_basis,
                "stability_score": candidate.stability_score,
            }
        )
        log.info(
            "LEARNING",
            f"Candidate queued: {strategy.id} {base_version} → {candidate.candidate_version} "
            f"{best_params}",
        )
        return [candidate]

    def _build_grid(self, space: dict[str, list[Any]]) -> list[dict[str, Any]]:
        """Cartesian product, bounded so a search cannot run away."""
        keys = list(space)
        values = [space[k] for k in keys]
        total = 1
        for options in values:
            total *= max(1, len(options))
        if total > MAX_GRID_COMBINATIONS:
            # Too large: vary one parameter at a time around the grid centre.
            combos: list[dict[str, Any]] = []
            centre = {k: space[k][len(space[k]) // 2] for k in keys}
            for key in keys:
                for option in space[key]:
                    combos.append({**centre, key: option})
            return combos[:MAX_GRID_COMBINATIONS]
        return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*values)]

    @staticmethod
    def _objective(metrics: StrategyMetrics) -> float:
        """Ranking objective — expectancy, discounted for drawdown and thin samples.

        Not raw PnL: that would select the most leveraged-looking parameter set.
        """
        if metrics.total_trades < 5:
            return -1.0
        sample_factor = min(1.0, metrics.total_trades / 30.0)
        drawdown_penalty = 1.0 / (1.0 + metrics.max_drawdown_pct * 3.0)
        return metrics.expectancy_r * sample_factor * drawdown_penalty

    def _stability(
        self,
        best: dict[str, Any],
        scored: list[tuple[dict[str, Any], float, StrategyMetrics]],
        space: dict[str, list[Any]],
    ) -> list[StabilityReport]:
        """Check each winning parameter against its neighbouring values."""
        lookup = {tuple(sorted(params.items())): score for params, score, _ in scored}
        best_score = lookup.get(tuple(sorted(best.items())), 0.0)
        if best_score <= 0:
            return []

        reports: list[StabilityReport] = []
        for key, value in best.items():
            options = space.get(key, [])
            if value not in options or len(options) < 3:
                continue
            index = options.index(value)
            neighbours = self.config.parameter_stability_neighbours
            neighbour_scores: dict[str, float] = {}
            for offset in range(-neighbours, neighbours + 1):
                if offset == 0:
                    continue
                position = index + offset
                if not 0 <= position < len(options):
                    continue
                probe = dict(best)
                probe[key] = options[position]
                score = lookup.get(tuple(sorted(probe.items())))
                if score is not None:
                    neighbour_scores[str(options[position])] = round(score, 4)

            if not neighbour_scores:
                continue
            mean_neighbour = float(np.mean(list(neighbour_scores.values())))
            stability = float(np.clip(safe_div(mean_neighbour, best_score), 0.0, 1.0))
            reports.append(
                StabilityReport(
                    parameter=key,
                    best_value=value,
                    best_score=best_score,
                    neighbour_scores=neighbour_scores,
                    stability=stability,
                    is_plateau=stability >= self.config.parameter_stability_min_ratio,
                )
            )
        return reports

    # --- validation -------------------------------------------------------

    def validate(
        self,
        strategy: Strategy,
        candidate: ParameterCandidate,
        validation_candles: dict[str, list[Candle]],
        *,
        symbol: str = "BTCUSDT",
    ) -> bool:
        """Evaluate a candidate on held-out data. Returns True when promoted.

        This is the guarantee the brief demands: the data used here is disjoint
        from the data that proposed the candidate.
        """
        from ..backtesting.engine import Backtester
        from ..backtesting.execution_model import ExecutionCosts

        backtester = Backtester(
            costs=ExecutionCosts(
                fee_rate_taker=self.backtest_config.fee_rate_taker,
                fee_rate_maker=self.backtest_config.fee_rate_maker,
                slippage_bps=self.backtest_config.slippage_bps,
                spread_bps=self.backtest_config.spread_bps,
            ),
            regime_config=self.regime_config,
        )

        production_result = backtester.run(strategy, validation_candles, symbol=symbol)
        production_metrics = compute_metrics(
            production_result.trade_dicts(), strategy_id=strategy.id, layer="validation_production"
        )

        try:
            variant = type(strategy)(**{**strategy.params, **candidate.parameters})
        except (ValueError, TypeError) as exc:
            self._reject(candidate, f"candidate parameters are invalid: {exc}")
            return False

        candidate_result = backtester.run(variant, validation_candles, symbol=symbol)
        candidate_metrics = compute_metrics(
            candidate_result.trade_dicts(), strategy_id=strategy.id, layer="validation_candidate"
        )

        reason = self._decide(production_metrics, candidate_metrics)
        if reason is not None:
            self._reject(
                candidate, reason, validation=candidate_metrics, production=production_metrics
            )
            return False

        improvement = candidate_metrics.expectancy_r - production_metrics.expectancy_r
        self.repo.decide(
            candidate.candidate_id,
            status="accepted",
            reason=(
                f"validated on held-out data: expectancy {production_metrics.expectancy_r:+.3f}R → "
                f"{candidate_metrics.expectancy_r:+.3f}R (+{improvement:.3f}) over "
                f"{candidate_metrics.total_trades} trades; profit factor "
                f"{candidate_metrics.profit_factor:.2f}; max drawdown "
                f"{candidate_metrics.max_drawdown_pct * 100:.1f}%"
            ),
            validation_metrics=candidate_metrics.as_dict(),
            production_metrics=production_metrics.as_dict(),
        )
        self.strategy_repo.upsert_version(
            candidate.strategy_id,
            candidate.candidate_version,
            candidate.parameters,
            is_production=True,
            source="promoted_candidate",
        )
        self.strategy_repo.set_production_version(
            candidate.strategy_id, candidate.candidate_version
        )
        log.info(
            "VALIDATION",
            f"Candidate ACCEPTED: {candidate.strategy_id} → {candidate.candidate_version} "
            f"(expectancy +{improvement:.3f}R on held-out data)",
        )
        return True

    def _decide(
        self, production: StrategyMetrics, candidate: StrategyMetrics
    ) -> str | None:
        """Return a rejection reason, or ``None`` to accept."""
        if candidate.total_trades < self.config.promotion_min_new_trades:
            return (
                f"insufficient validation trades: {candidate.total_trades} < "
                f"{self.config.promotion_min_new_trades}"
            )
        improvement = candidate.expectancy_r - production.expectancy_r
        if improvement < self.config.promotion_min_expectancy_improvement:
            return (
                f"expectancy improvement {improvement:+.3f}R below the required "
                f"{self.config.promotion_min_expectancy_improvement:.3f}R"
            )
        if candidate.profit_factor < self.config.promotion_min_profit_factor:
            return (
                f"profit factor {candidate.profit_factor:.2f} below the required "
                f"{self.config.promotion_min_profit_factor:.2f}"
            )
        if production.max_drawdown_pct > 0:
            ratio = safe_div(candidate.max_drawdown_pct, production.max_drawdown_pct)
            if ratio > self.config.promotion_max_drawdown_ratio:
                return (
                    f"drawdown worsened by {ratio:.2f}× (limit "
                    f"{self.config.promotion_max_drawdown_ratio:.2f}×)"
                )
        if candidate.expectancy_r_ci_low <= 0:
            return (
                f"expectancy confidence interval includes zero "
                f"[{candidate.expectancy_r_ci_low:+.3f}, {candidate.expectancy_r_ci_high:+.3f}]"
            )
        return None

    def _reject(
        self,
        candidate: ParameterCandidate,
        reason: str,
        *,
        validation: StrategyMetrics | None = None,
        production: StrategyMetrics | None = None,
    ) -> None:
        self.repo.decide(
            candidate.candidate_id,
            status="rejected",
            reason=reason,
            validation_metrics=validation.as_dict() if validation else None,
            production_metrics=production.as_dict() if production else None,
        )
        log.info("VALIDATION", f"Candidate REJECTED: {candidate.strategy_id} — {reason}")

    def pending(self) -> list[ParameterCandidate]:
        return [
            ParameterCandidate(
                candidate_id=row["candidate_id"],
                strategy_id=row["strategy_id"],
                base_version=row["base_version"],
                candidate_version=row["candidate_version"],
                parameters=row["parameters"],
                proposal_basis=row["proposal_basis"],
                stability_score=float(row.get("stability_score") or 0.0),
                status=row["status"],
            )
            for row in self.repo.pending(self.experiment_id)
        ]


def _next_version(version: str) -> str:
    """``1.3`` → ``1.4``; anything unparseable gets a ``.1`` suffix."""
    try:
        major, minor = version.split(".", 1)
        return f"{major}.{int(minor) + 1}"
    except (ValueError, AttributeError):
        return f"{version}.1"
