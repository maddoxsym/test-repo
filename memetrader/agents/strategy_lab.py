"""Strategy Lab agent — the AI that improves the other AIs.

Evolutionary parameter search: run full paper-trading epochs on the market
simulator with mutated parameter sets, score each by risk-adjusted return
(final equity penalized by drawdown), keep the elite, breed and mutate.
The champion is persisted to data/best_params.json and every mode
(backtest / live paper) picks it up automatically.

Fitness deliberately penalizes drawdown: a param set that 10x's once by
YOLOing and then halves the book must lose to one that compounds. Each
candidate is scored across multiple market seeds so it can't overfit one
random tape.
"""
from __future__ import annotations

import dataclasses
import random

from ..config import BEST_PARAMS_PATH, RiskParams, StrategyParams

# (field, min, max, is_int) — the knobs evolution may turn
PARAM_SPACE = [
    ("min_safety_score", 40, 85, True),
    ("max_mcap", 100_000, 900_000, False),
    ("grad_min_bonding", 0.6, 0.99, False),
    ("grad_min_holders", 20, 200, True),
    ("grad_max_top10_pct", 0.15, 0.5, False),
    ("grad_min_velocity", 0.0, 0.02, False),
    ("mom_vol_spike", 1.5, 6.0, False),
    ("mom_min_holders", 40, 300, True),
    ("copy_min_smart_buys", 1, 4, True),
    ("dip_drawdown_low", 0.15, 0.45, False),
    ("dip_drawdown_high", 0.45, 0.8, False),
    ("news_boost", 0.0, 0.3, False),
]
RISK_SPACE = [
    ("risk_per_trade", 0.005, 0.05, False),
    ("stop_loss", 0.25, 0.6, False),
    ("trailing_stop", 0.2, 0.5, False),
    ("max_hold_minutes", 180, 1440, False),
    ("max_concurrent_positions", 3, 15, True),
    ("max_per_strategy", 2, 8, True),
    ("spike_sell_threshold", 0.10, 0.50, False),
    ("spike_min_multiple", 1.02, 1.50, False),
    # spike_sell_fraction is deliberately NOT evolvable: exits are 100% out.
    # Day-guard knobs are fixed policy too — evolution once learned to bank
    # $2 days by hugging the profit floor, which is gaming the guard.
]


def _run_epoch(params: StrategyParams, seed: int, ticks: int) -> dict:
    # local import to avoid a circular dependency at module load
    from ..datafeed.simulator import SimMarket
    from ..engine.orchestrator import Orchestrator
    from ..engine.portfolio import Portfolio

    market = SimMarket(seed=seed)
    orch = Orchestrator(params, market, Portfolio(params.risk.starting_bankroll_usd))
    for _ in range(ticks):
        market.step()
        orch.tick(market.now_ts)
    return orch.portfolio.stats()


def fitness(stats: dict) -> float:
    ret = stats["return_pct"] / 100.0
    dd = stats["max_drawdown_pct"] / 100.0
    trades = stats["trades"]
    win = stats["win_rate"]
    if trades < 15:
        return -1.0  # too few trades to prove anything statistically
    # drawdown-penalized return, scaled by accuracy: a param set that wins
    # 90% of its trades beats one that gets the same return winning 60%
    base = ret - 1.5 * dd * max(0.0, ret)
    return base * (0.4 + 0.6 * win)


class StrategyLab:
    name = "strategy_lab"

    def __init__(self, base: StrategyParams | None = None, seed: int = 42):
        self.rng = random.Random(seed)
        self.base = base or StrategyParams()

    # ------------------------------------------------------------------
    def _mutate(self, p: StrategyParams, rate: float = 0.35) -> StrategyParams:
        d = p.to_dict()
        rd = d["risk"]
        for name, lo, hi, is_int in PARAM_SPACE:
            if self.rng.random() < rate:
                span = (hi - lo) * 0.25
                v = d[name] + self.rng.uniform(-span, span)
                d[name] = int(round(min(hi, max(lo, v)))) if is_int else min(hi, max(lo, v))
        for name, lo, hi, is_int in RISK_SPACE:
            if self.rng.random() < rate:
                span = (hi - lo) * 0.25
                v = rd[name] + self.rng.uniform(-span, span)
                rd[name] = int(round(min(hi, max(lo, v)))) if is_int else min(hi, max(lo, v))
        return StrategyParams.from_dict(d)

    def _crossover(self, a: StrategyParams, b: StrategyParams) -> StrategyParams:
        da, db = a.to_dict(), b.to_dict()
        child = {k: (da[k] if self.rng.random() < 0.5 else db[k])
                 for k in da if k != "risk"}
        child["risk"] = {k: (da["risk"][k] if self.rng.random() < 0.5 else db["risk"][k])
                         for k in da["risk"]}
        return StrategyParams.from_dict(child)

    # ------------------------------------------------------------------
    def evolve(self, generations: int = 6, population: int = 10,
               ticks: int = 1440, eval_seeds: tuple = (101, 202, 303),
               verbose: bool = True, save_path: str = BEST_PARAMS_PATH) -> StrategyParams:
        pop = [self.base] + [self._mutate(self.base, 0.6) for _ in range(population - 1)]
        best, best_fit = self.base, float("-inf")

        for gen in range(1, generations + 1):
            scored: list[tuple[float, StrategyParams, dict]] = []
            for cand in pop:
                fits, agg = [], {}
                for s in eval_seeds:
                    stats = _run_epoch(cand, seed=s, ticks=ticks)
                    fits.append(fitness(stats))
                    agg = stats
                scored.append((sum(fits) / len(fits), cand, agg))
            scored.sort(key=lambda x: x[0], reverse=True)

            top_fit, top_cand, top_stats = scored[0]
            if top_fit > best_fit:
                best_fit, best = top_fit, top_cand
            if verbose:
                print(f"gen {gen:>2}: best fitness {top_fit:+.3f} | "
                      f"return {top_stats['return_pct']:+.1f}% | "
                      f"dd {top_stats['max_drawdown_pct']:.1f}% | "
                      f"trades {top_stats['trades']} | "
                      f"win rate {top_stats['win_rate']:.0%}")

            # elitism + breeding
            elite = [c for _, c, _ in scored[: max(2, population // 4)]]
            children = [self._crossover(self.rng.choice(elite), self.rng.choice(elite))
                        for _ in range(population - len(elite))]
            pop = elite + [self._mutate(c) for c in children]

        best.save(save_path)
        if verbose:
            print(f"\nchampion saved -> {save_path}")
        return best
