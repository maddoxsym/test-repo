"""
Statistical learning: performance tracking, risk-adjusted ranking,
exploration-vs-exploitation selection and controlled daily adaptation.

This is deliberately plain statistics — no "AI" claims:

RANKING (documented formula, applied identically to every strategy):
    shrunk_exp  = sum(R) / (n + k)          (k pulls small samples to 0)
    score       = shrunk_exp
                  - dd_penalty * max_drawdown_R
                  - instability_penalty * stdev(R)
                  - complexity_penalty * mutations
  A profitable strategy therefore ranks high only when its edge survives
  shrinkage, its drawdown is contained, its results are stable, and it did
  not need many parameter changes to look good (overfitting guard).

SELECTION for the single real DEMO position (UCB-style):
    eligible: active, >= min_shadow_trades, shrunk expectancy > 0
              (or regime-specific expectancy > 0 with enough regime trades)
    choose max( score + c * sqrt( ln(1+T) / (1+n_real) ) )
  so new-but-promising strategies still get real opportunities while
  proven ones are favoured — and one lucky win cannot dominate: with n=1
  and k=6, a +2.5R win shrinks to an expectancy of ~0.36R, below what a
  consistent performer accumulates over a real sample.

ADAPTATION (once per day, never after individual trades):
    * retire: n >= retire_min_trades and shrunk expectancy <= retire level
    * bench for a day: last-5 trades sum R <= bench threshold
    * spawn: bounded parameter variants of the best performers
    * parameter evidence nudges (e.g. >=40% of stops proved "too tight"
      over >= adapt_min_n trades -> widen that strategy's stop buffer one
      bounded step, as a new version)
All decisions are logged with their evidence and persisted.
"""

from __future__ import annotations

import math
import random
from typing import Callable, Dict, List, Optional, Tuple

from .shadow import VirtualTrade
from .strategy_space import (PARAM_BOUNDS, StrategyConfig, mutate_strategy)


class StrategyStats:
    """Running performance record for one strategy (shadow + real)."""

    def __init__(self):
        self.n = 0
        self.wins = 0
        self.sum_r = 0.0
        self.sum_r2 = 0.0
        self.gross_win_r = 0.0
        self.gross_loss_r = 0.0
        self.cum_r = 0.0
        self.peak_r = 0.0
        self.max_dd_r = 0.0
        self.consec_losses = 0
        self.max_consec_losses = 0
        self.recent: List[float] = []          # last 10 R values
        self.mfe_sum = 0.0
        self.mae_sum = 0.0
        self.stop_tight = 0                    # stopped, target hit later
        self.stop_watch = 0                    # stop-outs watched
        self.target_far = 0                    # MFE >= 1.5R but lost
        self.early_entry = 0                   # won but MAE >= 0.6R
        self.by_regime: Dict[str, List[float]] = {}
        self.by_session: Dict[str, List[float]] = {}
        self.by_dow: Dict[str, List[float]] = {}
        self.n_real = 0
        self.sum_r_real = 0.0
        self.time_in_market_bars = 0

    # ------------------------------------------------------------ record
    def record(self, r: float, regime: str, session: str, dow: str,
               mfe: float, mae: float, bars: int, is_real: bool,
               stop_watched: bool = False, stop_tight: bool = False) -> None:
        self.n += 1
        self.sum_r += r
        self.sum_r2 += r * r
        if r > 0:
            self.wins += 1
            self.gross_win_r += r
            self.consec_losses = 0
            if mae >= 0.6:
                self.early_entry += 1
        else:
            self.gross_loss_r += -r
            self.consec_losses += 1
            self.max_consec_losses = max(self.max_consec_losses,
                                         self.consec_losses)
            if mfe >= 1.5:
                self.target_far += 1
        self.cum_r += r
        self.peak_r = max(self.peak_r, self.cum_r)
        self.max_dd_r = max(self.max_dd_r, self.peak_r - self.cum_r)
        self.recent = (self.recent + [r])[-10:]
        self.mfe_sum += mfe
        self.mae_sum += mae
        self.time_in_market_bars += bars
        if stop_watched:
            self.stop_watch += 1
            if stop_tight:
                self.stop_tight += 1
        self.by_regime.setdefault(regime, []).append(r)
        self.by_session.setdefault(session, []).append(r)
        self.by_dow.setdefault(dow, []).append(r)
        if is_real:
            self.n_real += 1
            self.sum_r_real += r

    # ------------------------------------------------------------ metrics
    def expectancy(self) -> float:
        return self.sum_r / self.n if self.n else 0.0

    def shrunk_expectancy(self, k: float) -> float:
        return self.sum_r / (self.n + k) if (self.n + k) > 0 else 0.0

    def std_r(self) -> float:
        if self.n < 2:
            return 1.0
        mean = self.sum_r / self.n
        var = max(0.0, self.sum_r2 / self.n - mean * mean)
        return math.sqrt(var)

    def profit_factor(self) -> float:
        if self.gross_loss_r <= 0:
            return float("inf") if self.gross_win_r > 0 else 0.0
        return self.gross_win_r / self.gross_loss_r

    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    def sortino(self) -> Optional[float]:
        """Only when there is enough data (n >= 15) and downside exists."""
        if self.n < 15:
            return None
        mean = self.expectancy()
        neg_var = 0.0
        neg_n = 0
        # approximate downside deviation from gross loss statistics
        if self.n - self.wins > 0:
            avg_loss = self.gross_loss_r / (self.n - self.wins)
            neg_var = avg_loss * avg_loss
            neg_n = self.n - self.wins
        if neg_n == 0 or neg_var == 0:
            return None
        return mean / math.sqrt(neg_var)

    def regime_expectancy(self, regime: str) -> Tuple[int, float]:
        rs = self.by_regime.get(regime, [])
        return len(rs), (sum(rs) / len(rs) if rs else 0.0)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @staticmethod
    def from_dict(d: dict) -> "StrategyStats":
        s = StrategyStats()
        for k, v in d.items():
            setattr(s, k, v)
        return s


