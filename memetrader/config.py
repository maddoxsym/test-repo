"""All tunable parameters live here. The Strategy Lab agent evolves a
`StrategyParams` instance and persists winners to data/best_params.json,
so every knob a strategy uses MUST be declared here to be tunable."""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
BEST_PARAMS_PATH = os.path.join(DATA_DIR, "best_params.json")
SCALP_PARAMS_PATH = os.path.join(DATA_DIR, "best_params_scalp.json")


@dataclass
class RiskParams:
    starting_bankroll_usd: float = 1_000.0
    risk_per_trade: float = 0.02          # fraction of equity per position
    max_concurrent_positions: int = 8
    max_per_strategy: int = 3            # no single strategy may hog the book
    max_position_usd: float = 200.0
    min_position_usd: float = 2.0        # floor so tiny bankrolls ($20) still trade
    stop_loss: float = 0.45               # exit if price falls this fraction from entry
    trailing_stop: float = 0.35           # after first TP, trail the high by this
    # ladder fractions sum to 1.0: exits are ALWAYS 100% out — memecoins
    # are not trusted to trail
    tp_multiples: tuple = (2.0, 4.0, 10.0)   # take-profit ladder (price multiples)
    tp_fractions: tuple = (0.50, 0.30, 0.20) # fraction of ORIGINAL size sold at each rung
    max_hold_minutes: float = 720.0       # time-stop: memecoins that go nowhere bleed
    stale_exit_multiple: float = 1.15     # if below this at time-stop, dump it
    # spike exit: the second price goes vertical while in profit, sell into it
    spike_sell_threshold: float = 0.20    # 1-tick jump that counts as a spike
    spike_sell_fraction: float = 1.00     # 100% out — no partials, no trailing trust
    spike_min_multiple: float = 1.05      # only spike-sell if actually in profit
    # day guard: trade as often as it wants, but protect the day's result.
    # A/B tested: arming too early banks $2 days; +50%/keep-50% maximizes
    # profit while still ending green. Fixed policy — not evolvable.
    daily_loss_limit: float = 0.10        # stop the day if down this much from start
    profit_lock_trigger: float = 0.50     # arm once the day is up this much
    profit_lock_keep: float = 0.50        # then never give back more than this
                                          # fraction of the day's peak profit


@dataclass
class StrategyParams:
    # ---- shared entry filters (every coin is entered at LOW market cap) ----
    min_mcap: float = 15_000.0
    max_mcap: float = 400_000.0           # low-cap only: no chasing pumped charts
    min_liquidity: float = 8_000.0
    max_age_minutes: float = 2_880.0      # only coins younger than 48h (dip buyer exempt)
    min_safety_score: int = 60

    # ---- graduation sniper ----
    grad_min_bonding: float = 0.85        # enter when bonding curve is nearly complete
    grad_min_holders: int = 60
    grad_max_top10_pct: float = 0.32
    grad_min_velocity: float = 0.004      # curve progress/min: accelerating curves
                                          # graduate, plateaued curves die (2026 studies)

    # ---- momentum breakout ----
    mom_vol_spike: float = 3.0            # 5m volume must be X times the hourly baseline
    mom_min_holders: int = 100
    mom_min_volume_5m: float = 6_000.0

    # ---- copy trade ----
    copy_min_smart_buys: int = 2          # distinct profitable wallets in 30 min
    copy_max_mcap: float = 800_000.0      # whales sometimes enter a bit later; allow more room

    # ---- dip buyer (mature survivors only) ----
    dip_min_age_minutes: float = 2_880.0  # >48h old = survived the rug window
    dip_min_mcap: float = 300_000.0
    dip_drawdown_low: float = 0.30
    dip_drawdown_high: float = 0.65

    # ---- news / narrative ----
    news_boost: float = 0.15              # confidence bonus when token matches a hot narrative

    risk: RiskParams = field(default_factory=RiskParams)

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["risk"]["tp_multiples"] = list(self.risk.tp_multiples)
        d["risk"]["tp_fractions"] = list(self.risk.tp_fractions)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyParams":
        d = dict(d)
        risk_d = dict(d.pop("risk", {}))
        if "tp_multiples" in risk_d:
            risk_d["tp_multiples"] = tuple(risk_d["tp_multiples"])
        if "tp_fractions" in risk_d:
            risk_d["tp_fractions"] = tuple(risk_d["tp_fractions"])
        known_risk = {f.name for f in dataclasses.fields(RiskParams)}
        known = {f.name for f in dataclasses.fields(cls)} - {"risk"}
        return cls(
            **{k: v for k, v in d.items() if k in known},
            risk=RiskParams(**{k: v for k, v in risk_d.items() if k in known_risk}),
        )

    def save(self, path: str = BEST_PARAMS_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: str = BEST_PARAMS_PATH) -> "StrategyParams":
        if os.path.exists(path):
            with open(path) as f:
                return cls.from_dict(json.load(f))
        return cls()


def apply_scalp(p: StrategyParams) -> StrategyParams:
    """Quick-flip exit policy: buy, exit 100%, move on. No moonbags, no
    trailing trust — memecoins retrace too violently to trail.

    A/B tested against partial-ladder variants across seeds: full exit at
    +50% (with 100% spike-sells) was the most profitable 100%-out policy.
    Entries stay evolvable; this exit policy is fixed by design."""
    p.risk.tp_multiples = (1.50,)
    p.risk.tp_fractions = (1.00,)
    p.risk.spike_sell_fraction = 1.00
    p.risk.spike_min_multiple = 1.05
    p.risk.stop_loss = 0.25
    p.risk.trailing_stop = 0.18       # safety net between entry and exit rungs
    p.risk.max_hold_minutes = 120.0
    p.risk.stale_exit_multiple = 1.08
    p.risk.daily_loss_limit = 0.10    # day-guard policy, fixed by A/B test
    p.risk.profit_lock_trigger = 0.50
    p.risk.profit_lock_keep = 0.50
    return p


def load_scalp() -> StrategyParams:
    """Scalp params: evolved champion entries if present, with the fixed
    100%-exit policy always applied on top."""
    if os.path.exists(SCALP_PARAMS_PATH):
        return apply_scalp(StrategyParams.load(SCALP_PARAMS_PATH))
    return apply_scalp(StrategyParams.load())
