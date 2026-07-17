"""
Daily and final reports for the 14-day research period.

The final report is honest about sample size: it labels the winner as
"PROVISIONAL WINNER — NOT AUTOMATICALLY READY FOR LIVE TRADING." and
states the confidence that the evidence actually supports, plus the
follow-up testing that must happen before any live consideration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .learning import LearningBook, StrategyStats
from .strategy_space import StrategyConfig

PROVISIONAL_LABEL = ("PROVISIONAL WINNER — NOT AUTOMATICALLY READY FOR "
                     "LIVE TRADING.")


def _confidence(n: int) -> str:
    if n >= 100:
        return "MODERATE (>=100 trades — still requires out-of-sample proof)"
    if n >= 30:
        return "LOW (30-99 trades)"
    if n >= 10:
        return "VERY LOW (10-29 trades)"
    return "INSUFFICIENT (<10 trades — treat as anecdote, not evidence)"


def _fmt_stats(sid: str, scfg: Optional[StrategyConfig],
               s: StrategyStats) -> List[str]:
    pf = s.profit_factor()
    pf_s = f"{pf:.2f}" if pf != float("inf") else "inf"
    lines = [
        f"  {sid}"
        + (f" (v{scfg.version}, {scfg.archetype} @ {scfg.tf}, "
           f"mgmt {scfg.mgmt_mode}, status {scfg.status})" if scfg else ""),
        f"    trades {s.n} (real {s.n_real}) | win rate {s.win_rate():.0%} "
        f"| expectancy {s.expectancy():+.2f}R | PF {pf_s} "
        f"| max DD {s.max_dd_r:.1f}R | worst streak {s.max_consec_losses}",
        f"    total {s.sum_r:+.1f}R | avg MFE {s.mfe_sum / max(1, s.n):.2f}R "
        f"| avg MAE {s.mae_sum / max(1, s.n):.2f}R "
        f"| stop-too-tight {s.stop_tight}/{max(1, s.stop_watch)} "
        f"| target-too-far {s.target_far}",
    ]
    so = s.sortino()
    if so is not None:
        lines.append(f"    sortino(approx) {so:.2f}")
    if s.by_regime:
        parts = [f"{k}: n={len(v)} {sum(v) / len(v):+.2f}R"
                 for k, v in sorted(s.by_regime.items()) if v]
        lines.append("    by regime: " + "; ".join(parts))
    if s.by_session:
        parts = [f"{k}: n={len(v)} {sum(v) / len(v):+.2f}R"
                 for k, v in sorted(s.by_session.items()) if v]
        lines.append("    by session: " + "; ".join(parts))
    return lines


def best_by_regime(book: LearningBook, population: List[StrategyConfig]
                   ) -> Dict[str, Tuple[str, int, float]]:
    """regime -> (sid, n, expectancy) with minimum-evidence filter."""
    out: Dict[str, Tuple[str, int, float]] = {}
    for p in population:
        s = book.get(p.sid)
        for regime, rs in s.by_regime.items():
            if len(rs) < book.cfg.min_regime_trades:
                continue
            exp = sum(rs) / len(rs)
            cur = out.get(regime)
            if cur is None or exp > cur[2]:
                out[regime] = (p.sid, len(rs), exp)
    return out


def daily_report(now: datetime, day_index: int, research_days: int,
                 book: LearningBook, population: List[StrategyConfig],
                 account_lines: List[str]) -> str:
    ranked = book.ranking(population)
    lines = [
        "=" * 70,
        f"DAILY RESEARCH REPORT — day {day_index}/{research_days} — "
        f"{now:%Y-%m-%d %H:%M} UTC",
        "=" * 70, ""]
    lines += account_lines + [""]
    lines.append(f"population: {len(population)} strategies "
                 f"({sum(1 for p in population if p.status == 'active')} "
                 f"active, "
                 f"{sum(1 for p in population if p.status == 'benched')} "
                 f"benched, "
                 f"{sum(1 for p in population if p.status == 'retired')} "
                 f"retired)")
    lines.append("")
    lines.append("top 8 by risk-adjusted score:")
    for p, sc in ranked[:8]:
        s = book.get(p.sid)
        lines.append(f"  {sc:+.3f}  {p.sid} v{p.version}  n={s.n} "
                     f"exp={s.expectancy():+.2f}R dd={s.max_dd_r:.1f}R")
    lines.append("")
    lines.append("bottom 5:")
    for p, sc in ranked[-5:]:
        s = book.get(p.sid)
        lines.append(f"  {sc:+.3f}  {p.sid} v{p.version}  n={s.n} "
                     f"exp={s.expectancy():+.2f}R")
    return "\n".join(lines)


def final_report(now: datetime, research_start: datetime,
                 book: LearningBook, population: List[StrategyConfig],
                 account_summary: Dict[str, float],
                 equity_notes: List[str]) -> Tuple[str, dict]:
    """Returns (text, json_dict)."""
    cfg = book.cfg
    ranked = book.ranking(population)
    total_trades = sum(book.get(p.sid).n for p in population)
    total_real = sum(book.get(p.sid).n_real for p in population)
    by_regime = best_by_regime(book, population)
    winner, winner_score = (ranked[0] if ranked else (None, 0.0))

    lines = [
        "=" * 70,
        "FINAL 14-DAY RESEARCH REPORT — XAUUSD_Adaptive_Bot_V4",
        f"period: {research_start:%Y-%m-%d %H:%M} -> {now:%Y-%m-%d %H:%M} UTC",
        "=" * 70, "",
        PROVISIONAL_LABEL, "",
        f"total recorded trades: {total_trades} shadow+real "
        f"({total_real} real DEMO trades)",
        f"overall sample confidence: {_confidence(total_trades)}", ""]

    lines.append("---- account (real DEMO position results) ----")
    for k, v in account_summary.items():
        lines.append(f"  {k}: {v}")
    lines += [""] + equity_notes + [""]

    if winner is not None:
        s = book.get(winner.sid)
        lines.append("---- PROVISIONAL BEST OVERALL STRATEGY ----")
        lines += _fmt_stats(winner.sid, winner, s)
        lines.append(f"    ranking score: {winner_score:+.3f}")
        lines.append(f"    strategy sample confidence: {_confidence(s.n)}")
        lines.append("    why it ranked highly: positive shrunk expectancy "
                     "after drawdown/instability/complexity penalties — "
                     "i.e. its edge survived small-sample shrinkage and it "
                     "did not rely on one lucky streak.")
        weak = sorted(((k, sum(v) / len(v)) for k, v in
                       s.by_regime.items() if v), key=lambda x: x[1])
        if weak:
            lines.append(f"    weakest regime: {weak[0][0]} "
                         f"({weak[0][1]:+.2f}R avg) — avoid there.")
        lines.append(f"    recommended risk: experimental tier "
                     f"({cfg.risk_tier_experimental[0]:.2%}-"
                     f"{cfg.risk_tier_experimental[1]:.2%}) until the "
                     f"follow-up testing below is complete")
        lines.append(f"    recommended stop method: as configured "
                     f"(v{winner.version} params: "
                     + ", ".join(f"{k}={v:g}" for k, v in
                                 sorted(winner.params.items())) + ")")
        lines.append(f"    recommended exit management: {winner.mgmt_mode}")
        best_sess = sorted(((k, sum(v) / len(v)) for k, v in
                            s.by_session.items() if len(v) >= 3),
                           key=lambda x: -x[1])
        if best_sess:
            lines.append("    recommended sessions: "
                         + ", ".join(f"{k} ({v:+.2f}R)"
                                     for k, v in best_sess[:2]))
        lines.append("")

    lines.append("---- BEST STRATEGY BY MARKET REGIME "
                  f"(min {cfg.min_regime_trades} trades) ----")
    if by_regime:
        for regime, (sid, n, exp) in sorted(by_regime.items()):
            lines.append(f"  {regime:<18} {sid}  n={n}  {exp:+.2f}R avg")
    else:
        lines.append("  insufficient per-regime evidence")
    lines.append("")

    lines.append("---- FULL RANKING ----")
    for p, sc in ranked:
        s = book.get(p.sid)
        lines.append(f"  {sc:+.3f}  {p.sid:<28} v{p.version} "
                     f"[{p.status}] n={s.n} exp={s.expectancy():+.2f}R "
                     f"PF={'inf' if s.profit_factor() == float('inf') else f'{s.profit_factor():.2f}'} "
                     f"dd={s.max_dd_r:.1f}R")
    lines.append("")

    lines.append("---- WHAT THE LEARNING SYSTEM DID ----")
    kinds: Dict[str, int] = {}
    for d in book.decisions:
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    lines.append("  decisions: " + (", ".join(f"{k}={v}" for k, v in
                                              sorted(kinds.items()))
                                    if kinds else "none recorded in memory"))
    lines.append("  losing-trade lessons: stop-too-tight and "
                 "target-too-far counters above show, per strategy, where "
                 "stops were inside noise and where targets were "
                 "unrealistic; ADAPT entries in learning_log.csv show every "
                 "evidence-based change and parameter_updates.csv whether "
                 "later performance improved or worsened.")
    lines.append("")

    lines.append("---- HONEST LIMITS OF THIS EXPERIMENT ----")
    lines.append(f"  {_confidence(total_trades)} overall; 14 days rarely "
                 "covers all market conditions for gold.")
    lines.append("  Required BEFORE any live consideration:")
    lines.append("    1. out-of-sample testing on data the strategies never "
                 "saw;")
    lines.append("    2. walk-forward testing across several windows;")
    lines.append("    3. at least 100 completed trades overall and a "
                 "meaningful sample per surviving strategy;")
    lines.append("    4. a longer forward-demo period with the selected "
                 "configuration frozen;")
    lines.append("    5. manual review of every ADAPT decision.")
    lines.append("")
    lines.append("  The DEMO-only lock remains in force. Nothing in this "
                 "report enables live trading.")

    js = {
        "label": PROVISIONAL_LABEL,
        "generated": now.isoformat(),
        "research_start": research_start.isoformat(),
        "total_trades": total_trades,
        "total_real_trades": total_real,
        "confidence": _confidence(total_trades),
        "account": account_summary,
        "winner": ({"sid": winner.sid, "version": winner.version,
                    "archetype": winner.archetype, "tf": winner.tf,
                    "params": winner.params, "mgmt": winner.mgmt,
                    "score": winner_score,
                    "stats": book.get(winner.sid).to_dict()}
                   if winner else None),
        "best_by_regime": {k: {"sid": v[0], "n": v[1], "expectancy": v[2]}
                           for k, v in by_regime.items()},
        "ranking": [{"sid": p.sid, "version": p.version, "status": p.status,
                     "score": sc, "n": book.get(p.sid).n,
                     "expectancy": book.get(p.sid).expectancy()}
                    for p, sc in ranked],
    }
    return "\n".join(lines), js
