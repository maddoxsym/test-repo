"""
Daily / weekly capital-protection locks.

At the start of each trading day the guard snapshots starting equity and
balance, resets the daily trade counter and the realised-loss accumulator.
New entries stop for the REST OF THE DAY when any of these trips:

  * realised daily loss reaches max_daily_loss (default 1%),
  * realised + floating daily loss reaches max_daily_loss,
  * the daily trade cap is reached,
  * the emergency stop is enabled,
  * (extra guards) weekly drawdown or a consecutive-loss streak.

Locks clear ONLY on the natural boundary (next trading day / week) — never
intra-period.  State is held in memory and REBUILT ON RESTART from the
broker's own trade history (the main cBot replays today's closed trades
into the guard at startup), so a mid-day restart cannot bypass the lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Tuple

from ..core.config import Config
from ..core.models import LockReason, SessionName


@dataclass
class DayState:
    day: str                          # ISO date key
    start_equity: float
    start_balance: float
    realised: float = 0.0
    trades_opened: int = 0
    session_trades: Dict[str, int] = field(default_factory=dict)
    wins: int = 0
    losses: int = 0
    min_equity: float = 0.0


@dataclass
class WeekState:
    week: str
    start_equity: float
    realised: float = 0.0
    min_equity: float = 0.0


class DailyLossGuard:

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.day: Optional[DayState] = None
        self.week: Optional[WeekState] = None
        self.consecutive_losses: int = 0
        self.emergency: bool = False
        self._last_lock: LockReason = LockReason.NONE

    # -- period keys -----------------------------------------------------------
    @staticmethod
    def _day_key(t: datetime) -> str:
        return t.date().isoformat()

    @staticmethod
    def _week_key(t: datetime) -> str:
        iso = t.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    def roll(self, t: datetime, equity: float,
             balance: float) -> Tuple[bool, str]:
        """Advance day/week state. Returns (new_day_started, day_key)."""
        new_day = False
        dk = self._day_key(t)
        if self.day is None or self.day.day != dk:
            self.day = DayState(day=dk, start_equity=equity,
                                start_balance=balance, min_equity=equity)
            new_day = True
        wk = self._week_key(t)
        if self.week is None or self.week.week != wk:
            self.week = WeekState(week=wk, start_equity=equity,
                                  min_equity=equity)
        self.day.min_equity = min(self.day.min_equity or equity, equity)
        self.week.min_equity = min(self.week.min_equity or equity, equity)
        return new_day, dk

    # -- trade outcome bookkeeping ----------------------------------------------
    def register_open(self, session: SessionName) -> None:
        if self.day:
            self.day.trades_opened += 1
            key = session.value
            self.day.session_trades[key] = self.day.session_trades.get(key, 0) + 1

    def register_close(self, profit: float) -> None:
        if self.day:
            self.day.realised += profit
            if profit > 0:
                self.day.wins += 1
                self.consecutive_losses = 0
            elif profit < 0:
                self.day.losses += 1
                self.consecutive_losses += 1
        if self.week:
            self.week.realised += profit

    # -- lock evaluation ----------------------------------------------------------
    def lock_reason(self, equity: float, unrealised: float = 0.0,
                    session: Optional[SessionName] = None) -> LockReason:
        cfg = self.cfg
        if self.emergency or cfg.emergency_stop:
            return LockReason.EMERGENCY
        if self.day:
            base = self.day.start_equity or equity
            if base > 0:
                realised_frac = self.day.realised / base
                combined_frac = (self.day.realised + min(0.0, unrealised)) / base
                if realised_frac <= -cfg.max_daily_loss:
                    return LockReason.DAILY_LOSS
                if combined_frac <= -cfg.max_daily_loss:
                    return LockReason.DAILY_LOSS
                if realised_frac >= cfg.daily_profit_hard_stop:
                    return LockReason.DAILY_TARGET
            if self.day.trades_opened >= cfg.max_trades_per_day:
                return LockReason.MAX_TRADES_DAY
            if session and self.day.session_trades.get(session.value, 0) \
                    >= cfg.max_trades_per_session:
                return LockReason.MAX_TRADES_SESSION
        if self.week and self.week.start_equity > 0:
            wk_dd = (self.week.start_equity
                     - min(self.week.min_equity,
                           equity + min(0.0, unrealised))) \
                / self.week.start_equity
            wk_pl = (self.week.realised + min(0.0, unrealised)) \
                / self.week.start_equity
            if wk_dd >= cfg.max_weekly_drawdown or wk_pl <= -cfg.max_weekly_drawdown:
                return LockReason.WEEKLY_LOSS
        if self.consecutive_losses >= cfg.max_consecutive_losses:
            return LockReason.CONSECUTIVE_LOSSES
        return LockReason.NONE

    def lock_changed(self, lock: LockReason) -> bool:
        """True the first time a new lock state appears (for one-shot logs)."""
        changed = lock != self._last_lock
        self._last_lock = lock
        return changed

    def describe(self, equity: float) -> str:
        if not self.day:
            return "day state not initialised"
        base = self.day.start_equity or 1.0
        return (f"day {self.day.day}: start equity {self.day.start_equity:.2f}, "
                f"realised {self.day.realised:+.2f} "
                f"({self.day.realised / base:+.2%}), "
                f"trades {self.day.trades_opened}/{self.cfg.max_trades_per_day}, "
                f"wins {self.day.wins} losses {self.day.losses}, "
                f"consec. losses {self.consecutive_losses}")
