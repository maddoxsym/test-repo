"""HTML rendering for the final report.

Self-contained: inline CSS, no external assets, no network requests. The report
must open correctly from a local file years from now.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any

from ..utils.timeutil import iso, now_utc

if TYPE_CHECKING:
    from ..app.experiment import ExperimentState
    from ..scoring.champion import ChampionSelection

_STYLES = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  margin: 0; padding: 2rem 1.5rem; line-height: 1.55;
  background: #f7f8fa; color: #1a1d21;
}
@media (prefers-color-scheme: dark) {
  body { background: #14161a; color: #e6e8eb; }
  .card { background: #1c1f24 !important; border-color: #2b2f36 !important; }
  th { background: #22262d !important; }
  tr:nth-child(even) td { background: #191c21 !important; }
  code { background: #22262d !important; }
}
.wrap { max-width: 1400px; margin: 0 auto; }
h1 { font-size: 1.9rem; margin: 0 0 .35rem; letter-spacing: -.02em; }
h2 { font-size: 1.25rem; margin: 2rem 0 .75rem; letter-spacing: -.01em; }
.sub { opacity: .65; font-size: .9rem; margin-bottom: 1.5rem; }
.card {
  background: #fff; border: 1px solid #e3e6ea; border-radius: 10px;
  padding: 1.25rem 1.4rem; margin-bottom: 1.25rem;
}
.champion { border-left: 4px solid #2f81f7; }
.no-champion { border-left: 4px solid #d29922; }
.grid { display: grid; gap: .75rem 2rem; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); }
.metric .label { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; opacity: .6; }
.metric .value { font-size: 1.15rem; font-weight: 600; }
.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; width: 100%; font-size: .82rem; min-width: 1100px; }
th, td { padding: .45rem .6rem; text-align: right; border-bottom: 1px solid #e8eaed; white-space: nowrap; }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
th { background: #eff1f4; font-weight: 600; position: sticky; top: 0; font-size: .74rem;
     text-transform: uppercase; letter-spacing: .04em; }
tr:nth-child(even) td { background: #fafbfc; }
.rank-1 td { font-weight: 700; }
.pos { color: #1a7f37; } .neg { color: #cf222e; }
.pill { display: inline-block; padding: .1rem .5rem; border-radius: 999px; font-size: .7rem;
        font-weight: 600; letter-spacing: .03em; }
.pill.HIGH { background: #1a7f37; color: #fff; }
.pill.MEDIUM { background: #bf8700; color: #fff; }
.pill.LOW { background: #6e7781; color: #fff; }
pre { background: #22262d; color: #e6e8eb; padding: 1rem 1.2rem; border-radius: 8px;
      overflow-x: auto; font-size: .82rem; line-height: 1.5; }
code { background: #eff1f4; padding: .1rem .35rem; border-radius: 4px; font-size: .85em; }
.note { font-size: .85rem; opacity: .8; }
.banner { background: #1a7f37; color: #fff; padding: .5rem .9rem; border-radius: 6px;
          display: inline-block; font-weight: 600; font-size: .85rem; }
ul { margin: .4rem 0 0; padding-left: 1.2rem; }
li { margin: .25rem 0; }
"""


