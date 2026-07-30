"""
Daily reports, ranking snapshots and the final research report.

The final report ranks SETUP FAMILIES as well as individual variants, because
the question this research exists to answer is "which confluence model works
on GOLD", not "which parameter tweak got lucky".  The best result is labelled

    PROVISIONAL WINNER — NOT AUTOMATICALLY READY FOR LIVE TRADING.

and is accompanied by its sample size, a confidence band, and the follow-up
testing that would be needed before anyone could take it seriously.  No
profitability claim is made anywhere.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Sequence, Tuple

PROVISIONAL_LABEL = ("PROVISIONAL WINNER — NOT AUTOMATICALLY READY FOR LIVE "
                     "TRADING.")

FOLLOW_UP = [
    "Out-of-sample testing on data the parameters were never tuned on.",
    "Walk-forward testing: re-fit on a rolling window, evaluate forward only.",
    "At least 100 completed trades per surviving family before any "
    "conclusion; 5-40 trades is an observation, not evidence.",
    "A longer forward demo run at unchanged settings, including at least one "
    "high-volatility news week and one quiet holiday week.",
    "Independent verification that shadow fills match real demo fills, by "
    "comparing the real_trades and shadow_trades files for the same setups.",
    "Explicit spread and slippage sensitivity: re-rank with the cost "
    "assumptions increased by 50% and confirm the ordering survives.",
]


def _pct(value: float) -> str:
    return f"{value:+.2%}"


def confidence_note(n: int) -> str:
    if n < 5:
        return (f"{n} trades: no statistical meaning whatsoever — this is "
                f"anecdote.")
    if n < 12:
        return (f"{n} trades: experimental only. A single outcome moves the "
                f"expectancy materially.")
    if n < 25:
        return (f"{n} trades: low confidence. Direction of the estimate may "
                f"be real, magnitude is not reliable.")
    if n < 40:
        return (f"{n} trades: moderate confidence in the sign of the "
                f"expectancy, weak confidence in its size.")
    if n < 100:
        return (f"{n} trades: reasonable for a research signal, still short "
                f"of the 100+ needed to trust the magnitude.")
    return f"{n} trades: adequate sample for a research conclusion."


def variant_row(date_str: str, rank: int, variant, stats, score: float,
                cfg) -> dict:
    best_regime = ""
    best_exp = None
    for regime, entry in stats.by_regime.items():
        if entry[0] < cfg.min_regime_trades:
            continue
        exp = entry[1] / entry[0]
        if best_exp is None or exp > best_exp:
            best_exp, best_regime = exp, f"{regime} ({exp:+.2f}R/{int(entry[0])})"
    return {
        "date": date_str, "rank": rank, "sid": variant.sid,
        "family": variant.family, "version": variant.version,
        "status": variant.status, "trades": stats.n,
        "real_trades": stats.n_real, "confidence": stats.confidence,
        "net_expectancy": f"{stats.expectancy:+.4f}",
        "shrunk_expectancy": f"{stats.shrunk_expectancy(cfg.shrinkage_k):+.4f}",
        "score": f"{score:+.4f}", "win_rate": f"{stats.win_rate:.3f}",
        "profit_factor": (f"{stats.profit_factor:.3f}"
                          if stats.profit_factor != float("inf") else "inf"),
        "avg_win_r": f"{stats.avg_win_r:+.3f}",
        "avg_loss_r": f"{stats.avg_loss_r:.3f}",
        "max_dd_r": f"{stats.max_dd_r:.3f}", "std_r": f"{stats.std_r:.3f}",
        "avg_mfe_r": f"{stats.avg_mfe:.3f}",
        "avg_mae_r": f"{stats.avg_mae:.3f}",
        "stop_too_tight_rate": f"{stats.stop_too_tight_rate:.3f}",
        "partials": stats.partials_taken,
        "be_activations": stats.be_activations,
        "trail_uses": stats.trail_uses, "early_exits": stats.early_exits,
        "suspect_excluded": stats.suspect_excluded,
        "best_regime": best_regime,
        "params": ";".join(f"{k}={v:g}" for k, v
                           in sorted(variant.params.items()))}


def family_row(date_str: str, rank: int, name: str, score: float, stats,
               cfg) -> dict:
    mix = ";".join(f"{k}={v}" for k, v in sorted(stats.by_label.items()))
    return {
        "date": date_str, "rank": rank, "family": name, "trades": stats.n,
        "real_trades": stats.n_real, "confidence": stats.confidence,
        "net_expectancy": f"{stats.expectancy:+.4f}",
        "shrunk_expectancy": f"{stats.shrunk_expectancy(cfg.shrinkage_k):+.4f}",
        "score": f"{score:+.4f}", "win_rate": f"{stats.win_rate:.3f}",
        "profit_factor": (f"{stats.profit_factor:.3f}"
                          if stats.profit_factor != float("inf") else "inf"),
        "max_dd_r": f"{stats.max_dd_r:.3f}",
        "avg_mfe_r": f"{stats.avg_mfe:.3f}",
        "avg_mae_r": f"{stats.avg_mae:.3f}", "exit_mix": mix}


def daily_report(now: datetime, clock, book, variants: Sequence,
                 account_lines: Sequence[str], cfg,
                 decisions: Sequence = ()) -> str:
    lines: List[str] = []
    lines.append("=" * 74)
    lines.append(f"XAUUSD_Adaptive_Bot_V5 — daily research report "
                 f"{now:%Y-%m-%d %H:%M} UTC")
    lines.append(f"{clock.describe(now)}")
    lines.append("=" * 74)
    lines.extend(account_lines)
    lines.append("")

    fam_rank = book.family_ranking()
    lines.append("SETUP FAMILY RANKING (net R, after costs)")
    if not fam_rank:
        lines.append("  no completed trades yet")
    for i, (name, score, stats) in enumerate(fam_rank, 1):
        lines.append(f"  {i}. {name:<20} score {score:+.3f} | "
                     f"{stats.n} trades ({stats.n_real} real) | "
                     f"expectancy {stats.expectancy:+.3f}R | "
                     f"win rate {stats.win_rate:.0%} | "
                     f"max DD {stats.max_dd_r:.2f}R | {stats.confidence}")
    lines.append("")

    ranked = book.ranking(variants)
    lines.append("VARIANT RANKING")
    for i, (variant, score) in enumerate(ranked, 1):
        stats = book.get(variant.sid, variant.family)
        if stats.n == 0 and variant.status == "active":
            lines.append(f"  {i}. {variant.sid:<26} [{variant.status}] "
                         f"no completed trades yet")
            continue
        lines.append(f"  {i}. {variant.sid:<26} [{variant.status}] "
                     f"score {score:+.3f} | {stats.n} trades "
                     f"({stats.n_real} real) | exp {stats.expectancy:+.3f}R | "
                     f"PF {stats.profit_factor:.2f} | "
                     f"DD {stats.max_dd_r:.2f}R | {stats.confidence}")
    lines.append("")

    if decisions:
        lines.append("LEARNING DECISIONS TODAY")
        for d in decisions:
            extra = (f" [{d.param} {d.old:g} -> {d.new:g}]" if d.param else "")
            lines.append(f"  {d.kind} {d.sid}{extra}: {d.why}")
        lines.append("")

    if book.total_suspect:
        lines.append(f"NOTE: {book.total_suspect} shadow trade(s) failed an "
                     f"accounting invariant and were excluded from ranking "
                     f"(see suspect_trades.csv).")
        lines.append("")
    lines.append("No profitability is claimed. This is a demo research run.")
    return "\n".join(lines)


def final_report(now: datetime, clock, book, variants: Sequence, cfg,
                 account_lines: Sequence[str],
                 extra_notes: Sequence[str] = ()) -> Tuple[str, dict]:
    lines: List[str] = []
    lines.append("=" * 74)
    lines.append("XAUUSD_Adaptive_Bot_V5 — FINAL RESEARCH REPORT")
    lines.append(f"generated {now:%Y-%m-%d %H:%M} UTC")
    start = clock.start.isoformat() if clock.start else "unknown"
    lines.append(f"research start {start}")
    lines.append(f"active trading days completed "
                 f"{clock.completed_days()}/{cfg.research_days}")
    lines.append("=" * 74)
    lines.extend(account_lines)
    lines.append("")

    fam_rank = book.family_ranking()
    var_rank = book.ranking([v for v in variants])

    # ---- the provisional winner -------------------------------------------
    winner_line = "no family produced a single completed trade"
    winner: Dict[str, object] = {}
    if fam_rank:
        name, score, stats = fam_rank[0]
        winner_line = (f"{name} — score {score:+.3f}, expectancy "
                       f"{stats.expectancy:+.3f}R over {stats.n} trades "
                       f"({stats.n_real} real)")
        winner = {"family": name, "score": round(score, 4),
                  "trades": stats.n, "real_trades": stats.n_real,
                  "expectancy": round(stats.expectancy, 4),
                  "confidence": stats.confidence}
    lines.append("PROVISIONAL RESULT")
    lines.append(f"  {winner_line}")
    lines.append(f"  {PROVISIONAL_LABEL}")
    if fam_rank:
        lines.append(f"  {confidence_note(fam_rank[0][2].n)}")
    lines.append("")

    # ---- family ranking ----------------------------------------------------
    lines.append("SETUP FAMILY RANKING (the question this run set out to "
                 "answer)")
    if not fam_rank:
        lines.append("  none")
    for i, (name, score, stats) in enumerate(fam_rank, 1):
        lines.append(f"  {i}. {name}")
        lines.append(f"       score {score:+.3f} (shrunk expectancy "
                     f"{stats.shrunk_expectancy(cfg.shrinkage_k):+.3f}R minus "
                     f"drawdown and instability penalties)")
        lines.append(f"       {stats.n} trades ({stats.n_real} real), win rate "
                     f"{stats.win_rate:.0%}, profit factor "
                     f"{stats.profit_factor:.2f}")
        lines.append(f"       avg win {stats.avg_win_r:+.2f}R, avg loss "
                     f"{stats.avg_loss_r:.2f}R, max drawdown "
                     f"{stats.max_dd_r:.2f}R, R std dev {stats.std_r:.2f}")
        lines.append(f"       avg MFE {stats.avg_mfe:.2f}R, avg MAE "
                     f"{stats.avg_mae:.2f}R, stopped-then-target-reached "
                     f"{stats.stop_too_tight_rate:.0%}")
        lines.append("       exits: " + ", ".join(
            f"{k} {v}" for k, v in sorted(stats.by_label.items())))
        lines.append(f"       management: {stats.partials_taken} partials, "
                     f"{stats.be_activations} breakeven moves, "
                     f"{stats.trail_uses} trailing updates, "
                     f"{stats.early_exits} early exits")
        lines.append(f"       {confidence_note(stats.n)}")
    lines.append("")

    # ---- best per regime ---------------------------------------------------
    lines.append("BEST FAMILY PER MARKET REGIME (minimum "
                 f"{cfg.min_regime_trades} trades)")
    regimes: Dict[str, List[Tuple[str, float, int]]] = {}
    for name, _score, stats in fam_rank:
        for regime, entry in stats.by_regime.items():
            if entry[0] < cfg.min_regime_trades:
                continue
            regimes.setdefault(regime, []).append(
                (name, entry[1] / entry[0], int(entry[0])))
    if not regimes:
        lines.append("  not enough per-regime evidence")
    for regime in sorted(regimes):
        best = sorted(regimes[regime], key=lambda item: item[1], reverse=True)
        head = best[0]
        lines.append(f"  {regime:<18} {head[0]} at {head[1]:+.3f}R over "
                     f"{head[2]} trades")
    lines.append("")

    # ---- variants ----------------------------------------------------------
    lines.append("FULL VARIANT RANKING")
    for i, (variant, score) in enumerate(var_rank, 1):
        stats = book.get(variant.sid, variant.family)
        lines.append(f"  {i}. {variant.sid} v{variant.version} "
                     f"[{variant.status}] score {score:+.3f}, {stats.n} trades "
                     f"({stats.n_real} real), expectancy "
                     f"{stats.expectancy:+.3f}R, {stats.confidence}")
        lines.append("       params: " + ", ".join(
            f"{k}={v:g}" for k, v in sorted(variant.params.items())))
        if variant.parent:
            lines.append(f"       spawned from {variant.parent} "
                         f"({variant.mutations} mutation(s))")
    lines.append("")

    # ---- what the learning system did --------------------------------------
    lines.append("WHAT THE LEARNING SYSTEM DID")
    retired = [v.sid for v in variants if v.status == "retired"]
    benched = [v.sid for v in variants if v.status == "benched"]
    spawned = [v.sid for v in variants if v.parent]
    lines.append(f"  retired: {', '.join(retired) if retired else 'none'}")
    lines.append(f"  benched at the end: "
                 f"{', '.join(benched) if benched else 'none'}")
    lines.append(f"  spawned variants: "
                 f"{', '.join(spawned) if spawned else 'none'}")
    lines.append(f"  shadow trades excluded for failing accounting "
                 f"invariants: {book.total_suspect}")
    lines.append(f"  total completed trades recorded: {book.total_trades}")
    lines.append("")

    if extra_notes:
        lines.append("RUN NOTES")
        for note in extra_notes:
            lines.append(f"  {note}")
        lines.append("")

    # ---- required follow-up -------------------------------------------------
    lines.append("WHAT WOULD HAVE TO HAPPEN BEFORE THIS MEANT ANYTHING")
    for item in FOLLOW_UP:
        lines.append(f"  - {item}")
    lines.append("")
    lines.append("HONEST LIMITATIONS OF THIS RUN")
    lines.append("  - News protection is schedule-based and manual. There is "
                 "no live news feed in this bot.")
    lines.append("  - Shadow fills are simulated at the next M1 open with "
                 "spread, a slippage allowance and commission; real slippage "
                 "can differ, especially around news.")
    lines.append("  - Demo execution is not live execution: no real queue "
                 "position, no real rejections, no funding effects.")
    lines.append("  - Parameters were adapted during the run, so these "
                 "results are in-sample by construction.")
    lines.append("")
    lines.append(PROVISIONAL_LABEL)
    lines.append("No profitability is claimed, and nothing here supports live "
                 "or funded trading.")

    payload = {
        "generated": now.isoformat(),
        "research_start": start,
        "active_days_completed": clock.completed_days(),
        "research_days_configured": cfg.research_days,
        "label": PROVISIONAL_LABEL,
        "provisional_winner": winner,
        "families": [
            {"rank": i, "family": name, "score": round(score, 4),
             "trades": stats.n, "real_trades": stats.n_real,
             "expectancy": round(stats.expectancy, 4),
             "shrunk_expectancy": round(
                 stats.shrunk_expectancy(cfg.shrinkage_k), 4),
             "win_rate": round(stats.win_rate, 4),
             "profit_factor": (None if stats.profit_factor == float("inf")
                               else round(stats.profit_factor, 4)),
             "max_dd_r": round(stats.max_dd_r, 4),
             "avg_mfe_r": round(stats.avg_mfe, 4),
             "avg_mae_r": round(stats.avg_mae, 4),
             "confidence": stats.confidence,
             "exit_mix": dict(stats.by_label),
             "confidence_note": confidence_note(stats.n)}
            for i, (name, score, stats) in enumerate(fam_rank, 1)],
        "variants": [
            {"rank": i, "sid": v.sid, "family": v.family,
             "version": v.version, "status": v.status,
             "score": round(s, 4), "trades": book.get(v.sid).n,
             "real_trades": book.get(v.sid).n_real,
             "expectancy": round(book.get(v.sid).expectancy, 4),
             "confidence": book.get(v.sid).confidence,
             "params": dict(v.params), "parent": v.parent,
             "mutations": v.mutations}
            for i, (v, s) in enumerate(var_rank, 1)],
        "best_per_regime": {
            regime: {"family": sorted(rows, key=lambda r: r[1],
                                      reverse=True)[0][0],
                     "expectancy": round(sorted(rows, key=lambda r: r[1],
                                                reverse=True)[0][1], 4),
                     "trades": sorted(rows, key=lambda r: r[1],
                                      reverse=True)[0][2]}
            for regime, rows in regimes.items()},
        "suspect_excluded": book.total_suspect,
        "total_trades": book.total_trades,
        "required_follow_up": list(FOLLOW_UP),
        "notes": list(extra_notes),
        "profitability_claim": "none",
    }
    return "\n".join(lines), payload
