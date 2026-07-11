"""
PerformanceCoach -- the learning engine.

Learns from every closed trade in three ways:

1. STRATEGY WEIGHTS.  Each strategy analyst has a weight (default 1.0).
   After a trade closes, every strategy that confirmed it is reweighted
   multiplicatively by exp(eta * R): winners gain influence in the council
   vote, losers lose it.  Weights are clamped so no analyst is ever fully
   silenced or dominant.

2. COMBO DISCOVERY.  The exact combination of confirming strategies plus
   the market regime is tracked as its own entity (e.g.
   "breakout_hunter+trend_rider @ trending_up").  Combos with enough
   history and NEGATIVE expectancy are vetoed outright -- the bot stops
   repeating mistakes.  Combos with strongly positive expectancy get a
   lower entry threshold -- the bot leans into what works.  This is how it
   "builds its own strategy" out of the pieces it was given.

3. ADAPTIVE SELECTIVITY.  The council's score threshold rises after losing
   days (be pickier) and drifts down slowly while recent expectancy is
   good (take a few more of the proven trades).

All state lives in the journal's sqlite db so learning survives restarts.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

from .config import LearningConfig
from .journal import Journal, TradeRecord

log = logging.getLogger("learning")


def combo_key(confirmations: list[str]) -> str:
    return "+".join(sorted(confirmations))


@dataclass
class ComboVerdict:
    veto: bool
    boost: bool
    expectancy: Optional[float]     # avg R per trade, None if not enough history
    trades: int
    reason: str


class PerformanceCoach:
    def __init__(self, cfg: LearningConfig, journal: Journal):
        self.cfg = cfg
        self.journal = journal

    # ----------------------------------------------------------- weights

    def weight(self, strategy_name: str) -> float:
        row = self.journal.conn.execute(
            "SELECT weight FROM strategy_weights WHERE name=?",
            (strategy_name,)).fetchone()
        return float(row["weight"]) if row else 1.0

    def weights(self) -> dict[str, float]:
        rows = self.journal.conn.execute(
            "SELECT name, weight FROM strategy_weights").fetchall()
        return {r["name"]: float(r["weight"]) for r in rows}

    def _set_weight(self, name: str, weight: float) -> None:
        weight = max(self.cfg.weight_min, min(self.cfg.weight_max, weight))
        with self.journal.conn:
            self.journal.conn.execute(
                """INSERT INTO strategy_weights (name, weight, updated_at)
                   VALUES (?,?,?)
                   ON CONFLICT(name) DO UPDATE
                   SET weight=excluded.weight, updated_at=excluded.updated_at""",
                (name, weight, time.time()))

    # ------------------------------------------------------------ combos

    def combo_verdict(self, confirmations: list[str], regime: str) -> ComboVerdict:
        key = combo_key(confirmations)
        row = self.journal.conn.execute(
            "SELECT trades, wins, total_r FROM combo_stats WHERE combo=? AND regime=?",
            (key, regime)).fetchone()
        if row is None or row["trades"] < self.cfg.combo_min_trades:
            n = row["trades"] if row else 0
            return ComboVerdict(False, False, None, n,
                                f"combo '{key}' still gathering history ({n} trades)")
        expectancy = row["total_r"] / row["trades"]
        if expectancy <= self.cfg.combo_veto_expectancy:
            return ComboVerdict(
                True, False, expectancy, row["trades"],
                f"combo '{key}' in {regime} has proven unprofitable "
                f"({expectancy:+.2f}R avg over {row['trades']} trades) -- vetoed")
        if expectancy >= self.cfg.combo_boost_expectancy:
            return ComboVerdict(
                False, True, expectancy, row["trades"],
                f"combo '{key}' in {regime} has a proven edge "
                f"({expectancy:+.2f}R avg over {row['trades']} trades)")
        return ComboVerdict(False, False, expectancy, row["trades"],
                            f"combo '{key}' neutral ({expectancy:+.2f}R)")

    # --------------------------------------------------------- threshold

    def score_threshold(self, base: float) -> float:
        stored = self.journal.get_meta("score_threshold")
        try:
            value = float(stored) if stored else base
        except ValueError:
            value = base
        return max(self.cfg.threshold_min, min(self.cfg.threshold_max, value))

    def _adjust_threshold(self, base: float, delta: float) -> None:
        new = self.score_threshold(base) + delta
        new = max(self.cfg.threshold_min, min(self.cfg.threshold_max, new))
        self.journal.set_meta("score_threshold", f"{new:.4f}")
        log.info("selectivity threshold adjusted to %.2f", new)

    # ---------------------------------------------------------- learning

    def learn_from_close(self, trade: TradeRecord, base_threshold: float) -> None:
        """Call once for every trade that just closed."""
        r = trade.r_multiple if trade.r_multiple is not None else 0.0
        r = max(-3.0, min(3.0, r))          # clamp outliers

        # 1. reweight every confirming strategy
        for name in trade.confirmations:
            old = self.weight(name)
            new = old * math.exp(self.cfg.eta * r)
            self._set_weight(name, new)
            log.info("weight %s: %.3f -> %.3f (trade %+0.2fR)",
                     name, old, max(self.cfg.weight_min,
                                    min(self.cfg.weight_max, new)), r)

        # 2. update combo statistics
        key = combo_key(trade.confirmations)
        with self.journal.conn:
            self.journal.conn.execute(
                """INSERT INTO combo_stats (combo, regime, trades, wins, total_r)
                   VALUES (?,?,1,?,?)
                   ON CONFLICT(combo, regime) DO UPDATE SET
                       trades = trades + 1,
                       wins = wins + excluded.wins,
                       total_r = total_r + excluded.total_r""",
                (key, trade.regime or "unknown", 1 if r > 0 else 0, r))

        # 3. adapt selectivity from recent form
        recent = self.journal.recent_closed(self.cfg.recent_window)
        if len(recent) >= self.cfg.recent_window:
            avg_r = sum((t.r_multiple or 0.0) for t in recent) / len(recent)
            if avg_r > 0.3:
                self._adjust_threshold(base_threshold, -self.cfg.threshold_step_down)
            elif avg_r < -0.2:
                self._adjust_threshold(base_threshold, self.cfg.threshold_step_up)

    def on_daily_loss_limit(self, base_threshold: float) -> None:
        """Losing enough to trip the daily limit makes the bot pickier."""
        self._adjust_threshold(base_threshold, self.cfg.threshold_step_up)

    # ----------------------------------------------------------- reports

    def report(self) -> str:
        lines = ["strategy weights:"]
        weights = self.weights()
        if not weights:
            lines.append("  (all at default 1.00 -- no closed trades yet)")
        for name, w in sorted(weights.items()):
            lines.append(f"  {name:<20} {w:.3f}")
        rows = self.journal.conn.execute(
            """SELECT combo, regime, trades, wins, total_r FROM combo_stats
               ORDER BY total_r DESC LIMIT 10""").fetchall()
        if rows:
            lines.append("top strategy combos (by total R):")
            for r in rows:
                exp = r["total_r"] / r["trades"] if r["trades"] else 0.0
                lines.append(
                    f"  {r['combo']:<40} @ {r['regime']:<13} "
                    f"n={r['trades']:<3} win={r['wins']}/{r['trades']} "
                    f"exp={exp:+.2f}R")
        return "\n".join(lines)
