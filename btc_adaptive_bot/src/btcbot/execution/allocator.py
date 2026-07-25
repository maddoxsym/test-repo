"""Demo execution allocator — which strategy gets the real order.

Only one strategy at a time can control the actual demo BTC position, so
choosing between them is itself a statistical problem: exploit what looks good
without letting an early lucky run monopolise the account.

**Thompson sampling** is used because it handles that trade-off naturally and
stays interpretable — each strategy's plausible mean R-multiple is sampled from
its own posterior, and the highest draw wins. Uncertainty produces exploration
automatically, without a hand-tuned schedule.

Layer-1 (historical) and Layer-2 (shadow) evidence enter as the **prior**, so a
strategy with no live demo trades yet is not treated as a blank slate. Live demo
observations then progressively dominate as they accumulate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from ..config.schema import AllocatorConfig
from ..database.repositories import AllocatorRepository, DemoOrderRepository
from ..strategies.base import StrategySignal
from ..utils.logging import get_logger
from ..utils.numeric import clamp, safe_div
from ..utils.stats import student_t_sample
from ..utils.timeutil import iso, now_utc, parse_iso

log = get_logger(__name__)


@dataclass(slots=True)
class ArmState:
    """Allocator state for one strategy."""

    strategy_id: str
    demo_rewards: list[float] = field(default_factory=list)
    allocations: int = 0
    last_allocated: datetime | None = None
    prior_mean: float = 0.0
    prior_strength: float = 1.0
    shadow_expectancy: float = 0.0
    shadow_observations: int = 0
    historical_expectancy: float = 0.0
    historical_observations: int = 0
    regime_fitness: dict[str, float] = field(default_factory=dict)

    @property
    def demo_observations(self) -> int:
        return len(self.demo_rewards)

    @property
    def demo_expectancy(self) -> float:
        return float(np.mean(self.demo_rewards)) if self.demo_rewards else 0.0

    @property
    def total_observations(self) -> int:
        return self.demo_observations + self.shadow_observations + self.historical_observations

    def cooldown_remaining(self, cooldown_seconds: int) -> float:
        if self.last_allocated is None:
            return 0.0
        elapsed = (now_utc() - self.last_allocated).total_seconds()
        return max(0.0, cooldown_seconds - elapsed)


@dataclass(slots=True)
class AllocationDecision:
    """The allocator's answer, with its reasoning recorded."""

    granted: bool
    strategy_id: str | None = None
    signal: StrategySignal | None = None
    reason: str = ""
    sampled_scores: dict[str, float] = field(default_factory=dict)
    exploration: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "granted": self.granted,
            "strategy_id": self.strategy_id,
            "reason": self.reason,
            "exploration": self.exploration,
            "sampled_scores": {k: round(v, 4) for k, v in sorted(
                self.sampled_scores.items(), key=lambda kv: kv[1], reverse=True
            )[:8]},
        }


