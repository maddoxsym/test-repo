"""Gold trader configuration.

Hard constraints (engineered for Islamic-finance compliance — verify with
your own scholar; this is engineering, not a fatwa):
  * SPOT ONLY: the bot models buying/selling spot gold with immediate
    settlement. No futures, no CFDs, no options.
  * NO LEVERAGE: position size can never exceed available cash.
  * NO SHORT SELLING: long-only — the bot only ever sells gold it holds.
  * NO SWAPS/INTEREST: nothing in the model earns or pays overnight
    interest; brokers offering "Islamic / swap-free" accounts match this.

Scale reality check: unleveraged spot gold moves ~0.5-1.5% per day.
A $20 account trading it perfectly earns cents per day. That is the
honest cost of keeping the leverage out.
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field

from memetrader.config import DATA_DIR, RiskParams

GOLD_PARAMS_PATH = os.path.join(DATA_DIR, "gold_params.json")


def gold_risk() -> RiskParams:
    """RiskParams re-scaled from memecoin magnitudes to gold magnitudes."""
    return RiskParams(
        starting_bankroll_usd=20.0,
        risk_per_trade=0.015,            # 1.5% of equity risked per trade: the
                                         # stop distance then sets the notional
        max_concurrent_positions=1,      # only ONE position, no pyramiding
        max_per_strategy=1,
        max_position_usd=1_000_000.0,
        min_position_usd=2.0,
        stop_loss=0.006,                 # -0.6% hard stop
        trailing_stop=0.004,
        tp_multiples=(1.005,),           # +0.5% -> exit 100% (quick, full exits)
        tp_fractions=(1.0,),
        max_hold_minutes=600.0,          # never hold stale intraday positions
        stale_exit_multiple=1.0002,
        spike_sell_threshold=0.0025,     # 1-min vertical -> sell into it
        spike_sell_fraction=1.0,
        spike_min_multiple=1.0015,
        daily_loss_limit=0.05,           # day guard at leveraged-gold scale:
        profit_lock_trigger=0.04,        # ~3 losing trades stops the day,
        profit_lock_keep=0.50,           # +4% arms the profit lock
    )


@dataclass
class GoldParams:
    # execution mode
    exit_style: str = "trail"            # "trail" | "breakeven" | "binary"
    allow_short: bool = True             # set False for long-only (spot/Islamic)
    max_leverage: float = 100.0          # broker cap (1:100 account). Actual
                                         # leverage used = risk_per_trade /
                                         # stop_loss; set 1.0 for unleveraged

    # trend following (EMA crossover)
    ema_fast: int = 20                   # minutes
    ema_slow: int = 60
    trend_min_slope: float = 0.00003     # slow EMA must actually rise

    # mean reversion (z-score dip buying in ranging markets)
    mr_window: int = 45
    mr_entry_z: float = -1.6             # buy dips this deep
    mr_max_trend_z: float = 0.8          # only when not strongly trending

    # breakout (rolling-high break with range expansion)
    bo_lookback: int = 120
    bo_min_range_expansion: float = 1.4

    risk: RiskParams = field(default_factory=gold_risk)

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["risk"]["tp_multiples"] = list(self.risk.tp_multiples)
        d["risk"]["tp_fractions"] = list(self.risk.tp_fractions)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "GoldParams":
        d = dict(d)
        risk_d = dict(d.pop("risk", {}))
        if "tp_multiples" in risk_d:
            risk_d["tp_multiples"] = tuple(risk_d["tp_multiples"])
        if "tp_fractions" in risk_d:
            risk_d["tp_fractions"] = tuple(risk_d["tp_fractions"])
        known_risk = {f.name for f in dataclasses.fields(RiskParams)}
        known = {f.name for f in dataclasses.fields(cls)} - {"risk"}
        base = gold_risk()
        for k, v in risk_d.items():
            if k in known_risk:
                setattr(base, k, v)
        return cls(**{k: v for k, v in d.items() if k in known}, risk=base)

    def save(self, path: str = GOLD_PARAMS_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: str = GOLD_PARAMS_PATH) -> "GoldParams":
        if os.path.exists(path):
            with open(path) as f:
                return cls.from_dict(json.load(f))
        return cls()
