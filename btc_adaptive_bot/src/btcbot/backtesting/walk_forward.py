"""Walk-forward analysis and cost-stress testing (Layer 1).

Walk-forward is the honest version of a backtest: repeatedly fit or select on
one period, then measure on the period immediately after it, and never let the
two overlap. What matters is not the average result but the **consistency** —
a strategy profitable in 6 of 6 windows is evidence; one that made everything in
window 3 is an artefact.

Cost stress runs the same out-of-sample data at 2x and 4x frictions. A strategy
whose edge evaporates when fees double never had much of one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..config.schema import BacktestingConfig, RegimeConfig
from ..exchange.models import Candle
from ..learning.metrics import StrategyMetrics, compute_metrics
from ..strategies.base import Strategy
from ..utils.logging import get_logger
from ..utils.numeric import safe_div
from .data_split import DataSplit, build_walk_forward_windows, split_candles
from .engine import Backtester
from .execution_model import ExecutionCosts

log = get_logger(__name__)


@dataclass(slots=True)
class WindowResult:
    label: str
    metrics: StrategyMetrics
    start_ts: str
    end_ts: str
    trade_count: int

    @property
    def profitable(self) -> bool:
        return self.metrics.net_pnl > 0


@dataclass(slots=True)
class WalkForwardReport:
    """Aggregate evidence for one strategy from Layer 1."""

    strategy_id: str
    strategy_version: str
    windows: list[WindowResult] = field(default_factory=list)
    consistency: float = 0.0
    mean_expectancy_r: float = 0.0
    expectancy_stability: float = 0.0
    total_trades: int = 0
    profitable_windows: int = 0
    stress_results: dict[float, StrategyMetrics] = field(default_factory=dict)
    cost_robustness: float = 0.0
    period_concentration: float = 0.0

    def describe(self) -> str:
        return (
            f"{self.strategy_id}: {self.profitable_windows}/{len(self.windows)} profitable "
            f"windows, mean expectancy {self.mean_expectancy_r:+.3f}R, "
            f"consistency {self.consistency:.2f}, cost robustness {self.cost_robustness:.2f}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "window_count": len(self.windows),
            "profitable_windows": self.profitable_windows,
            "consistency": self.consistency,
            "mean_expectancy_r": self.mean_expectancy_r,
            "expectancy_stability": self.expectancy_stability,
            "total_trades": self.total_trades,
            "cost_robustness": self.cost_robustness,
            "period_concentration": self.period_concentration,
            "stress": {
                str(mult): {
                    "expectancy_r": m.expectancy_r,
                    "profit_factor": m.profit_factor,
                    "net_pnl": m.net_pnl,
                    "trades": m.total_trades,
                }
                for mult, m in self.stress_results.items()
            },
        }


class WalkForwardAnalyzer:
    """Runs walk-forward windows and stress tests for a strategy."""

    def __init__(self, config: BacktestingConfig, regime_config: RegimeConfig) -> None:
        self.config = config
        self.regime_config = regime_config
        self.costs = ExecutionCosts(
            fee_rate_taker=config.fee_rate_taker,
            fee_rate_maker=config.fee_rate_maker,
            slippage_bps=config.slippage_bps,
            spread_bps=config.spread_bps,
            latency_ms=config.latency_ms,
            partial_fill_probability=config.partial_fill_probability,
            funding_rate_8h=config.funding_rate_8h,
        )

    def _backtester(self) -> Backtester:
        return Backtester(costs=self.costs, regime_config=self.regime_config)

    def run(
        self,
        strategy: Strategy,
        candles: dict[str, list[Candle]],
        *,
        symbol: str = "BTCUSDT",
    ) -> WalkForwardReport:
        """Full Layer-1 evaluation: walk-forward windows plus cost stress."""
        report = WalkForwardReport(strategy_id=strategy.id, strategy_version=strategy.version)
        primary = candles.get(strategy.primary_timeframe, [])
        if len(primary) < strategy.min_bars * 2:
            return report

        windows = build_walk_forward_windows(
            primary,
            windows=self.config.walk_forward_windows,
            mode=self.config.walk_forward_mode,
            embargo_bars=self.config.embargo_bars,
        )
        if not windows:
            return report

        backtester = self._backtester()
        expectancies: list[float] = []
        window_pnls: list[float] = []

        for window in windows:
            # Only the test segment is evaluated. Context timeframes are trimmed
            # to the test window's end so no future context leaks in.
            segment = {strategy.primary_timeframe: window.test.candles}
            for timeframe, series in candles.items():
                if timeframe == strategy.primary_timeframe:
                    continue
                segment[timeframe] = [c for c in series if c.open_ms <= window.test.end_ms]

            result = backtester.run(strategy, segment, symbol=symbol)
            metrics = compute_metrics(
                result.trade_dicts(),
                strategy_id=strategy.id,
                layer="walkforward",
                bootstrap_samples=max(200, self.config.min_trades_for_confidence * 10),
            )
            report.windows.append(
                WindowResult(
                    label=window.label,
                    metrics=metrics,
                    start_ts=window.test.start_iso,
                    end_ts=window.test.end_iso,
                    trade_count=metrics.total_trades,
                )
            )
            if metrics.total_trades > 0:
                expectancies.append(metrics.expectancy_r)
                window_pnls.append(metrics.net_pnl)

        report.total_trades = sum(w.trade_count for w in report.windows)
        report.profitable_windows = sum(1 for w in report.windows if w.profitable)
        evaluated = [w for w in report.windows if w.trade_count > 0]
        report.consistency = safe_div(report.profitable_windows, len(evaluated)) if evaluated else 0.0

        if expectancies:
            report.mean_expectancy_r = float(np.mean(expectancies))
            spread = float(np.std(expectancies))
            # Stability: 1.0 when every window agrees, → 0 as they disagree.
            report.expectancy_stability = float(
                1.0 / (1.0 + spread / max(0.05, abs(report.mean_expectancy_r)))
            )

        # Concentration: did one window produce most of the profit?
        positives = [p for p in window_pnls if p > 0]
        if positives:
            report.period_concentration = safe_div(max(positives), sum(positives))

        report.stress_results = self._stress_test(strategy, candles, symbol=symbol)
        report.cost_robustness = self._cost_robustness(report.stress_results)
        return report

    def _stress_test(
        self, strategy: Strategy, candles: dict[str, list[Candle]], *, symbol: str
    ) -> dict[float, StrategyMetrics]:
        """Run out-of-sample data at each configured friction multiplier."""
        primary = candles.get(strategy.primary_timeframe, [])
        if len(primary) < strategy.min_bars * 2:
            return {}

        try:
            split = split_candles(
                primary,
                train_fraction=self.config.train_fraction,
                validation_fraction=self.config.validation_fraction,
                embargo_bars=self.config.embargo_bars,
            )
        except Exception as exc:  # noqa: BLE001 - split config issues must not abort research
            log.debug("BACKTEST", f"Could not split data for {strategy.id}: {exc}")
            return {}

        oos = split.out_of_sample.candles
        if len(oos) < strategy.min_bars:
            return {}

        segment = {strategy.primary_timeframe: oos}
        for timeframe, series in candles.items():
            if timeframe == strategy.primary_timeframe:
                continue
            segment[timeframe] = [c for c in series if c.open_ms <= split.out_of_sample.end_ms]

        backtester = self._backtester()
        results: dict[float, StrategyMetrics] = {}
        for multiplier in self.config.stress_multipliers:
            result = backtester.run(strategy, segment, symbol=symbol, stress_multiplier=multiplier)
            results[float(multiplier)] = compute_metrics(
                result.trade_dicts(), strategy_id=strategy.id, layer=f"stress_{multiplier}x"
            )
        return results

    @staticmethod
    def _cost_robustness(stress: dict[float, StrategyMetrics]) -> float:
        """0-1 score for how well the edge survives higher frictions.

        Full credit requires the *worst* stressed case to remain profitable;
        partial credit is awarded for retaining most of the baseline expectancy.
        """
        if not stress or 1.0 not in stress:
            return 0.0
        baseline = stress[1.0]
        if baseline.total_trades == 0 or baseline.expectancy_r <= 0:
            return 0.0

        stressed = [(m, s) for m, s in stress.items() if m > 1.0 and s.total_trades > 0]
        if not stressed:
            return 0.5  # untested, so neither rewarded nor punished

        scores = []
        for multiplier, metrics in stressed:
            retained = safe_div(metrics.expectancy_r, baseline.expectancy_r)
            still_positive = metrics.expectancy_r > 0
            score = max(0.0, min(1.0, retained))
            if not still_positive:
                score *= 0.25
            # Surviving a bigger multiplier is worth more.
            scores.append(score * min(1.0, multiplier / 2.0))
        return float(np.clip(np.mean(scores), 0.0, 1.0))


def evaluate_segments(
    strategy: Strategy,
    candles: dict[str, list[Candle]],
    split: DataSplit,
    *,
    config: BacktestingConfig,
    regime_config: RegimeConfig,
    symbol: str = "BTCUSDT",
) -> dict[str, StrategyMetrics]:
    """Evaluate a strategy separately on train, validation, and out-of-sample.

    A large gap between training and out-of-sample results is the clearest
    single indicator of overfitting, and the scorer penalises it directly.
    """
    costs = ExecutionCosts(
        fee_rate_taker=config.fee_rate_taker,
        fee_rate_maker=config.fee_rate_maker,
        slippage_bps=config.slippage_bps,
        spread_bps=config.spread_bps,
        latency_ms=config.latency_ms,
        partial_fill_probability=config.partial_fill_probability,
        funding_rate_8h=config.funding_rate_8h,
    )
    backtester = Backtester(costs=costs, regime_config=regime_config)

    output: dict[str, StrategyMetrics] = {}
    for name, segment in split.segments().items():
        if len(segment) < strategy.min_bars:
            output[name] = StrategyMetrics(strategy_id=strategy.id, layer=name)
            continue
        payload = {strategy.primary_timeframe: segment.candles}
        for timeframe, series in candles.items():
            if timeframe == strategy.primary_timeframe:
                continue
            payload[timeframe] = [c for c in series if c.open_ms <= segment.end_ms]
        result = backtester.run(strategy, payload, symbol=symbol)
        output[name] = compute_metrics(
            result.trade_dicts(), strategy_id=strategy.id, layer=name
        )
    return output
