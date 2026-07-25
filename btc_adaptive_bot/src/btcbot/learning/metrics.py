"""Performance metrics.

Every number the champion decision depends on is computed here from a plain list
of trade dictionaries, so any result can be reproduced from the exported CSV
without running the system. Trades from the backtester, the shadow engine, and
the live demo layer all share the same shape, which is what lets the three
evidence layers be compared on equal terms.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from ..utils.numeric import safe_div
from ..utils.stats import (
    bootstrap_mean_ci,
    concentration_ratio,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    wilson_interval,
)

# Trades are the unit of observation, so ratios are annualised on a per-trade
# basis using a nominal 365 periods. This is a scaling convention, applied
# identically to every strategy — it is used for ranking, not for forecasting.
PERIODS_PER_YEAR = 365.0


@dataclass(slots=True)
class StrategyMetrics:
    """The full metric set for one strategy on one evidence layer."""

    strategy_id: str = ""
    layer: str = ""

    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate: float = 0.0
    win_rate_ci_low: float = 0.0
    win_rate_ci_high: float = 0.0

    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    return_pct: float = 0.0
    profit_factor: float = 0.0

    average_win: float = 0.0
    average_loss: float = 0.0
    win_loss_ratio: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    expectancy: float = 0.0
    expectancy_r: float = 0.0
    expectancy_r_ci_low: float = 0.0
    expectancy_r_ci_high: float = 0.0
    average_r: float = 0.0
    median_r: float = 0.0

    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    recovery_factor: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0

    average_holding_seconds: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    average_mfe: float = 0.0
    average_mae: float = 0.0
    mfe_capture: float = 0.0

    longest_winning_streak: int = 0
    longest_losing_streak: int = 0
    single_winner_concentration: float = 0.0

    initial_equity: float = 10_000.0
    final_equity: float = 10_000.0

    by_timeframe: dict[str, dict[str, float]] = field(default_factory=dict)
    by_regime: dict[str, dict[str, float]] = field(default_factory=dict)
    by_hour: dict[str, dict[str, float]] = field(default_factory=dict)
    by_weekday: dict[str, dict[str, float]] = field(default_factory=dict)
    by_volatility_state: dict[str, dict[str, float]] = field(default_factory=dict)
    by_news_state: dict[str, dict[str, float]] = field(default_factory=dict)
    by_exit_reason: dict[str, dict[str, float]] = field(default_factory=dict)
    by_direction: dict[str, dict[str, float]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def best_regime(self) -> str:
        return _best_bucket(self.by_regime, "expectancy_r")

    @property
    def worst_regime(self) -> str:
        return _worst_bucket(self.by_regime, "expectancy_r")

    @property
    def best_timeframe(self) -> str:
        return _best_bucket(self.by_timeframe, "expectancy_r")

    @property
    def has_meaningful_sample(self) -> bool:
        return self.total_trades >= 10

    def summary_line(self) -> str:
        return (
            f"{self.strategy_id}: {self.total_trades} trades, "
            f"{self.win_rate * 100:.1f}% win, PF {self.profit_factor:.2f}, "
            f"expectancy {self.expectancy_r:+.3f}R, maxDD {self.max_drawdown_pct * 100:.1f}%"
        )


def compute_metrics(
    trades: list[dict[str, Any]],
    *,
    strategy_id: str = "",
    layer: str = "",
    initial_equity: float = 10_000.0,
    bootstrap_samples: int = 2000,
    bootstrap_confidence: float = 0.9,
) -> StrategyMetrics:
    """Compute the full metric set for a list of closed trades."""
    metrics = StrategyMetrics(
        strategy_id=strategy_id,
        layer=layer,
        initial_equity=initial_equity,
        final_equity=initial_equity,
    )
    closed = [t for t in trades if t.get("pnl") is not None]
    if not closed:
        return metrics

    pnls = np.array([float(t["pnl"]) for t in closed], dtype=float)
    r_multiples = np.array(
        [float(t.get("r_multiple") or 0.0) for t in closed], dtype=float
    )

    metrics.total_trades = len(closed)
    wins_mask = pnls > 0
    losses_mask = pnls < 0
    metrics.wins = int(np.sum(wins_mask))
    metrics.losses = int(np.sum(losses_mask))
    metrics.breakeven = metrics.total_trades - metrics.wins - metrics.losses
    metrics.win_rate = safe_div(metrics.wins, metrics.total_trades)
    metrics.win_rate_ci_low, metrics.win_rate_ci_high = wilson_interval(
        metrics.wins, metrics.total_trades
    )

    metrics.gross_profit = float(np.sum(pnls[wins_mask])) if metrics.wins else 0.0
    metrics.gross_loss = float(abs(np.sum(pnls[losses_mask]))) if metrics.losses else 0.0
    metrics.net_pnl = float(np.sum(pnls))
    metrics.return_pct = safe_div(metrics.net_pnl, initial_equity)
    # Profit factor with no losses at all is mathematically infinite; report it
    # as a large finite number so it can be ranked, and let the sample-size
    # penalty handle the fact that it is usually a small-sample artefact.
    metrics.profit_factor = (
        safe_div(metrics.gross_profit, metrics.gross_loss)
        if metrics.gross_loss > 0
        else (float(min(metrics.gross_profit, 999.0)) if metrics.gross_profit > 0 else 0.0)
    )

    metrics.average_win = float(np.mean(pnls[wins_mask])) if metrics.wins else 0.0
    metrics.average_loss = float(abs(np.mean(pnls[losses_mask]))) if metrics.losses else 0.0
    metrics.win_loss_ratio = safe_div(metrics.average_win, metrics.average_loss)
    metrics.largest_win = float(np.max(pnls)) if metrics.total_trades else 0.0
    metrics.largest_loss = float(np.min(pnls)) if metrics.total_trades else 0.0

    metrics.expectancy = float(np.mean(pnls))
    metrics.expectancy_r = float(np.mean(r_multiples))
    low, _, high = bootstrap_mean_ci(
        r_multiples.tolist(), samples=bootstrap_samples, confidence=bootstrap_confidence
    )
    metrics.expectancy_r_ci_low = low
    metrics.expectancy_r_ci_high = high
    metrics.average_r = metrics.expectancy_r
    metrics.median_r = float(np.median(r_multiples))

    equity_curve = [initial_equity]
    for pnl in pnls:
        equity_curve.append(equity_curve[-1] + float(pnl))
    metrics.final_equity = equity_curve[-1]
    absolute_dd, fractional_dd = max_drawdown(equity_curve)
    metrics.max_drawdown = absolute_dd
    metrics.max_drawdown_pct = fractional_dd
    metrics.recovery_factor = safe_div(metrics.net_pnl, absolute_dd)

    returns = (pnls / initial_equity).tolist()
    metrics.sharpe_ratio = sharpe_ratio(returns, periods_per_year=PERIODS_PER_YEAR)
    metrics.sortino_ratio = sortino_ratio(returns, periods_per_year=PERIODS_PER_YEAR)
    metrics.calmar_ratio = safe_div(metrics.return_pct, fractional_dd)

    durations = [float(t.get("duration_seconds") or 0.0) for t in closed]
    metrics.average_holding_seconds = float(np.mean(durations)) if durations else 0.0
    metrics.total_fees = float(sum(float(t.get("fees") or 0.0) for t in closed))
    metrics.total_slippage = float(
        sum(float(t.get("slippage_cost") or 0.0) + float(t.get("spread_cost") or 0.0) for t in closed)
    )

    mfes = [float(t.get("mfe") or 0.0) for t in closed]
    maes = [float(t.get("mae") or 0.0) for t in closed]
    metrics.average_mfe = float(np.mean(mfes)) if mfes else 0.0
    metrics.average_mae = float(np.mean(maes)) if maes else 0.0
    # How much of the best available excursion the exits actually captured —
    # low capture with positive MFE points at targets that are too far away.
    total_mfe = float(sum(m for m in mfes if m > 0))
    metrics.mfe_capture = safe_div(max(0.0, metrics.net_pnl), total_mfe)

    metrics.longest_winning_streak, metrics.longest_losing_streak = _streaks(pnls)
    metrics.single_winner_concentration = concentration_ratio(pnls.tolist())

    metrics.by_timeframe = _bucket(closed, "timeframe")
    metrics.by_regime = _bucket(closed, "regime", fallback_key="entry_regime")
    metrics.by_hour = _bucket(closed, "hour_utc")
    metrics.by_weekday = _bucket(closed, "weekday_utc")
    metrics.by_volatility_state = _bucket(closed, "volatility_state")
    metrics.by_news_state = _bucket(closed, "news_state")
    metrics.by_exit_reason = _bucket(closed, "exit_reason")
    metrics.by_direction = _bucket(closed, "direction")

    return metrics


def _streaks(pnls: np.ndarray) -> tuple[int, int]:
    """Longest winning and losing streaks."""
    best_win = best_loss = current_win = current_loss = 0
    for pnl in pnls:
        if pnl > 0:
            current_win += 1
            current_loss = 0
            best_win = max(best_win, current_win)
        elif pnl < 0:
            current_loss += 1
            current_win = 0
            best_loss = max(best_loss, current_loss)
        else:
            current_win = current_loss = 0
    return best_win, best_loss


def _bucket(
    trades: list[dict[str, Any]], key: str, *, fallback_key: str | None = None
) -> dict[str, dict[str, float]]:
    """Group trades by ``key`` and summarise each group."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        value = trade.get(key)
        if value is None and fallback_key:
            value = trade.get(fallback_key)
        if value is None:
            continue
        groups[str(value)].append(trade)

    summary: dict[str, dict[str, float]] = {}
    for name, group in groups.items():
        pnls = np.array([float(t["pnl"]) for t in group], dtype=float)
        r_values = np.array([float(t.get("r_multiple") or 0.0) for t in group], dtype=float)
        wins = int(np.sum(pnls > 0))
        gross_profit = float(np.sum(pnls[pnls > 0])) if wins else 0.0
        gross_loss = float(abs(np.sum(pnls[pnls < 0])))
        summary[name] = {
            "trades": float(len(group)),
            "wins": float(wins),
            "win_rate": safe_div(wins, len(group)),
            "net_pnl": float(np.sum(pnls)),
            "expectancy_r": float(np.mean(r_values)),
            "profit_factor": safe_div(gross_profit, gross_loss) if gross_loss > 0 else 0.0,
        }
    return summary


