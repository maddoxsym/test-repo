"""
Trade-count limits and duplicate-entry prevention.

The final can_open() combines every account-protection rule into a single
yes/no with a reason.  All checks are pure reads — nothing here ever
increases risk.  There is no martingale, no grid, no averaging down, no
recovery mode anywhere in this codebase.
"""

from __future__ import annotations

from typing import Optional, Tuple

from ..core.config import Config
from ..core.models import LockReason, SessionName
from .daily_loss_guard import DailyLossGuard


class TradeLimits:

    def __init__(self, cfg: Config, guard: DailyLossGuard):
        self.cfg = cfg
        self.guard = guard

    def can_open(self, equity: float, open_positions: int,
                 open_risk_money: float, new_risk_money: float,
                 unrealised: float = 0.0,
                 session: Optional[SessionName] = None) -> Tuple[bool, str]:
        cfg = self.cfg
        lock = self.guard.lock_reason(equity, unrealised, session)
        if lock != LockReason.NONE:
            return False, f"lock active: {lock.value}"
        if open_positions >= cfg.max_positions:
            return False, (f"max positions ({cfg.max_positions}) reached — "
                           f"one open GOLD position rule")
        if equity <= 0:
            return False, "equity not verifiable"
        if new_risk_money <= 0:
            return False, "new trade has no measurable risk (sizing failed)"
        max_total = equity * cfg.max_risk_per_trade * cfg.max_positions
        if open_risk_money + new_risk_money > max_total + 1e-9:
            return False, (f"combined open risk "
                           f"{(open_risk_money + new_risk_money) / equity:.2%} "
                           f"> allowed {max_total / equity:.2%}")
        return True, "ok"
