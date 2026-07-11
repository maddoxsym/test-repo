"""
Order safety: the full pre-order checklist from the spec, evaluated as pure
logic.  The main cBot gathers the live facts (account type, symbol state,
market open, spread, open positions, news, locks), passes them in, and only
sends the order if EVERY check passes.  The actual ExecuteMarketOrder call
lives in the main file — this module never touches the cAlgo API, which is
what makes the checklist testable outside cTrader.

Also enforces the no-spam rule: after a failed order request the manager
imposes a cooldown before any new order may be attempted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from ..core.config import Config
from ..core.models import LockReason, Setup
from ..risk.position_sizing import SizingResult


@dataclass
class OrderFacts:
    """Everything the checklist needs, gathered by the main cBot."""
    is_demo_account: bool
    symbol_is_gold: bool
    market_open: bool
    spread_points: float
    spread_ok: bool
    open_positions_count: int
    has_pending_bot_order: bool       # duplicate prevention
    news_blocked: bool
    news_reason: str
    lock: LockReason
    equity: float
    session_allowed: bool
    session_reason: str


@dataclass
class Preflight:
    ok: bool
    checks: List[str] = field(default_factory=list)   # "PASS/FAIL — detail"
    reason: str = ""                                  # first failure


class OrderManager:

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cooldown_until: Optional[datetime] = None
        self._last_failure: str = ""

    # ---- failure cooldown (no order spam) ---------------------------------
    def order_failed(self, now: datetime, broker_message: str) -> None:
        self._cooldown_until = now + timedelta(
            minutes=self.cfg.order_fail_cooldown_min)
        self._last_failure = broker_message

    def order_succeeded(self) -> None:
        self._cooldown_until = None
        self._last_failure = ""

    def in_cooldown(self, now: datetime) -> bool:
        return self._cooldown_until is not None and now < self._cooldown_until

    # ---- the checklist ------------------------------------------------------
    def preflight(self, now: datetime, setup: Setup, sizing: SizingResult,
                  facts: OrderFacts) -> Preflight:
        cfg = self.cfg
        checks: List[str] = []
        failures: List[str] = []

        def check(name: str, passed: bool, detail: str) -> None:
            checks.append(f"{'PASS' if passed else 'FAIL'} — {name}: {detail}")
            if not passed:
                failures.append(f"{name}: {detail}")

        check("demo account", facts.is_demo_account,
              "account is demo" if facts.is_demo_account
              else "LIVE ACCOUNT — trading refused")
        check("gold symbol", facts.symbol_is_gold,
              "symbol verified as gold" if facts.symbol_is_gold
              else "symbol is not an approved gold alias")
        check("market open", facts.market_open,
              "market open" if facts.market_open else "market closed")
        check("spread", facts.spread_ok,
              f"{facts.spread_points:.0f} pts (max {cfg.max_spread_points:.0f})")
        check("stop loss present", setup.stop_price > 0
              and setup.stop_price != setup.entry_price,
              f"SL {setup.stop_price:.2f}")
        check("take profit present", setup.tp1 > 0
              and setup.tp1 != setup.entry_price,
              f"TP {setup.tp1:.2f}")
        sl_side_ok = (setup.stop_price < setup.entry_price < setup.tp1
                      if setup.direction.value == "LONG"
                      else setup.tp1 < setup.entry_price < setup.stop_price)
        check("SL/TP on correct sides", sl_side_ok,
              f"entry {setup.entry_price:.2f} SL {setup.stop_price:.2f} "
              f"TP {setup.tp1:.2f} ({setup.direction.value})")
        check("volume valid", not sizing.rejected and sizing.volume_units > 0,
              f"{sizing.volume_units} units" if not sizing.rejected
              else sizing.reason)
        risk_ok = (facts.equity > 0 and not sizing.rejected
                   and sizing.risk_money
                   <= facts.equity * cfg.max_risk_per_trade * 1.0001)
        check("risk below maximum", risk_ok,
              f"{sizing.risk_money:.2f} "
              f"({sizing.risk_fraction_actual:.2%} of equity, "
              f"max {cfg.max_risk_per_trade:.2%})" if not sizing.rejected
              else sizing.reason)
        check("no daily lock", facts.lock == LockReason.NONE,
              "no lock" if facts.lock == LockReason.NONE
              else f"lock: {facts.lock.value}")
        check("no other GOLD position", facts.open_positions_count == 0,
              f"{facts.open_positions_count} open position(s)")
        check("no duplicate pending order", not facts.has_pending_bot_order,
              "none" if not facts.has_pending_bot_order
              else "a bot order is already pending")
        check("news clear", not facts.news_blocked,
              "clear" if not facts.news_blocked else facts.news_reason)
        check("session allowed", facts.session_allowed, facts.session_reason)
        check("score above minimum", setup.score >= cfg.min_score,
              f"{setup.score:.1f} >= {cfg.min_score:.1f}")
        cooled = not self.in_cooldown(now)
        check("no failure cooldown", cooled,
              "clear" if cooled else
              f"cooling down after order failure: {self._last_failure}")

        return Preflight(ok=not failures, checks=checks,
                         reason=failures[0] if failures else "")