class DemoAllocator:
    """Chooses which qualifying signal becomes a real demo order."""

    def __init__(
        self,
        config: AllocatorConfig,
        repository: AllocatorRepository,
        order_repository: DemoOrderRepository,
        *,
        experiment_id: str,
        seed: int = 20260724,
    ) -> None:
        self.config = config
        self.repo = repository
        self.orders = order_repository
        self.experiment_id = experiment_id
        self.arms: dict[str, ArmState] = {}
        self._rng = np.random.default_rng(seed)
        self._last_global_allocation: datetime | None = None

    # --- lifecycle --------------------------------------------------------

    def initialise(self, strategy_ids: list[str]) -> None:
        """Create or restore arm state so a restart keeps its learning."""
        stored = self.repo.load(self.experiment_id)
        for strategy_id in strategy_ids:
            row = stored.get(strategy_id)
            if row is None:
                arm = ArmState(strategy_id=strategy_id)
            else:
                observations = int(row["demo_observations"] or 0)
                reward_sum = float(row["reward_sum"] or 0.0)
                arm = ArmState(
                    strategy_id=strategy_id,
                    allocations=int(row["allocations"] or 0),
                    prior_mean=float(row["prior_mean"] or 0.0),
                    prior_strength=float(row["prior_strength"] or 1.0),
                    last_allocated=(
                        parse_iso(row["last_allocated_ts_utc"])
                        if row.get("last_allocated_ts_utc")
                        else None
                    ),
                )
                # Individual rewards are not persisted; the mean is reconstructed
                # so the posterior resumes at the right centre after a restart.
                if observations > 0:
                    arm.demo_rewards = [reward_sum / observations] * observations
            self.arms[strategy_id] = arm
        log.info("ALLOC", f"Allocator ready with {len(self.arms)} arms")

    def persist(self) -> None:
        for arm in self.arms.values():
            rewards = np.asarray(arm.demo_rewards, dtype=float)
            self.repo.upsert(
                {
                    "strategy_id": arm.strategy_id,
                    "experiment_id": self.experiment_id,
                    "observations": arm.total_observations,
                    "demo_observations": arm.demo_observations,
                    "reward_sum": float(rewards.sum()) if rewards.size else 0.0,
                    "reward_sq_sum": float(np.square(rewards).sum()) if rewards.size else 0.0,
                    "last_allocated_ts_utc": iso(arm.last_allocated) if arm.last_allocated else None,
                    "allocations": arm.allocations,
                    "prior_mean": arm.prior_mean,
                    "prior_strength": arm.prior_strength,
                }
            )

    # --- evidence ---------------------------------------------------------

    def update_evidence(
        self,
        *,
        shadow: dict[str, tuple[float, int]] | None = None,
        historical: dict[str, tuple[float, int]] | None = None,
        regime_fitness: dict[str, dict[str, float]] | None = None,
    ) -> None:
        """Refresh Layer-1/Layer-2 evidence used to build each arm's prior."""
        for strategy_id, arm in self.arms.items():
            if shadow and strategy_id in shadow:
                arm.shadow_expectancy, arm.shadow_observations = shadow[strategy_id]
            if historical and strategy_id in historical:
                arm.historical_expectancy, arm.historical_observations = historical[strategy_id]
            if regime_fitness and strategy_id in regime_fitness:
                arm.regime_fitness = regime_fitness[strategy_id]
            arm.prior_mean, arm.prior_strength = self._build_prior(arm)

    def _build_prior(self, arm: ArmState) -> tuple[float, float]:
        """Blend historical and shadow evidence into a prior for the demo arm.

        Prior *strength* grows with the amount of supporting evidence but is
        capped, so live demo results can always overturn it given enough trades.
        """
        weights = 0.0
        mean = 0.0

        if arm.historical_observations > 0:
            weight = self.config.historical_prior_weight * min(
                1.0, arm.historical_observations / 50.0
            )
            mean += arm.historical_expectancy * weight
            weights += weight
        if arm.shadow_observations > 0:
            weight = self.config.shadow_prior_weight * min(1.0, arm.shadow_observations / 30.0)
            mean += arm.shadow_expectancy * weight
            weights += weight

        if weights <= 0:
            return (0.0, 1.0)
        # Strength in "pseudo-observations", capped at 8 so ~8 real demo trades
        # carry as much weight as all the prior evidence combined.
        return (mean / weights, clamp(weights * 8.0, 1.0, 8.0))

    def record_result(self, strategy_id: str, r_multiple: float) -> None:
        """Record a completed real demo trade for the arm."""
        arm = self.arms.get(strategy_id)
        if arm is None:
            return
        if not math.isfinite(r_multiple):
            return
        # Clip extreme outliers so one anomalous fill cannot dominate the posterior.
        arm.demo_rewards.append(float(clamp(r_multiple, -10.0, 10.0)))
        if len(arm.demo_rewards) > 500:
            arm.demo_rewards = arm.demo_rewards[-500:]

    # --- allocation -------------------------------------------------------

    def allocate(
        self,
        candidates: list[tuple[str, StrategySignal]],
        *,
        regime: str,
        position_open: bool,
    ) -> AllocationDecision:
        """Pick at most one candidate signal to send to the real demo account."""
        if not candidates:
            return AllocationDecision(granted=False, reason="no candidate signals")
        if position_open:
            return AllocationDecision(
                granted=False,
                reason="a strategy-owned demo position is already open (attribution requires one at a time)",
            )

        now = now_utc()
        if self._last_global_allocation is not None:
            elapsed = (now - self._last_global_allocation).total_seconds()
            if elapsed < self.config.global_cooldown_seconds:
                return AllocationDecision(
                    granted=False,
                    reason=f"global cooldown: {self.config.global_cooldown_seconds - elapsed:.0f}s remaining",
                )

        if not self._within_rate_limits(now):
            return AllocationDecision(granted=False, reason="hourly/daily demo order limit reached")

        eligible: list[tuple[str, StrategySignal]] = []
        for strategy_id, signal in candidates:
            if signal.confidence < self.config.min_signal_confidence:
                continue
            arm = self.arms.get(strategy_id)
            if arm is None:
                continue
            if arm.cooldown_remaining(self.config.cooldown_seconds_per_strategy) > 0:
                continue
            eligible.append((strategy_id, signal))

        if not eligible:
            return AllocationDecision(
                granted=False, reason="all candidates below confidence threshold or in cooldown"
            )

        # Forced exploration: strategies short of observations must still get
        # opportunities, otherwise the experiment learns nothing about them.
        under_sampled = [
            (sid, sig)
            for sid, sig in eligible
            if self.arms[sid].demo_observations < self.config.min_observations_before_exploitation
        ]
        exploring = bool(under_sampled) and (
            self._rng.random() < self.config.forced_exploration_ratio or len(under_sampled) == len(eligible)
        )

        pool = under_sampled if exploring else eligible
        scores = self._score(pool, regime=regime)
        if not scores:
            return AllocationDecision(granted=False, reason="scoring produced no eligible arm")

        winner_id = max(scores, key=lambda k: scores[k])
        winner_signal = next(sig for sid, sig in pool if sid == winner_id)
        arm = self.arms[winner_id]

        reason = (
            f"exploration draw (only {arm.demo_observations} demo observations)"
            if exploring
            else f"highest Thompson sample {scores[winner_id]:.3f} "
                 f"from {arm.demo_observations} demo + {arm.shadow_observations} shadow observations"
        )
        return AllocationDecision(
            granted=True,
            strategy_id=winner_id,
            signal=winner_signal,
            reason=reason,
            sampled_scores=scores,
            exploration=exploring,
        )

    def _score(self, pool: list[tuple[str, StrategySignal]], *, regime: str) -> dict[str, float]:
        """Score each candidate by the configured bandit method."""
        scores: dict[str, float] = {}
        for strategy_id, signal in pool:
            arm = self.arms[strategy_id]

            if self.config.method == "thompson":
                base = student_t_sample(
                    arm.demo_rewards,
                    self._rng,
                    prior_mean=arm.prior_mean,
                    prior_strength=arm.prior_strength,
                )
            elif self.config.method == "ucb":
                total = max(1, sum(a.allocations for a in self.arms.values()))
                exploration_bonus = math.sqrt(
                    2.0 * math.log(total) / max(1, arm.demo_observations)
                )
                base = arm.demo_expectancy + exploration_bonus
            else:  # confidence_weighted
                base = arm.demo_expectancy * 0.5 + arm.prior_mean * 0.5

            # Regime suitability: measured, not assumed.
            fitness = arm.regime_fitness.get(regime, 0.0)
            base += self.config.regime_suitability_weight * clamp(fitness, -1.0, 1.0)
            # A more confident signal is worth slightly more, all else equal.
            base += 0.1 * (signal.confidence - 0.5)
            scores[strategy_id] = float(base)
        return scores

    def confirm_allocation(self, strategy_id: str) -> None:
        """Mark an allocation as actually used (called after order submission)."""
        arm = self.arms.get(strategy_id)
        if arm is None:
            return
        arm.allocations += 1
        arm.last_allocated = now_utc()
        self._last_global_allocation = arm.last_allocated

    def _within_rate_limits(self, now: datetime) -> bool:
        hour_ago = iso(now - timedelta(hours=1))
        day_ago = iso(now - timedelta(days=1))
        hourly = self.orders.count_since(self.experiment_id, hour_ago)
        if hourly >= self.config.max_demo_orders_per_hour:
            return False
        daily = self.orders.count_since(self.experiment_id, day_ago)
        return daily < self.config.max_demo_orders_per_day

    # --- reporting --------------------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        return sorted(
            (
                {
                    "strategy_id": arm.strategy_id,
                    "demo_observations": arm.demo_observations,
                    "demo_expectancy": round(arm.demo_expectancy, 4),
                    "shadow_expectancy": round(arm.shadow_expectancy, 4),
                    "historical_expectancy": round(arm.historical_expectancy, 4),
                    "prior_mean": round(arm.prior_mean, 4),
                    "allocations": arm.allocations,
                }
                for arm in self.arms.values()
            ),
            key=lambda row: row["allocations"],
            reverse=True,
        )

    def allocation_fairness(self) -> float:
        """0-1 evenness of allocations across arms (1.0 = perfectly even).

        Watched because a monopolising arm is exactly what the brief forbids.
        """
        counts = np.array([a.allocations for a in self.arms.values()], dtype=float)
        if counts.sum() <= 0:
            return 1.0
        shares = counts / counts.sum()
        concentration = float(np.sum(shares**2))
        best = 1.0 / len(counts)
        return float(clamp(safe_div(best, concentration), 0.0, 1.0))
