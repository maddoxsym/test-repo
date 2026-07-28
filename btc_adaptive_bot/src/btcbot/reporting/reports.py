"""Report generation: daily, strategy, execution, learning, and the Day-14 report.

Reports are written as Markdown (readable in any editor), HTML (readable in a
browser), and CSV (loadable anywhere). The Day-14 report is the deliverable of
the whole experiment and contains every column the brief specifies, per strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config.schema import AppConfig
from ..database.repositories import Repositories
from ..learning.metrics import compute_metrics
from ..utils.logging import get_logger
from ..utils.numeric import safe_div
from ..utils.timeutil import humanize_duration, iso, now_utc
from .export import write_metrics_csv
from .templates import render_final_report_html

if TYPE_CHECKING:
    from ..app.experiment import ExperimentState
    from ..scoring.champion import ChampionSelection
    from ..scoring.scorer import ScoreBreakdown, StrategyEvidence
    from ..strategies.registry import StrategyRegistry

log = get_logger(__name__)


@dataclass(slots=True)
class ReportPaths:
    markdown: Path | None = None
    html: Path | None = None
    csv: Path | None = None

    def all(self) -> list[Path]:
        return [p for p in (self.markdown, self.html, self.csv) if p is not None]


class ReportGenerator:
    """Builds every report the system produces."""

    def __init__(
        self,
        repositories: Repositories,
        config: AppConfig,
        *,
        registry: StrategyRegistry | None = None,
    ) -> None:
        self.repos = repositories
        self.config = config
        self.registry = registry
        self.output_dir = Path(config.reporting.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # --- daily ------------------------------------------------------------

    def daily_report(self, state: ExperimentState) -> Path:
        """Write the day's summary report."""
        experiment_id = state.experiment_id
        shadow_trades = self.repos.shadow.closed_trades(experiment_id=experiment_id)
        demo_positions = self.repos.positions.closed_positions(experiment_id)
        outages = self.repos.system.outages(experiment_id)
        news = self.repos.news.recent(15)
        candidates = self.repos.candidates.recent(10)
        balance = self.repos.market.latest_balance(experiment_id)

        by_strategy: dict[str, list[dict[str, Any]]] = {}
        for trade in shadow_trades:
            by_strategy.setdefault(trade["strategy_id"], []).append(trade)

        ranked = sorted(
            (
                (sid, compute_metrics(trades, strategy_id=sid, layer="shadow",
                                      initial_equity=self.config.shadow.initial_equity,
                                      bootstrap_samples=300))
                for sid, trades in by_strategy.items()
            ),
            key=lambda item: item[1].expectancy_r,
            reverse=True,
        )

        demo_pnl = sum(float(p.get("realized_pnl") or 0.0) for p in demo_positions)
        demo_wins = sum(1 for p in demo_positions if float(p.get("realized_pnl") or 0.0) > 0)
        total_outage = sum(int(o.get("duration_seconds") or 0) for o in outages)

        lines = [
            f"# Daily Report — Day {state.day} of {state.duration_days}",
            "",
            f"*Generated {iso(now_utc())} · experiment `{experiment_id}`*",
            "",
            "## Experiment",
            "",
            f"- **Progress:** {state.progress_pct:.1f}%",
            f"- **Elapsed:** {humanize_duration(state.elapsed)}",
            f"- **Remaining:** {humanize_duration(state.remaining)}",
            f"- **Scheduled end:** {iso(state.scheduled_end)}",
            "",
            "## OKX Demo (Layer 3)",
            "",
            f"- **Current equity:** ${float(balance['total_equity']):,.2f}" if balance else "- Balance unavailable",
            f"- **Starting equity:** ${state.starting_demo_equity:,.2f}",
            f"- **Realised demo PnL:** ${demo_pnl:+,.2f}",
            f"- **Demo trades closed:** {len(demo_positions)} ({demo_wins} winners)",
            "",
            "## Shadow research (Layer 2)",
            "",
            f"- **Shadow trades closed:** {len(shadow_trades)}",
            f"- **Strategies with trades:** {len(by_strategy)}",
            "",
        ]

        if ranked:
            lines.extend(
                [
                    "### Leaders",
                    "",
                    "| Strategy | Trades | Win rate | Expectancy (R) | Net PnL | Max DD |",
                    "|---|---:|---:|---:|---:|---:|",
                ]
            )
            for sid, metrics in ranked[:5]:
                lines.append(
                    f"| {sid} | {metrics.total_trades} | {metrics.win_rate * 100:.1f}% | "
                    f"{metrics.expectancy_r:+.3f} | ${metrics.net_pnl:+,.2f} | "
                    f"{metrics.max_drawdown_pct * 100:.1f}% |"
                )
            lines.extend(["", "### Laggards", "",
                          "| Strategy | Trades | Win rate | Expectancy (R) | Net PnL |",
                          "|---|---:|---:|---:|---:|"])
            for sid, metrics in ranked[-5:][::-1]:
                lines.append(
                    f"| {sid} | {metrics.total_trades} | {metrics.win_rate * 100:.1f}% | "
                    f"{metrics.expectancy_r:+.3f} | ${metrics.net_pnl:+,.2f} |"
                )
            lines.append("")

        worst_dd = max((m.max_drawdown_pct for _, m in ranked), default=0.0)
        lines.extend(
            [
                "## System health",
                "",
                f"- **Outages recorded:** {len(outages)} "
                f"(total {humanize_duration(total_outage)})",
                f"- **Worst strategy drawdown:** {worst_dd * 100:.1f}%",
                f"- **Outage policy:** {state.outage_policy}",
                "",
                "## News",
                "",
            ]
        )
        if news:
            for event in news[:8]:
                lines.append(
                    f"- `{event['impact']}` **{event['category']}** — {event['headline'][:130]}"
                )
        else:
            lines.append("- No news events recorded (news may be degraded).")

        lines.extend(["", "## Learning", ""])
        if candidates:
            for candidate in candidates[:8]:
                lines.append(
                    f"- `{candidate['status']}` {candidate['strategy_id']} "
                    f"{candidate['base_version']} → {candidate['candidate_version']}: "
                    f"{(candidate.get('decision_reason') or candidate['proposal_basis'])[:150]}"
                )
        else:
            lines.append("- No parameter candidates proposed yet.")

        if ranked:
            leader_id, leader_metrics = ranked[0]
            lines.extend(
                [
                    "",
                    "## Current champion candidate",
                    "",
                    f"**{leader_id}** — expectancy {leader_metrics.expectancy_r:+.3f}R over "
                    f"{leader_metrics.total_trades} shadow trades. "
                    "This is an interim leader, not a validated champion; the champion is "
                    "chosen at Day 14 from all three evidence layers.",
                ]
            )

        path = self.output_dir / f"daily_report_day{state.day:02d}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        log.info("REPORT", f"Daily report written: {path.name}")
        return path

    # --- strategy / execution / learning ----------------------------------

    def strategy_report(self, experiment_id: str) -> Path:
        """Per-strategy performance, including regime and time breakdowns."""
        shadow_trades = self.repos.shadow.closed_trades(experiment_id=experiment_id)
        by_strategy: dict[str, list[dict[str, Any]]] = {}
        for trade in shadow_trades:
            by_strategy.setdefault(trade["strategy_id"], []).append(trade)

        lines = ["# Strategy Report", "", f"*Generated {iso(now_utc())}*", ""]
        if not by_strategy:
            lines.append("No closed shadow trades yet.")
        for sid, trades in sorted(by_strategy.items()):
            metrics = compute_metrics(
                trades, strategy_id=sid, layer="shadow",
                initial_equity=self.config.shadow.initial_equity, bootstrap_samples=300,
            )
            lines.extend(
                [
                    f"## {sid}",
                    "",
                    f"- Trades: {metrics.total_trades} ({metrics.wins}W / {metrics.losses}L)",
                    f"- Win rate: {metrics.win_rate * 100:.1f}% "
                    f"(95% CI {metrics.win_rate_ci_low * 100:.1f}–{metrics.win_rate_ci_high * 100:.1f}%)",
                    f"- Expectancy: {metrics.expectancy_r:+.3f}R "
                    f"(CI {metrics.expectancy_r_ci_low:+.3f} to {metrics.expectancy_r_ci_high:+.3f})",
                    f"- Profit factor: {metrics.profit_factor:.2f} · Net PnL ${metrics.net_pnl:+,.2f}",
                    f"- Max drawdown: {metrics.max_drawdown_pct * 100:.1f}% · "
                    f"Sharpe {metrics.sharpe_ratio:.2f} · Sortino {metrics.sortino_ratio:.2f}",
                    f"- Best regime: {metrics.best_regime} · Worst regime: {metrics.worst_regime}",
                    f"- Longest streaks: {metrics.longest_winning_streak}W / "
                    f"{metrics.longest_losing_streak}L",
                    "",
                ]
            )
            if metrics.by_regime:
                lines.extend(["### By regime", "", "| Regime | Trades | Win rate | Expectancy (R) |",
                              "|---|---:|---:|---:|"])
                for regime, stats in sorted(metrics.by_regime.items()):
                    lines.append(
                        f"| {regime} | {int(stats['trades'])} | {stats['win_rate'] * 100:.1f}% | "
                        f"{stats['expectancy_r']:+.3f} |"
                    )
                lines.append("")

        path = self.output_dir / "strategy_report.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def execution_report(self, experiment_id: str) -> Path:
        """Real demo order flow, fills, rejections, and attribution."""
        orders = self.repos.demo_orders.recent(300, experiment_id=experiment_id)
        positions = self.repos.positions.closed_positions(experiment_id)

        statuses: dict[str, int] = {}
        for order in orders:
            statuses[order["status"]] = statuses.get(order["status"], 0) + 1

        lines = [
            "# Execution Report",
            "",
            f"*Generated {iso(now_utc())}*",
            "",
            "## Order outcomes",
            "",
        ]
        for status, count in sorted(statuses.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"- **{status}**: {count}")

        lines.extend(["", "## Closed demo positions", "",
                      "| Strategy | Direction | Entry | Exit | R | PnL | Exit reason |",
                      "|---|---|---:|---:|---:|---:|---|"])
        for position in positions[-40:]:
            lines.append(
                f"| {position['strategy_id']} | {position['direction']} | "
                f"{float(position['entry_price'] or 0):,.2f} | "
                f"{float(position.get('exit_price') or 0):,.2f} | "
                f"{float(position.get('r_multiple') or 0):+.2f} | "
                f"${float(position.get('realized_pnl') or 0):+,.2f} | "
                f"{position.get('exit_reason', '')} |"
            )

        rejected = [o for o in orders if o["status"] == "rejected"]
        if rejected:
            lines.extend(["", "## Rejected orders", ""])
            for order in rejected[:25]:
                lines.append(
                    f"- {order['strategy_id']}: {order.get('reject_reason', 'unknown reason')}"
                )

        path = self.output_dir / "execution_report.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def learning_report(self, experiment_id: str) -> Path:
        """Parameter candidates, promotions, rejections, and signal filtering."""
        candidates = self.repos.candidates.recent(60)
        rejections = self.repos.signals.rejection_summary(experiment_id)
        counts = self.repos.signals.counts_by_strategy(experiment_id)
        champions = self.repos.champion.history()

        lines = [
            "# Learning Report",
            "",
            f"*Generated {iso(now_utc())}*",
            "",
            "## Parameter candidates",
            "",
        ]
        if candidates:
            lines.extend(
                ["| Strategy | Base → Candidate | Status | Stability | Reason |",
                 "|---|---|---|---:|---|"]
            )
            for candidate in candidates:
                reason = (candidate.get("decision_reason") or candidate["proposal_basis"])[:120]
                lines.append(
                    f"| {candidate['strategy_id']} | {candidate['base_version']} → "
                    f"{candidate['candidate_version']} | {candidate['status']} | "
                    f"{float(candidate.get('stability_score') or 0):.2f} | {reason} |"
                )
        else:
            lines.append("No candidates have been proposed yet.")

        lines.extend(["", "## Signal filtering", "",
                      "| Strategy | Accepted | Rejected | Acceptance rate |",
                      "|---|---:|---:|---:|"])
        for sid, stats in sorted(counts.items()):
            rate = safe_div(stats["accepted"], stats["total"]) * 100
            lines.append(f"| {sid} | {stats['accepted']} | {stats['rejected']} | {rate:.0f}% |")

        if rejections:
            lines.extend(["", "### Rejection reasons", ""])
            for row in rejections[:20]:
                lines.append(
                    f"- {row['strategy_id']}: `{row['rejection_reason']}` × {row['count']}"
                )

        if champions:
            lines.extend(["", "## Champion history", ""])
            for entry in champions[:10]:
                lines.append(
                    f"- {entry['ts_utc']}: {entry.get('previous_champion') or '—'} → "
                    f"**{entry['new_champion']}** ({entry['confidence']}) — {entry['reason'][:180]}"
                )

        path = self.output_dir / "learning_report.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    # --- final 14-day report ----------------------------------------------

    def final_report(
        self,
        state: ExperimentState,
        ranked: list[ScoreBreakdown],
        selection: ChampionSelection,
        evidence_map: dict[str, StrategyEvidence],
    ) -> list[Path]:
        """Write ``final_14_day_report.{md,html}`` and ``final_14_day_metrics.csv``."""
        rows = self._ranking_rows(ranked, evidence_map)

        csv_path = write_metrics_csv(self.output_dir / "final_14_day_metrics.csv", rows)
        md_path = self.output_dir / "final_14_day_report.md"
        html_path = self.output_dir / "final_14_day_report.html"

        md_path.write_text(
            self._final_markdown(state, rows, selection), encoding="utf-8"
        )
        html_path.write_text(
            render_final_report_html(state=state, rows=rows, selection=selection),
            encoding="utf-8",
        )

        # Companion reports so the whole picture is on disk at Day 14.
        self.strategy_report(state.experiment_id)
        self.execution_report(state.experiment_id)
        self.learning_report(state.experiment_id)

        return [md_path, html_path, csv_path]

    def _ranking_rows(
        self, ranked: list[ScoreBreakdown], evidence_map: dict[str, StrategyEvidence]
    ) -> list[dict[str, Any]]:
        """Build the ranking table — every column the brief lists."""
        rows: list[dict[str, Any]] = []
        for rank, breakdown in enumerate(ranked, start=1):
            evidence = evidence_map.get(breakdown.strategy_id)
            primary = None
            if evidence:
                primary = evidence.shadow or evidence.historical or evidence.demo

            strategy = self.registry.get(breakdown.strategy_id) if self.registry else None
            rows.append(
                {
                    "rank": rank,
                    "strategy": breakdown.strategy_id,
                    "version": breakdown.strategy_version,
                    "trades": primary.total_trades if primary else 0,
                    "wins": primary.wins if primary else 0,
                    "losses": primary.losses if primary else 0,
                    "win_rate_pct": round(primary.win_rate * 100, 2) if primary else 0.0,
                    "return_pct": round(primary.return_pct * 100, 2) if primary else 0.0,
                    "net_pnl": round(primary.net_pnl, 2) if primary else 0.0,
                    "profit_factor": round(primary.profit_factor, 3) if primary else 0.0,
                    "expectancy": round(primary.expectancy, 4) if primary else 0.0,
                    "avg_r": round(primary.average_r, 4) if primary else 0.0,
                    "median_r": round(primary.median_r, 4) if primary else 0.0,
                    "sharpe": round(primary.sharpe_ratio, 3) if primary else 0.0,
                    "sortino": round(primary.sortino_ratio, 3) if primary else 0.0,
                    "max_drawdown_pct": round(primary.max_drawdown_pct * 100, 2) if primary else 0.0,
                    "best_regime": primary.best_regime if primary else "n/a",
                    "worst_regime": primary.worst_regime if primary else "n/a",
                    "best_timeframe": (
                        primary.best_timeframe
                        if primary and primary.best_timeframe != "insufficient_data"
                        else (strategy.primary_timeframe if strategy else "n/a")
                    ),
                    "historical_score": round(breakdown.historical_score, 2),
                    "walk_forward_score": round(breakdown.walk_forward_score, 2),
                    "shadow_score": round(breakdown.shadow_score, 2),
                    "demo_score": round(breakdown.demo_score, 2),
                    "robustness_score": round(breakdown.robustness_score, 2),
                    "final_score": round(breakdown.final_score, 2),
                    "confidence": breakdown.confidence,
                    "observations": breakdown.total_observations,
                    "demo_trades": breakdown.demo_trades,
                    "penalties": "; ".join(breakdown.penalties) or "none",
                }
            )
        return rows

    def _final_markdown(
        self, state: ExperimentState, rows: list[dict[str, Any]], selection: ChampionSelection
    ) -> str:
        lines = [
            "# 14-Day OKX Demo Research — Final Report",
            "",
            f"*Generated {iso(now_utc())}*",
            "",
            "## Experiment",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Experiment ID | `{state.experiment_id}` |",
            f"| Name | {state.name} |",
            f"| Started | {iso(state.start)} |",
            f"| Scheduled end | {iso(state.scheduled_end)} |",
            f"| Duration | {state.duration_days} calendar days |",
            f"| Primary market | {state.primary_symbol} ({state.demo_category}) |",
            f"| Starting demo equity | ${state.starting_demo_equity:,.2f} |",
            f"| Shadow equity per strategy | ${state.shadow_equity_per_strategy:,.2f} |",
            f"| Strategies | {len(state.enabled_strategies)} |",
            f"| Config hash | `{state.config_hash}` |",
            f"| Software | v{state.software_version} (`{state.git_commit}`) |",
            "| Real money | **DISABLED** |",
            "",
            "---",
            "",
            "## Champion decision",
            "",
            "```",
        ]
        lines.extend(selection.banner())
        lines.extend(["```", "", "---", "", "## Full ranking", ""])

        header = (
            "| # | Strategy | Ver | Trades | W | L | Win% | Return% | Net PnL | PF | "
            "Expectancy | Avg R | Sharpe | Sortino | MaxDD% | Best regime | Worst regime | "
            "Best TF | Hist | WF | Shadow | Demo | Robust | **Final** | Conf |"
        )
        lines.append(header)
        lines.append("|" + "---|" * 25)
        for row in rows:
            lines.append(
                f"| {row['rank']} | {row['strategy']} | {row['version']} | {row['trades']} | "
                f"{row['wins']} | {row['losses']} | {row['win_rate_pct']:.1f} | "
                f"{row['return_pct']:.2f} | {row['net_pnl']:+,.2f} | {row['profit_factor']:.2f} | "
                f"{row['expectancy']:+.4f} | {row['avg_r']:+.3f} | {row['sharpe']:.2f} | "
                f"{row['sortino']:.2f} | {row['max_drawdown_pct']:.1f} | {row['best_regime']} | "
                f"{row['worst_regime']} | {row['best_timeframe']} | {row['historical_score']:.1f} | "
                f"{row['walk_forward_score']:.1f} | {row['shadow_score']:.1f} | "
                f"{row['demo_score']:.1f} | {row['robustness_score']:.1f} | "
                f"**{row['final_score']:.1f}** | {row['confidence']} |"
            )

        lines.extend(
            [
                "",
                "---",
                "",
                "## How to read this",
                "",
                "- **Final score** combines twelve weighted components across all three evidence",
                "  layers, then applies penalties for small samples, single-winner dependence,",
                "  period concentration, cost fragility, and parameter instability.",
                "- **Confidence** is `HIGH` only when a strategy has a full sample, at least ten",
                "  real demo trades, positive expectancy on two or more independent layers, a",
                "  bootstrap interval clear of zero, and no penalties.",
                "- A high win rate alone does not win. Neither does a large return on few trades —",
                "  strategies below the minimum sample threshold receive **zero** credit for",
                "  expectancy regardless of how good their returns look.",
                "- `SHORT`-only strategies could not trade the real demo layer when the account is",
                "  allocated to the real demo account. Their shadow and historical evidence is still complete; their",
                "  `Demo` score is zero because that layer was unavailable to them, not because",
                "  they failed.",
                "",
                "## What happens next",
                "",
                "The system has transitioned to **OKX_DEMO_CHAMPION** on the same demo account.",
                "The champion controls real demo execution; every challenger keeps running in",
                "shadow mode and can be promoted later, but only on statistically meaningful",
                "out-of-sample evidence. No real money is involved at any point.",
                "",
            ]
        )
        return "\n".join(lines)
