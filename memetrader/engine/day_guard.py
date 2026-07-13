"""Day guard — trade as many times as you like, but protect the day.

Two circuit breakers, checked against equity every tick:

  * PROFIT LOCK: once the day's equity peak clears the trigger (+10% by
    default), a floor ratchets upward beneath it. Touch the floor and the
    bot liquidates everything and stops for the day — the green day is
    banked instead of round-tripped.
  * DAILY LOSS STOP: if the day never got going and equity drops the
    daily loss limit (-10% by default), liquidate and stop. A losing day
    is capped at a scratch, never a blowup.

This cannot make every day profitable — nothing honest can — but it
converts "was profitable at some point today" into "ENDED profitable",
and shrinks the bad days to noise.
"""
from __future__ import annotations

from ..config import RiskParams


class DayGuard:
    def __init__(self, risk: RiskParams, start_equity: float):
        self.risk = risk
        self.start = start_equity
        self.peak = start_equity
        self.halted = False
        self.reason = ""

    @property
    def armed(self) -> bool:
        """Has the day been green enough to defend?"""
        return self.peak >= self.start * (1.0 + self.risk.profit_lock_trigger)

    def floor(self) -> float:
        if self.armed:
            return self.start + self.risk.profit_lock_keep * (self.peak - self.start)
        return self.start * (1.0 - self.risk.daily_loss_limit)

    def check(self, equity: float) -> bool:
        """Update with current equity; returns True when trading must halt."""
        if self.halted:
            return True
        self.peak = max(self.peak, equity)
        if equity <= self.floor():
            self.halted = True
            self.reason = "profit_lock" if self.armed else "daily_loss_stop"
        return self.halted