def render_final_report_html(
    *,
    state: ExperimentState,
    rows: list[dict[str, Any]],
    selection: ChampionSelection,
) -> str:
    """Render the Day-14 report as a self-contained HTML page."""
    champion_class = "no-champion" if selection.champion_type == "none" else "champion"
    banner_lines = "\n".join(escape(line) for line in selection.banner())

    metrics_html = "".join(
        f'<div class="metric"><div class="label">{escape(label)}</div>'
        f'<div class="value">{escape(value)}</div></div>'
        for label, value in (
            ("Experiment", state.experiment_id),
            ("Started", iso(state.start)),
            ("Ended", iso(state.scheduled_end)),
            ("Duration", f"{state.duration_days} days"),
            ("Market", f"{state.primary_symbol} ({state.demo_category})"),
            ("Research starting equity", f"${state.starting_research_equity_usdt:,.2f} (USDT only)"),
            ("Research equity cap", f"${state.research_equity_cap_usdt:,.2f}"),
            ("Actual OKX equity at start", f"${state.starting_demo_equity:,.2f} (all assets, unused)"),
            ("Shadow equity each", f"${state.shadow_equity_per_strategy:,.2f}"),
            ("Strategies", str(len(state.enabled_strategies))),
            ("Config hash", state.config_hash),
            ("Software", f"v{state.software_version} ({state.git_commit})"),
        )
    )

    headers = (
        "#", "Strategy", "Ver", "Trades", "W", "L", "Win %", "Return %", "Net PnL", "PF",
        "Expectancy", "Avg R", "Sharpe", "Sortino", "Max DD %", "Best regime", "Worst regime",
        "Best TF", "Hist", "WF", "Shadow", "Demo", "Robust", "Final", "Confidence",
    )
    header_html = "".join(f"<th>{escape(h)}</th>" for h in headers)

    body_rows = []
    for row in rows:
        pnl_class = "pos" if row["net_pnl"] > 0 else ("neg" if row["net_pnl"] < 0 else "")
        exp_class = "pos" if row["avg_r"] > 0 else ("neg" if row["avg_r"] < 0 else "")
        body_rows.append(
            f'<tr class="rank-{row["rank"]}">'
            f'<td>{row["rank"]}</td>'
            f'<td>{escape(str(row["strategy"]))}</td>'
            f'<td>{escape(str(row["version"]))}</td>'
            f'<td>{row["trades"]}</td><td>{row["wins"]}</td><td>{row["losses"]}</td>'
            f'<td>{row["win_rate_pct"]:.1f}</td>'
            f'<td class="{pnl_class}">{row["return_pct"]:+.2f}</td>'
            f'<td class="{pnl_class}">{row["net_pnl"]:+,.2f}</td>'
            f'<td>{row["profit_factor"]:.2f}</td>'
            f'<td class="{exp_class}">{row["expectancy"]:+.4f}</td>'
            f'<td class="{exp_class}">{row["avg_r"]:+.3f}</td>'
            f'<td>{row["sharpe"]:.2f}</td><td>{row["sortino"]:.2f}</td>'
            f'<td>{row["max_drawdown_pct"]:.1f}</td>'
            f'<td>{escape(str(row["best_regime"]))}</td>'
            f'<td>{escape(str(row["worst_regime"]))}</td>'
            f'<td>{escape(str(row["best_timeframe"]))}</td>'
            f'<td>{row["historical_score"]:.1f}</td>'
            f'<td>{row["walk_forward_score"]:.1f}</td>'
            f'<td>{row["shadow_score"]:.1f}</td>'
            f'<td>{row["demo_score"]:.1f}</td>'
            f'<td>{row["robustness_score"]:.1f}</td>'
            f'<td><strong>{row["final_score"]:.1f}</strong></td>'
            f'<td><span class="pill {escape(str(row["confidence"]))}">'
            f'{escape(str(row["confidence"]))}</span></td>'
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>14-Day OKX Demo Research — Final Report</title>
<style>{_STYLES}</style>
</head>
<body>
<div class="wrap">
  <h1>14-Day OKX Demo Research — Final Report</h1>
  <div class="sub">Generated {escape(iso(now_utc()))} ·
    <span class="banner">REAL MONEY: DISABLED</span></div>

  <div class="card">
    <h2 style="margin-top:0">Experiment</h2>
    <div class="grid">{metrics_html}</div>
  </div>

  <div class="card {champion_class}">
    <h2 style="margin-top:0">Champion decision</h2>
    <pre>{banner_lines}</pre>
  </div>

  <h2>Full ranking</h2>
  <div class="card">
    <div class="scroll">
      <table>
        <thead><tr>{header_html}</tr></thead>
        <tbody>{"".join(body_rows)}</tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h2 style="margin-top:0">How to read this</h2>
    <ul class="note">
      <li><strong>Final score</strong> combines twelve weighted components across all three
          evidence layers, then applies penalties for small samples, single-winner dependence,
          period concentration, cost fragility and parameter instability.</li>
      <li><strong>Confidence</strong> reaches <code>HIGH</code> only with a full sample, at least
          ten real demo trades, positive expectancy on two or more independent layers, a bootstrap
          interval clear of zero, and no penalties applied.</li>
      <li>A high win rate alone does not win, and neither does a large return on very few trades —
          strategies below the minimum sample threshold earn zero expectancy credit.</li>
      <li>A zero <em>Demo</em> score means the strategy never won an allocation to the real demo account; its shadow and historical evidence is still complete.
          That layer was unavailable to them; their shadow and historical evidence still counts.</li>
    </ul>
  </div>

  <div class="card">
    <h2 style="margin-top:0">What happens next</h2>
    <p class="note">The system has transitioned to <strong>OKX_DEMO_CHAMPION</strong> on the same
    demo account. The champion controls real demo execution while every challenger continues in
    shadow mode, eligible for promotion only on statistically meaningful out-of-sample evidence.
    No real money is involved at any stage.</p>
  </div>
</div>
</body>
</html>
"""
