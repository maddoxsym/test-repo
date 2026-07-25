"""Loss attribution and confidence calibration — "learning from mistakes", done properly.

Two disciplines are enforced here:

* **A single loss is not a mistake.** Every attribution requires a
  statistically meaningful sample before it can influence anything. A strategy
  with positive expectancy is *supposed* to lose regularly.
* **Attribution is diagnostic, not automatic.** This module produces findings;
  changing anything still requires a candidate to pass held-out validation.

The questions asked are exactly those in the brief: was the regime unsuitable,
was volatility wrong, was the stop too tight, the target unrealistic, did costs
eat the edge, was the signal late, was news risk elevated, did a filter fail,
did sizing amplify it, is performance deteriorating — or was this simply normal
variance inside a positive-expectancy process?
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..config.schema import LearningConfig
from ..database.repositories import PerformanceRepository
from ..utils.logging import get_logger
from ..utils.numeric import clamp, safe_div
from ..utils.stats import welch_t_statistic
from ..utils.timeutil import iso, now_utc
from .metrics import StrategyMetrics

log = get_logger(__name__)

MIN_SAMPLE_FOR_ATTRIBUTION = 15


@dataclass(slots=True)
class Finding:
    """One diagnostic conclusion, with the evidence behind it."""

    code: str
    severity: str          # info | warning | critical
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    suggested_action: str = ""

    def describe(self) -> str:
        return f"[{self.severity.upper()}] {self.message}"


@dataclass(slots=True)
class LossAnalysis:
    strategy_id: str
    sample_size: int
    findings: list[Finding] = field(default_factory=list)
    verdict: str = "insufficient_data"

    @property
    def has_actionable_findings(self) -> bool:
        return any(f.severity in {"warning", "critical"} for f in self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "sample_size": self.sample_size,
            "verdict": self.verdict,
            "findings": [
                {
                    "code": f.code,
                    "severity": f.severity,
                    "message": f.message,
                    "evidence": f.evidence,
                    "action": f.suggested_action,
                }
                for f in self.findings
            ],
        }


class LossAnalyzer:
    """Diagnoses why a strategy is losing — when there is enough data to say."""

    def __init__(self, config: LearningConfig) -> None:
        self.config = config

    def analyse(
        self, strategy_id: str, trades: list[dict[str, Any]], metrics: StrategyMetrics
    ) -> LossAnalysis:
        losses = [t for t in trades if float(t.get("pnl") or 0.0) < 0]
        analysis = LossAnalysis(strategy_id=strategy_id, sample_size=len(trades))

        if len(trades) < MIN_SAMPLE_FOR_ATTRIBUTION:
            analysis.verdict = "insufficient_data"
            analysis.findings.append(
                Finding(
                    "sample_too_small",
                    "info",
                    f"only {len(trades)} trades — too few to attribute losses to anything. "
                    "Continuing to collect data.",
                    {"trades": len(trades), "required": MIN_SAMPLE_FOR_ATTRIBUTION},
                )
            )
            return analysis

        # The most important check: is this just normal variance?
        if metrics.expectancy_r > 0 and metrics.expectancy_r_ci_low > -0.05:
            analysis.verdict = "normal_variance_positive_expectancy"
            analysis.findings.append(
                Finding(
                    "normal_variance",
                    "info",
                    f"{len(losses)} losses across {len(trades)} trades, but expectancy is "
                    f"{metrics.expectancy_r:+.3f}R with a confidence interval of "
                    f"[{metrics.expectancy_r_ci_low:+.3f}, {metrics.expectancy_r_ci_high:+.3f}]. "
                    "These losses are the expected cost of a positive-expectancy process.",
                    {"expectancy_r": metrics.expectancy_r},
                    "no action — do not tune on ordinary losses",
                )
            )
        else:
            analysis.verdict = "underperforming"

        analysis.findings.extend(self._regime_findings(metrics))
        analysis.findings.extend(self._volatility_findings(metrics, trades))
        analysis.findings.extend(self._stop_findings(trades, metrics))
        analysis.findings.extend(self._target_findings(trades, metrics))
        analysis.findings.extend(self._cost_findings(metrics))
        analysis.findings.extend(self._news_findings(metrics))
        analysis.findings.extend(self._sizing_findings(trades))
        analysis.findings.extend(self._deterioration_findings(trades))
        return analysis

    # --- individual diagnostics -------------------------------------------

    def _regime_findings(self, metrics: StrategyMetrics) -> list[Finding]:
        findings = []
        for regime, stats in metrics.by_regime.items():
            if stats.get("trades", 0) < 8:
                continue
            if stats.get("expectancy_r", 0.0) < -0.15:
                findings.append(
                    Finding(
                        "unsuitable_regime",
                        "warning",
                        f"loses consistently in {regime}: {stats['expectancy_r']:+.3f}R over "
                        f"{int(stats['trades'])} trades",
                        {"regime": regime, **stats},
                        f"consider excluding {regime} from this strategy's allowed regimes",
                    )
                )
        return findings

    def _volatility_findings(
        self, metrics: StrategyMetrics, trades: list[dict[str, Any]]
    ) -> list[Finding]:
        findings = []
        for state, stats in metrics.by_volatility_state.items():
            if stats.get("trades", 0) < 8:
                continue
            if stats.get("expectancy_r", 0.0) < -0.15:
                findings.append(
                    Finding(
                        "volatility_mismatch",
                        "warning",
                        f"performs poorly in {state} volatility: "
                        f"{stats['expectancy_r']:+.3f}R over {int(stats['trades'])} trades",
                        {"volatility_state": state, **stats},
                        "add a volatility filter or adjust stop distance for this state",
                    )
                )
        return findings

    def _stop_findings(
        self, trades: list[dict[str, Any]], metrics: StrategyMetrics
    ) -> list[Finding]:
        """Detect stops that are systematically too tight.

        The signature: stopped-out trades that had already travelled a long way
        in the intended direction first. That means the idea was right and the
        stop was in the wrong place — not that the entry was bad.
        """
        stopped = [t for t in trades if str(t.get("exit_reason", "")).startswith("stop")]
        if len(stopped) < 8:
            return []

        near_misses = 0
        for trade in stopped:
            mfe = float(trade.get("mfe") or 0.0)
            notional = abs(float(trade.get("notional") or 0.0))
            risk = safe_div(abs(float(trade.get("pnl") or 0.0)), 1.0)
            if notional <= 0 or risk <= 0:
                continue
            if mfe > risk * 0.75:
                near_misses += 1

        ratio = safe_div(near_misses, len(stopped))
        if ratio > 0.45:
            return [
                Finding(
                    "stop_too_tight",
                    "warning",
                    f"{ratio * 100:.0f}% of stopped-out trades had first moved most of the way "
                    f"to their target ({near_misses}/{len(stopped)}). The direction was right; "
                    "the stop was too close.",
                    {"near_miss_ratio": ratio, "stopped_trades": len(stopped)},
                    "widen the ATR stop multiple and re-validate on held-out data",
                )
            ]
        return []

    def _target_findings(
        self, trades: list[dict[str, Any]], metrics: StrategyMetrics
    ) -> list[Finding]:
        """Detect unrealistic targets via poor MFE capture."""
        if metrics.total_trades < 15:
            return []
        if 0 < metrics.mfe_capture < 0.25 and metrics.average_mfe > 0:
            return [
                Finding(
                    "target_unrealistic",
                    "warning",
                    f"only {metrics.mfe_capture * 100:.0f}% of available favourable movement is "
                    "being captured — targets are likely too far away",
                    {"mfe_capture": metrics.mfe_capture, "avg_mfe": metrics.average_mfe},
                    "reduce the R:R target or add a partial exit",
                )
            ]
        return []

    def _cost_findings(self, metrics: StrategyMetrics) -> list[Finding]:
        """Detect an edge being consumed by fees and slippage."""
        gross = metrics.net_pnl + metrics.total_fees + metrics.total_slippage
        if metrics.total_trades < 15 or gross <= 0:
            return []
        cost_share = safe_div(metrics.total_fees + metrics.total_slippage, gross)
        if cost_share > 0.5:
            return [
                Finding(
                    "costs_dominate",
                    "critical",
                    f"fees and slippage consume {cost_share * 100:.0f}% of gross profit "
                    f"(${metrics.total_fees + metrics.total_slippage:,.2f} of ${gross:,.2f}). "
                    "The edge does not survive realistic execution.",
                    {"cost_share": cost_share, "fees": metrics.total_fees},
                    "increase the minimum move targeted, or retire the strategy",
                )
            ]
        return []

    def _news_findings(self, metrics: StrategyMetrics) -> list[Finding]:
        elevated = metrics.by_news_state.get("elevated") or metrics.by_news_state.get("blocked")
        calm = metrics.by_news_state.get("calm")
        if not elevated or not calm:
            return []
        if elevated.get("trades", 0) < 8 or calm.get("trades", 0) < 8:
            return []
        gap = elevated.get("expectancy_r", 0.0) - calm.get("expectancy_r", 0.0)
        if gap < -0.2:
            return [
                Finding(
                    "news_risk_elevated",
                    "warning",
                    f"performs {abs(gap):.2f}R worse during elevated-news windows "
                    f"({elevated['expectancy_r']:+.3f}R vs {calm['expectancy_r']:+.3f}R calm)",
                    {"elevated": elevated, "calm": calm},
                    "increase the news gate's influence for this strategy",
                )
            ]
        return []

    def _sizing_findings(self, trades: list[dict[str, Any]]) -> list[Finding]:
        """Detect sizing amplifying losses — bigger positions doing worse."""
        sized = [
            (float(t.get("notional") or 0.0), float(t.get("r_multiple") or 0.0))
            for t in trades
            if t.get("notional")
        ]
        if len(sized) < 20:
            return []
        notionals = np.array([n for n, _ in sized])
        r_values = np.array([r for _, r in sized])
        median = float(np.median(notionals))
        large = r_values[notionals > median]
        small = r_values[notionals <= median]
        if large.size < 8 or small.size < 8:
            return []
        if float(large.mean()) < float(small.mean()) - 0.25:
            t_stat = welch_t_statistic(small.tolist(), large.tolist())
            if abs(t_stat) > 1.5:
                return [
                    Finding(
                        "sizing_amplifies_losses",
                        "warning",
                        f"larger positions perform worse ({large.mean():+.3f}R vs "
                        f"{small.mean():+.3f}R for smaller ones, t={t_stat:.2f}) — the "
                        "confidence signal driving size is not predictive",
                        {"large_mean_r": float(large.mean()), "small_mean_r": float(small.mean())},
                        "recalibrate confidence before it feeds position sizing",
                    )
                ]
        return []

    def _deterioration_findings(self, trades: list[dict[str, Any]]) -> list[Finding]:
        """Detect statistically meaningful decay between older and newer trades."""
        if len(trades) < self.config.min_trades_for_parameter_update:
            return []
        ordered = sorted(trades, key=lambda t: str(t.get("exit_ts_utc") or ""))
        midpoint = len(ordered) // 2
        older = [float(t.get("r_multiple") or 0.0) for t in ordered[:midpoint]]
        newer = [float(t.get("r_multiple") or 0.0) for t in ordered[midpoint:]]
        if len(older) < 10 or len(newer) < 10:
            return []

        t_stat = welch_t_statistic(older, newer)
        drop = float(np.mean(older) - np.mean(newer))
        # Require both a meaningful drop and statistical separation, so ordinary
        # noise does not read as decay.
        if drop > 0.25 and t_stat > 1.96:
            return [
                Finding(
                    "performance_deteriorating",
                    "critical",
                    f"expectancy fell from {np.mean(older):+.3f}R to {np.mean(newer):+.3f}R "
                    f"(t={t_stat:.2f}) — a statistically meaningful deterioration",
                    {"older_mean": float(np.mean(older)), "newer_mean": float(np.mean(newer))},
                    "reduce allocation and investigate before promoting anything",
                )
            ]
        return []


class ConfidenceCalibrator:
    """Measures whether stated confidence predicts realised outcomes.

    If a strategy's 0.8-confidence signals win no more often than its
    0.5-confidence ones, its confidence is noise and must not be scaling
    position size. This produces a multiplier that damps miscalibrated
    confidence.
    """

    def __init__(self, config: LearningConfig, repository: PerformanceRepository) -> None:
        self.config = config
        self.repo = repository

    def calibrate(
        self, strategy_id: str, trades: list[dict[str, Any]], *, experiment_id: str
    ) -> dict[str, Any]:
        usable = [
            t
            for t in trades
            if t.get("confidence") is not None and t.get("pnl") is not None
        ]
        if len(usable) < self.config.min_trades_for_high_confidence:
            return {"strategy_id": strategy_id, "calibrated": False, "reason": "insufficient sample"}

        bins = self.config.calibration_bins
        edges = np.linspace(0.0, 1.0, bins + 1)
        buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for trade in usable:
            confidence = clamp(float(trade["confidence"]), 0.0, 0.999)
            buckets[int(confidence * bins)].append(trade)

        errors: list[float] = []
        rows: list[dict[str, Any]] = []
        for index, group in sorted(buckets.items()):
            if len(group) < 4:
                continue
            predicted = float(np.mean([float(t["confidence"]) for t in group]))
            realised = float(np.mean([1.0 if float(t["pnl"]) > 0 else 0.0 for t in group]))
            error = abs(predicted - realised)
            errors.append(error)
            record = {
                "experiment_id": experiment_id,
                "strategy_id": strategy_id,
                "ts_utc": iso(now_utc()),
                "bucket_low": float(edges[index]),
                "bucket_high": float(edges[min(index + 1, bins)]),
                "predicted_rate": predicted,
                "realized_rate": realised,
                "sample_size": len(group),
                "calibration_error": error,
            }
            self.repo.record_calibration(record)
            rows.append(record)

        if not errors:
            return {"strategy_id": strategy_id, "calibrated": False, "reason": "no populated buckets"}

        mean_error = float(np.mean(errors))
        # Large calibration error → shrink confidence's influence toward neutral.
        multiplier = float(clamp(1.0 - mean_error, 0.5, 1.0))
        if mean_error > 0.25:
            log.info(
                "LEARNING",
                f"{strategy_id} confidence is poorly calibrated (mean error {mean_error:.2f}); "
                f"its influence on sizing is damped to ×{multiplier:.2f}",
            )
        return {
            "strategy_id": strategy_id,
            "calibrated": True,
            "mean_error": mean_error,
            "multiplier": multiplier,
            "buckets": rows,
        }