def _best_bucket(buckets: dict[str, dict[str, float]], metric: str, *, min_trades: int = 3) -> str:
    eligible = {k: v for k, v in buckets.items() if v.get("trades", 0) >= min_trades}
    if not eligible:
        return "insufficient_data"
    return max(eligible.items(), key=lambda kv: kv[1].get(metric, 0.0))[0]


def _worst_bucket(buckets: dict[str, dict[str, float]], metric: str, *, min_trades: int = 3) -> str:
    eligible = {k: v for k, v in buckets.items() if v.get("trades", 0) >= min_trades}
    if not eligible:
        return "insufficient_data"
    return min(eligible.items(), key=lambda kv: kv[1].get(metric, 0.0))[0]


def regime_expectancy_map(metrics_by_strategy: dict[str, StrategyMetrics]) -> dict[str, dict[str, float]]:
    """``{strategy_id: {regime: expectancy_r}}`` — feeds the regime-adaptive ensemble."""
    return {
        strategy_id: {
            regime: stats.get("expectancy_r", 0.0)
            for regime, stats in metrics.by_regime.items()
            if stats.get("trades", 0) >= 3
        }
        for strategy_id, metrics in metrics_by_strategy.items()
    }


def merge_metrics(parts: list[StrategyMetrics], *, strategy_id: str, layer: str) -> StrategyMetrics:
    """Combine metrics from several windows by weighting on trade count.

    Used for walk-forward aggregates, where each window is an independent
    observation and a window with 5 trades should not count as much as one
    with 80.
    """
    combined = StrategyMetrics(strategy_id=strategy_id, layer=layer)
    populated = [m for m in parts if m.total_trades > 0]
    if not populated:
        return combined

    total = sum(m.total_trades for m in populated)
    combined.total_trades = total
    combined.wins = sum(m.wins for m in populated)
    combined.losses = sum(m.losses for m in populated)
    combined.win_rate = safe_div(combined.wins, total)
    combined.gross_profit = sum(m.gross_profit for m in populated)
    combined.gross_loss = sum(m.gross_loss for m in populated)
    combined.net_pnl = sum(m.net_pnl for m in populated)
    combined.profit_factor = safe_div(combined.gross_profit, combined.gross_loss)
    combined.expectancy_r = safe_div(
        sum(m.expectancy_r * m.total_trades for m in populated), total
    )
    combined.average_r = combined.expectancy_r
    combined.max_drawdown_pct = max(m.max_drawdown_pct for m in populated)
    combined.sharpe_ratio = safe_div(sum(m.sharpe_ratio * m.total_trades for m in populated), total)
    combined.sortino_ratio = safe_div(sum(m.sortino_ratio * m.total_trades for m in populated), total)
    combined.total_fees = sum(m.total_fees for m in populated)
    combined.total_slippage = sum(m.total_slippage for m in populated)
    combined.return_pct = safe_div(sum(m.return_pct for m in populated), len(populated))
    return combined