class LearningBook:

    def __init__(self, cfg, log: Callable[[str], None]):
        self.cfg = cfg
        self.log = log
        self.stats: Dict[str, StrategyStats] = {}
        self.real_selections = 0
        self.decisions: List[dict] = []       # in-memory tail of learning log

    def get(self, sid: str) -> StrategyStats:
        return self.stats.setdefault(sid, StrategyStats())

    # ------------------------------------------------------------ intake
    def record_shadow(self, t: VirtualTrade) -> None:
        self.get(t.strategy_id).record(
            r=t.r_multiple, regime=t.regime, session=t.session,
            dow=(t.entry_time or t.signal_time).strftime("%a"),
            mfe=t.mfe_r, mae=t.mae_r, bars=t.bars_open, is_real=False)

    def record_shadow_postmortem(self, t: VirtualTrade) -> None:
        """Called when the post-stop watch window resolves."""
        s = self.get(t.strategy_id)
        s.stop_watch += 1
        if t.watch_target_hit:
            s.stop_tight += 1

    def record_real(self, sid: str, r: float, regime: str, session: str,
                    dow: str, mfe: float, mae: float, bars: int) -> None:
        self.get(sid).record(r=r, regime=regime, session=session, dow=dow,
                             mfe=mfe, mae=mae, bars=bars, is_real=True)

    # ------------------------------------------------------------ ranking
    def score(self, scfg: StrategyConfig) -> float:
        c = self.cfg
        s = self.get(scfg.sid)
        return (s.shrunk_expectancy(c.shrinkage_k)
                - c.dd_penalty * s.max_dd_r
                - c.instability_penalty * s.std_r()
                - c.complexity_penalty * scfg.mutations)

    def ranking(self, population: List[StrategyConfig]
                ) -> List[Tuple[StrategyConfig, float]]:
        rows = [(p, self.score(p)) for p in population
                if p.status != "retired"]
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows

    # ------------------------------------------------------------ selection
    def eligible_for_real(self, scfg: StrategyConfig, regime: str) -> bool:
        c = self.cfg
        if scfg.status != "active":
            return False
        s = self.get(scfg.sid)
        if s.n < c.min_shadow_trades_for_real:
            return False
        rn, rexp = s.regime_expectancy(regime)
        if rn >= c.min_regime_trades:
            return rexp > 0.0
        return s.shrunk_expectancy(c.shrinkage_k) > 0.0

    def select_real(self, candidates: List[Tuple[StrategyConfig, object]],
                    regime: str) -> Optional[Tuple[StrategyConfig, object]]:
        """candidates: (config, signal) pairs with fresh signals this bar."""
        c = self.cfg
        best = None
        best_v = -1e9
        total = 1 + self.real_selections
        for scfg, sig in candidates:
            if not self.eligible_for_real(scfg, regime):
                continue
            s = self.get(scfg.sid)
            ucb = c.exploration_c * math.sqrt(math.log(1 + total)
                                              / (1 + s.n_real))
            v = self.score(scfg) + ucb
            if v > best_v:
                best_v, best = v, (scfg, sig)
        if best is not None:
            self.real_selections += 1
        return best

    # ------------------------------------------------------------ adaptation
    def daily_update(self, population: List[StrategyConfig],
                     rng: random.Random, today_iso: str
                     ) -> Tuple[List[StrategyConfig], List[dict]]:
        """Controlled once-per-day evolution.  Returns (new population,
        decision log entries)."""
        c = self.cfg
        decisions: List[dict] = []

        def note(kind, sid, why):
            d = {"date": today_iso, "kind": kind, "sid": sid, "why": why}
            decisions.append(d)
            self.log(f"LEARNING [{kind}] {sid}: {why}")

        # un-bench strategies whose bench day has passed
        for p in population:
            if p.status == "benched" and p.bench_until < today_iso:
                p.status = "active"
                note("UNBENCH", p.sid, "bench period ended")

        # retire / bench on evidence
        for p in population:
            if p.status != "active":
                continue
            s = self.get(p.sid)
            shrunk = s.shrunk_expectancy(c.shrinkage_k)
            if s.n >= c.retire_min_trades and shrunk <= c.retire_expectancy:
                p.status = "retired"
                note("RETIRE", p.sid,
                     f"n={s.n} shrunk expectancy {shrunk:+.2f}R <= "
                     f"{c.retire_expectancy}")
                continue
            if len(s.recent) >= c.bench_recent_n and \
                    sum(s.recent[-c.bench_recent_n:]) <= c.bench_recent_sum_r:
                p.status = "benched"
                p.bench_until = today_iso
                note("BENCH", p.sid,
                     f"last {c.bench_recent_n} trades sum "
                     f"{sum(s.recent[-c.bench_recent_n:]):+.1f}R — benched "
                     f"for one day")

        # evidence-based parameter nudges (new bounded version)
        for p in list(population):
            if p.status != "active":
                continue
            s = self.get(p.sid)
            bounds = PARAM_BOUNDS[p.archetype]
            if s.stop_watch >= c.adapt_min_n and "buffer_atr" in bounds:
                rate = s.stop_tight / s.stop_watch
                if rate >= c.adapt_rate_threshold:
                    lo, hi = bounds["buffer_atr"]
                    old = p.params.get("buffer_atr", lo)
                    new = min(hi, old + 0.10)
                    if new > old:
                        p.params["buffer_atr"] = new
                        p.version += 1
                        s.stop_watch = 0
                        s.stop_tight = 0
                        note("ADAPT", p.sid,
                             f"{rate:.0%} of stop-outs later reached target "
                             f"-> stop buffer {old:.2f} -> {new:.2f} ATR "
                             f"(v{p.version})")
            if s.n >= c.adapt_min_n and s.n - s.wins > 0 and "rr" in bounds:
                far_rate = s.target_far / max(1, s.n - s.wins)
                if far_rate >= c.adapt_rate_threshold:
                    lo, hi = bounds["rr"]
                    old = p.params.get("rr", lo)
                    new = max(lo, old - 0.25)
                    if new < old:
                        p.params["rr"] = new
                        p.version += 1
                        s.target_far = 0
                        note("ADAPT", p.sid,
                             f"{far_rate:.0%} of losers reached >=1.5R "
                             f"before losing -> RR target {old:.2f} -> "
                             f"{new:.2f} (v{p.version})")

        # spawn bounded variants of the best performers
        active = [p for p in population if p.status == "active"]
        if len(population) < c.max_population:
            ranked = self.ranking(active)
            spawned = 0
            serial = sum(p.mutations for p in population) + 1
            for parent, sc in ranked:
                if spawned >= c.spawn_per_day:
                    break
                s = self.get(parent.sid)
                if s.n >= c.spawn_parent_min_n and sc > 0:
                    child = mutate_strategy(parent, rng, serial + spawned,
                                            today_iso)
                    population.append(child)
                    spawned += 1
                    note("SPAWN", child.sid,
                         f"variant of {parent.sid} (score {sc:+.3f}, "
                         f"n={s.n}) v{child.version}")
        self.decisions.extend(decisions)
        return population, decisions

    # ------------------------------------------------------------ state io
    def snapshot(self) -> dict:
        return {"stats": {sid: s.to_dict() for sid, s in self.stats.items()},
                "real_selections": self.real_selections}

    def restore(self, snap: dict) -> None:
        self.stats = {sid: StrategyStats.from_dict(d)
                      for sid, d in snap.get("stats", {}).items()}
        self.real_selections = int(snap.get("real_selections", 0))
