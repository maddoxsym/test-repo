"""
Statistical learning: documented statistics, no invented terminology.

Everything here is ordinary applied statistics on NET R (after spread,
slippage and commission).  There is no machine learning, no neural network,
no "AI" — and no self-modifying code.  The system stores numbers, ranks
configurations by those numbers, and adjusts stored parameters inside
published bounds.

What it does:
  * keeps per-VARIANT and per-FAMILY statistics, so the final ranking can
    answer "which setup family actually works", not just "which parameter
    tweak got lucky";
  * ranks with a SHRUNK expectancy (pulled toward zero by k pseudo-trades),
    minus penalties for drawdown, instability and configuration complexity,
    so one lucky 6R winner cannot outrank a steady performer;
  * gates real trades on both variant-level and family-level evidence;
  * graduates risk slowly (0.10% -> 0.25%) as completed trades accumulate,
    and requires real (not just shadow) evidence for the top tiers;
  * chooses which eligible strategy gets the single real position with a
    UCB-style rule, so exploration continues and one early winner cannot
    monopolise the research;
  * retires losers, benches slumps, spawns bounded variants of proven
    parents, and adapts parameters only when the evidence names the problem
    (for example: a high rate of "stopped out, then the target was reached
    anyway" widens the stop buffer, once, within bounds).

Suspect shadow trades (accounting invariant violations) never reach this
module — the shadow engine excludes them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

CONFIDENCE_BANDS: Tuple[Tuple[int, str], ...] = (
    (0, "NONE"),
    (5, "EXPERIMENTAL"),
    (12, "LOW"),
    (25, "MODERATE"),
    (40, "REASONABLE"),
)


def confidence_label(n: int) -> str:
    label = "NONE"
    for threshold, name in CONFIDENCE_BANDS:
        if n >= threshold:
            label = name
    return label


@dataclass
class Stats:
    """Net-R statistics for one variant or one family."""
    key: str
    family: str = ""
    n: int = 0
    n_real: int = 0
    wins: int = 0
    losses: int = 0
    sum_r: float = 0.0
    sum_r2: float = 0.0
    sum_win_r: float = 0.0
    sum_loss_r: float = 0.0
    equity_r: float = 0.0
    peak_r: float = 0.0
    max_dd_r: float = 0.0
    streak_losses: int = 0
    worst_streak: int = 0
    recent: List[float] = field(default_factory=list)
    sum_mfe: float = 0.0
    sum_mae: float = 0.0
    stop_watch_events: int = 0
    stop_watch_target_hit: int = 0
    partials_taken: int = 0
    be_activations: int = 0
    trail_uses: int = 0
    early_exits: int = 0
    suspect_excluded: int = 0
    by_regime: Dict[str, List[float]] = field(default_factory=dict)
    by_session: Dict[str, List[float]] = field(default_factory=dict)
    by_family: Dict[str, List[float]] = field(default_factory=dict)
    by_label: Dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------- accounting
    def add(self, r: float, regime: str, session: str, family: str,
            label: str, mfe: float, mae: float, real: bool) -> None:
        self.n += 1
        if real:
            self.n_real += 1
        self.sum_r += r
        self.sum_r2 += r * r
        if r > 0:
            self.wins += 1
            self.sum_win_r += r
            self.streak_losses = 0
        elif r < 0:
            self.losses += 1
            self.sum_loss_r += -r
            self.streak_losses += 1
            self.worst_streak = max(self.worst_streak, self.streak_losses)
        self.equity_r += r
        self.peak_r = max(self.peak_r, self.equity_r)
        self.max_dd_r = max(self.max_dd_r, self.peak_r - self.equity_r)
        self.recent.append(r)
        if len(self.recent) > 20:
            self.recent = self.recent[-20:]
        self.sum_mfe += mfe
        self.sum_mae += mae
        for bucket, name in ((self.by_regime, regime),
                             (self.by_session, session),
                             (self.by_family, family)):
            entry = bucket.setdefault(name or "UNKNOWN", [0.0, 0.0])
            entry[0] += 1
            entry[1] += r
        self.by_label[label] = self.by_label.get(label, 0) + 1

    # ------------------------------------------------------------- statistics
    @property
    def expectancy(self) -> float:
        return self.sum_r / self.n if self.n else 0.0

    def shrunk_expectancy(self, k: float) -> float:
        """Expectancy pulled toward zero by k pseudo-trades of 0R.

        With k = 8, a single +6R trade reads as +0.67R per trade, not +6R."""
        if self.n <= 0:
            return 0.0
        return self.sum_r / (self.n + k)

    @property
    def std_r(self) -> float:
        if self.n < 2:
            return 0.0
        mean = self.expectancy
        var = max(0.0, self.sum_r2 / self.n - mean * mean)
        return math.sqrt(var * self.n / (self.n - 1))

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def profit_factor(self) -> float:
        if self.sum_loss_r <= 0:
            return float("inf") if self.sum_win_r > 0 else 0.0
        return self.sum_win_r / self.sum_loss_r

    @property
    def avg_win_r(self) -> float:
        return self.sum_win_r / self.wins if self.wins else 0.0

    @property
    def avg_loss_r(self) -> float:
        return self.sum_loss_r / self.losses if self.losses else 0.0

    @property
    def avg_mfe(self) -> float:
        return self.sum_mfe / self.n if self.n else 0.0

    @property
    def avg_mae(self) -> float:
        return self.sum_mae / self.n if self.n else 0.0

    @property
    def stop_too_tight_rate(self) -> float:
        if self.stop_watch_events <= 0:
            return 0.0
        return self.stop_watch_target_hit / self.stop_watch_events

    @property
    def confidence(self) -> str:
        return confidence_label(self.n)

    def sortino(self) -> float:
        if self.n < 15:
            return 0.0
        downside = [r for r in self.recent if r < 0]
        if not downside:
            return float("inf") if self.expectancy > 0 else 0.0
        dd = math.sqrt(sum(r * r for r in downside) / len(downside))
        return self.expectancy / dd if dd > 0 else 0.0

    def regime_expectancy(self, regime: str) -> Optional[float]:
        entry = self.by_regime.get(regime)
        if not entry or entry[0] <= 0:
            return None
        return entry[1] / entry[0]

    def regime_n(self, regime: str) -> int:
        entry = self.by_regime.get(regime)
        return int(entry[0]) if entry else 0

    # ------------------------------------------------------------- state io
    def to_dict(self) -> dict:
        return {
            "key": self.key, "family": self.family, "n": self.n,
            "n_real": self.n_real, "wins": self.wins, "losses": self.losses,
            "sum_r": self.sum_r, "sum_r2": self.sum_r2,
            "sum_win_r": self.sum_win_r, "sum_loss_r": self.sum_loss_r,
            "equity_r": self.equity_r, "peak_r": self.peak_r,
            "max_dd_r": self.max_dd_r, "streak_losses": self.streak_losses,
            "worst_streak": self.worst_streak, "recent": list(self.recent),
            "sum_mfe": self.sum_mfe, "sum_mae": self.sum_mae,
            "stop_watch_events": self.stop_watch_events,
            "stop_watch_target_hit": self.stop_watch_target_hit,
            "partials_taken": self.partials_taken,
            "be_activations": self.be_activations,
            "trail_uses": self.trail_uses, "early_exits": self.early_exits,
            "suspect_excluded": self.suspect_excluded,
            "by_regime": {k: list(v) for k, v in self.by_regime.items()},
            "by_session": {k: list(v) for k, v in self.by_session.items()},
            "by_family": {k: list(v) for k, v in self.by_family.items()},
            "by_label": dict(self.by_label)}

    @staticmethod
    def from_dict(d: dict) -> "Stats":
        s = Stats(key=d.get("key", ""), family=d.get("family", ""))
        for name in ("n", "n_real", "wins", "losses", "streak_losses",
                     "worst_streak", "stop_watch_events",
                     "stop_watch_target_hit", "partials_taken",
                     "be_activations", "trail_uses", "early_exits",
                     "suspect_excluded"):
            setattr(s, name, int(d.get(name, 0)))
        for name in ("sum_r", "sum_r2", "sum_win_r", "sum_loss_r", "equity_r",
                     "peak_r", "max_dd_r", "sum_mfe", "sum_mae"):
            setattr(s, name, float(d.get(name, 0.0)))
        s.recent = [float(x) for x in d.get("recent", [])]
        s.by_regime = {str(k): [float(v[0]), float(v[1])]
                       for k, v in dict(d.get("by_regime", {})).items()}
        s.by_session = {str(k): [float(v[0]), float(v[1])]
                        for k, v in dict(d.get("by_session", {})).items()}
        s.by_family = {str(k): [float(v[0]), float(v[1])]
                       for k, v in dict(d.get("by_family", {})).items()}
        s.by_label = {str(k): int(v)
                      for k, v in dict(d.get("by_label", {})).items()}
        return s


@dataclass
class LearningDecision:
    kind: str            # RETIRE | BENCH | UNBENCH | SPAWN | ADAPT
    sid: str
    why: str
    param: str = ""
    old: float = 0.0
    new: float = 0.0


class LearningBook:

    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log or (lambda m: None)
        self.variants: Dict[str, Stats] = {}
        self.families: Dict[str, Stats] = {}
        self.total_trades = 0
        self.total_suspect = 0

    # ------------------------------------------------------------- accessors
    def get(self, sid: str, family: str = "") -> Stats:
        s = self.variants.get(sid)
        if s is None:
            s = Stats(key=sid, family=family)
            self.variants[sid] = s
        elif family and not s.family:
            s.family = family
        return s

    def family(self, name: str) -> Stats:
        s = self.families.get(name)
        if s is None:
            s = Stats(key=name, family=name)
            self.families[name] = s
        return s

    # ------------------------------------------------------------- recording
    def record(self, sid: str, family: str, net_r: float, regime: str,
               session: str, label: str, mfe: float, mae: float,
               real: bool, partial: bool = False, be: bool = False,
               trail: bool = False, early: bool = False) -> None:
        for stats in (self.get(sid, family), self.family(family)):
            stats.add(net_r, regime, session, family, label, mfe, mae, real)
            if partial:
                stats.partials_taken += 1
            if be:
                stats.be_activations += 1
            if trail:
                stats.trail_uses += 1
            if early:
                stats.early_exits += 1
        self.total_trades += 1

    def record_suspect(self, sid: str, family: str) -> None:
        self.get(sid, family).suspect_excluded += 1
        self.family(family).suspect_excluded += 1
        self.total_suspect += 1

    def record_stop_watch(self, sid: str, family: str,
                          target_hit: bool) -> None:
        for stats in (self.get(sid, family), self.family(family)):
            stats.stop_watch_events += 1
            if target_hit:
                stats.stop_watch_target_hit += 1

    # --------------------------------------------------------------- ranking
    def uncertainty_penalty(self, s: Stats) -> float:
        """Standard-error term of a one-sided lower confidence bound.

        This is what stops a single lucky outcome from topping the table.
        With n = 1 the sample standard deviation is meaningless, so a prior
        spread of prior_sigma_r is assumed; the penalty then decays as
        1/sqrt(n), which is exactly the rate at which the estimate actually
        becomes trustworthy."""
        cfg = self.cfg
        if s.n <= 0:
            return 0.0
        sigma = max(s.std_r, cfg.prior_sigma_r)
        return cfg.uncertainty_z * sigma / math.sqrt(float(s.n))

    def score(self, sid: str, mutations: int = 0) -> float:
        """Risk-adjusted RANKING score: a lower confidence bound on the
        shrunk net expectancy, minus drawdown, instability and complexity
        penalties.  Used for ranking, selection and reporting — never for the
        eligibility gate, which has its own explicit sample thresholds."""
        cfg = self.cfg
        s = self.get(sid)
        if s.n <= 0:
            return 0.0
        base = s.shrunk_expectancy(cfg.shrinkage_k)
        penalty = (cfg.dd_penalty * s.max_dd_r
                   + cfg.instability_penalty * s.std_r
                   + cfg.complexity_penalty * mutations
                   + self.uncertainty_penalty(s))
        return base - penalty

    def family_score(self, name: str) -> float:
        cfg = self.cfg
        s = self.family(name)
        if s.n <= 0:
            return 0.0
        return (s.shrunk_expectancy(cfg.shrinkage_k)
                - cfg.dd_penalty * s.max_dd_r
                - cfg.instability_penalty * s.std_r
                - self.uncertainty_penalty(s))

    def ranking(self, variants: Sequence) -> List[Tuple[object, float]]:
        scored = [(v, self.score(v.sid, v.mutations)) for v in variants]
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored

    def family_ranking(self) -> List[Tuple[str, float, Stats]]:
        out = [(name, self.family_score(name), stats)
               for name, stats in self.families.items() if stats.n > 0]
        out.sort(key=lambda item: item[1], reverse=True)
        return out

    # ---------------------------------------------------------- eligibility
    def eligible_for_real(self, variant, regime: str) -> Tuple[bool, str]:
        """Both the variant AND its family need evidence, and the expectancy
        must be positive after shrinkage."""
        cfg = self.cfg
        if variant.status != "active":
            return False, f"variant is {variant.status}"
        s = self.get(variant.sid, variant.family)
        fam = self.family(variant.family)
        if s.n < cfg.min_shadow_trades_for_real:
            return False, (f"{s.n} completed shadow trades, needs "
                           f"{cfg.min_shadow_trades_for_real}")
        if fam.n < cfg.min_family_trades_for_real:
            return False, (f"family {variant.family} has {fam.n} trades, "
                           f"needs {cfg.min_family_trades_for_real}")
        exp = s.shrunk_expectancy(cfg.shrinkage_k)
        if exp <= 0:
            return False, (f"shrunk net expectancy {exp:+.3f}R is not "
                           f"positive")
        fam_exp = fam.shrunk_expectancy(cfg.shrinkage_k)
        if fam_exp <= 0:
            return False, (f"family {variant.family} shrunk expectancy "
                           f"{fam_exp:+.3f}R is not positive")
        rn = s.regime_n(regime)
        if rn >= cfg.min_regime_trades:
            rexp = s.regime_expectancy(regime)
            if rexp is not None and rexp <= 0:
                return False, (f"negative expectancy {rexp:+.2f}R in regime "
                               f"{regime} over {rn} trades")
        return True, (f"{s.n} trades ({s.n_real} real), shrunk expectancy "
                      f"{exp:+.3f}R, confidence {s.confidence}")

    def risk_tier(self, variant) -> Tuple[float, str]:
        """Slow graduation. Real evidence is required for the top tiers."""
        cfg = self.cfg
        s = self.get(variant.sid, variant.family)
        exp = s.shrunk_expectancy(cfg.shrinkage_k)
        if s.n >= cfg.tier_established_min_n \
                and exp >= cfg.tier_established_min_expectancy \
                and s.n_real >= cfg.tier_min_real_trades_established:
            return cfg.risk_tier_established, (
                f"established: {s.n} trades ({s.n_real} real), "
                f"expectancy {exp:+.3f}R")
        if s.n >= cfg.tier_confirmed_min_n \
                and exp >= cfg.tier_confirmed_min_expectancy \
                and s.n_real >= cfg.tier_min_real_trades_confirmed:
            return cfg.risk_tier_confirmed, (
                f"confirmed: {s.n} trades ({s.n_real} real), "
                f"expectancy {exp:+.3f}R")
        if s.n >= cfg.tier_early_min_n and exp >= cfg.tier_early_min_expectancy:
            return cfg.risk_tier_early, (
                f"early: {s.n} trades, expectancy {exp:+.3f}R")
        return cfg.risk_tier_probe, (
            f"probe: {s.n} trades, expectancy {exp:+.3f}R "
            f"(confidence {s.confidence})")

    # ----------------------------------------------------------- selection
    def select_real(self, candidates: Sequence[Tuple[object, object]],
                    regime: str) -> Tuple[Optional[Tuple[object, object]],
                                          List[str]]:
        """Pick which eligible setup gets the single real position.

        UCB-style: ranking score plus an exploration bonus that decays as a
        strategy accumulates trades, so under-tested strategies keep getting
        opportunities and one early winner cannot monopolise the research."""
        cfg = self.cfg
        notes: List[str] = []
        pool: List[Tuple[Tuple[object, object], float]] = []
        total = max(1, self.total_trades)
        for variant, cand in candidates:
            ok, why = self.eligible_for_real(variant, regime)
            if not ok:
                notes.append(f"{variant.sid}: not eligible — {why}")
                continue
            s = self.get(variant.sid, variant.family)
            base = self.score(variant.sid, variant.mutations)
            bonus = cfg.exploration_c * math.sqrt(
                math.log(total + 1.0) / max(1.0, float(s.n)))
            # a higher confluence score breaks ties between equals
            conf_bonus = 0.0
            score_attr = getattr(cand, "confluence", None)
            if score_attr is not None:
                conf_bonus = 0.0015 * float(score_attr.score)
            value = base + bonus + conf_bonus
            pool.append(((variant, cand), value))
            notes.append(f"{variant.sid}: score {base:+.3f} + exploration "
                         f"{bonus:.3f} + confluence {conf_bonus:.3f} = "
                         f"{value:+.3f} ({why})")
        if not pool:
            return None, notes
        pool.sort(key=lambda item: item[1], reverse=True)
        notes.append(f"selected {pool[0][0][0].sid} with value "
                     f"{pool[0][1]:+.3f}")
        return pool[0][0], notes

    # -------------------------------------------------------- daily updates
    def daily_update(self, variants: List, today: str, rng,
                     spawn_fn) -> List[LearningDecision]:
        """Once per day — never after individual trades.

        Retire proven losers, bench slumps for a day, adapt one named
        parameter when the evidence identifies the problem, and spawn bounded
        variants of proven parents."""
        cfg = self.cfg
        decisions: List[LearningDecision] = []

        for v in variants:
            s = self.get(v.sid, v.family)
            if v.status == "benched" and v.bench_until \
                    and v.bench_until <= today:
                v.status = "active"
                v.bench_until = ""
                v.bench_at_n = s.n
                decisions.append(LearningDecision(
                    "UNBENCH", v.sid, f"bench period served; needs new "
                                     f"evidence beyond {s.n} trades before it "
                                     f"can be benched again"))
            if v.status != "active":
                continue
            exp = s.shrunk_expectancy(cfg.shrinkage_k)
            if s.n >= cfg.retire_min_trades and exp <= cfg.retire_expectancy:
                v.status = "retired"
                decisions.append(LearningDecision(
                    "RETIRE", v.sid,
                    f"{s.n} trades, shrunk net expectancy {exp:+.3f}R at or "
                    f"below the {cfg.retire_expectancy:+.2f}R retirement "
                    f"threshold; max drawdown {s.max_dd_r:.2f}R"))
                continue
            recent = s.recent[-cfg.bench_recent_n:]
            if len(recent) >= cfg.bench_recent_n \
                    and sum(recent) <= cfg.bench_recent_sum_r \
                    and s.n > v.bench_at_n:
                v.status = "benched"
                v.bench_until = today
                decisions.append(LearningDecision(
                    "BENCH", v.sid,
                    f"last {len(recent)} trades sum {sum(recent):+.2f}R at or "
                    f"below {cfg.bench_recent_sum_r:+.2f}R — benched for a day"))
                continue
            # evidence-named parameter adaptation
            dec = self._adapt(v, s)
            if dec is not None:
                decisions.append(dec)

        # spawn bounded variants of proven parents
        active = [v for v in variants if v.status == "active"]
        if len(variants) < cfg.max_population:
            parents = sorted(
                [v for v in active
                 if self.get(v.sid).n >= cfg.spawn_parent_min_n
                 and self.score(v.sid, v.mutations) > 0],
                key=lambda v: self.score(v.sid, v.mutations), reverse=True)
            spawned = 0
            for parent in parents:
                if spawned >= cfg.spawn_per_day \
                        or len(variants) >= cfg.max_population:
                    break
                fam_count = sum(1 for v in variants
                                if v.family == parent.family
                                and v.status != "retired")
                if fam_count >= cfg.max_variants_per_family + 2:
                    continue
                child = spawn_fn(parent)
                if child is None:
                    continue
                variants.append(child)
                spawned += 1
                decisions.append(LearningDecision(
                    "SPAWN", child.sid,
                    f"bounded variant of {parent.sid} (score "
                    f"{self.score(parent.sid, parent.mutations):+.3f} over "
                    f"{self.get(parent.sid).n} trades); changed params: "
                    f"{self._diff(parent.params, child.params)}"))
        return decisions

    def _adapt(self, v, s: Stats) -> Optional[LearningDecision]:
        """Adjust ONE stored parameter when the statistics name the problem."""
        cfg = self.cfg
        from .setups_v5 import PARAM_BOUNDS
        if s.n < cfg.adapt_min_n:
            return None
        # evidence: stopped out, and the original target was reached anyway
        if s.stop_watch_events >= 4 \
                and s.stop_too_tight_rate >= cfg.adapt_rate_threshold:
            name = "retest_atr"
            lo, hi = PARAM_BOUNDS[name]
            old = v.params.get(name, lo)
            new = min(hi, old * 1.15)
            if new > old + 1e-9:
                v.params[name] = new
                v.version += 1
                return LearningDecision(
                    "ADAPT", v.sid,
                    f"{s.stop_watch_target_hit} of {s.stop_watch_events} "
                    f"stop-outs went on to reach the original target "
                    f"({s.stop_too_tight_rate:.0%} >= "
                    f"{cfg.adapt_rate_threshold:.0%}): entry tolerance widened "
                    f"so the stop sits further from the retest",
                    name, old, new)
        # evidence: MFE is large but results are not — the runner is giving
        # back too much, so require a slightly earlier breakeven
        if s.n >= cfg.adapt_min_n and s.avg_mfe >= 1.4 \
                and s.expectancy <= 0.05:
            name = "be_min_r"
            lo, hi = PARAM_BOUNDS[name]
            old = v.params.get(name, cfg.be_min_r)
            new = max(lo, old * 0.90)
            if new < old - 1e-9:
                v.params[name] = new
                v.version += 1
                return LearningDecision(
                    "ADAPT", v.sid,
                    f"average MFE {s.avg_mfe:.2f}R but expectancy only "
                    f"{s.expectancy:+.3f}R over {s.n} trades: breakeven floor "
                    f"lowered so justified protection can arrive sooner",
                    name, old, new)
        return None

    @staticmethod
    def _diff(a: Dict[str, float], b: Dict[str, float]) -> str:
        out = []
        for key in sorted(set(a) | set(b)):
            va, vb = a.get(key), b.get(key)
            if va is None or vb is None or abs(va - vb) > 1e-9:
                out.append(f"{key} {va} -> {vb}")
        return ", ".join(out) if out else "none"

    # -------------------------------------------------------------- state io
    def snapshot(self) -> dict:
        return {"variants": {k: v.to_dict() for k, v in self.variants.items()},
                "families": {k: v.to_dict() for k, v in self.families.items()},
                "total_trades": self.total_trades,
                "total_suspect": self.total_suspect}

    def restore(self, snap: dict) -> None:
        self.variants = {str(k): Stats.from_dict(v) for k, v
                         in dict(snap.get("variants", {})).items()}
        self.families = {str(k): Stats.from_dict(v) for k, v
                         in dict(snap.get("families", {})).items()}
        self.total_trades = int(snap.get("total_trades", 0))
        self.total_suspect = int(snap.get("total_suspect", 0))
