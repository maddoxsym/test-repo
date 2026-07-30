"""
The research clock, measured in ACTIVE TRADING DAYS.

V4 counted calendar days, so a 14-day run silently spent 4 of its days on
weekends.  V5 counts a day only once the bot has actually observed
`active_day_min_minutes` of open market on that date.  Weekends, holidays,
platform outages and computer sleep therefore cost no research days.

The start timestamp is written once and never overwritten: on restore the
EARLIEST known start wins, and observed minutes are merged by taking the
maximum per date, so a restart can neither reset the clock nor double-count a
day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Optional


@dataclass
class ResearchClock:
    cfg: object
    start: Optional[datetime] = None
    minutes: Dict[str, float] = field(default_factory=dict)
    final_report_done: bool = False

    # ------------------------------------------------------------------ setup
    def begin(self, now: datetime) -> bool:
        """Record the first successful start. Returns True if newly started."""
        if self.start is None:
            self.start = now
            return True
        return False

    # ---------------------------------------------------------- observations
    def observe(self, now: datetime, market_open: bool,
                minutes: float = 1.0) -> None:
        """Credit observed open-market time to today's date.

        Called once per completed M1 bar, so one bar = one minute."""
        if not market_open or now.weekday() >= 5:
            return
        key = now.date().isoformat()
        self.minutes[key] = self.minutes.get(key, 0.0) + minutes

    def is_active_day(self, day: date) -> bool:
        return self.minutes.get(day.isoformat(), 0.0) \
            >= self.cfg.active_day_min_minutes

    # -------------------------------------------------------------- counting
    def completed_days(self) -> int:
        """Dates that reached the active-minutes threshold."""
        threshold = self.cfg.active_day_min_minutes
        return sum(1 for value in self.minutes.values() if value >= threshold)

    def day_index(self, now: datetime) -> int:
        """1-based index of the day currently being researched."""
        done = self.completed_days()
        today_qualifies = self.is_active_day(now.date())
        index = done if today_qualifies else done + 1
        return max(1, min(self.cfg.research_days, index))

    def is_over(self) -> bool:
        return self.completed_days() >= self.cfg.research_days

    def remaining_days(self) -> int:
        return max(0, self.cfg.research_days - self.completed_days())

    def describe(self, now: datetime) -> str:
        today = self.minutes.get(now.date().isoformat(), 0.0)
        return (f"day {self.day_index(now)}/{self.cfg.research_days} active "
                f"trading days ({self.completed_days()} complete, "
                f"{today:.0f} min observed today, threshold "
                f"{self.cfg.active_day_min_minutes})")

    # -------------------------------------------------------------- state io
    def snapshot(self) -> dict:
        return {"start": self.start.isoformat() if self.start else "",
                "minutes": dict(self.minutes),
                "final_report_done": self.final_report_done}

    def restore(self, snap: dict) -> None:
        """Merge persisted state without ever losing research progress."""
        raw = str(snap.get("start", ""))
        if raw:
            try:
                saved = datetime.fromisoformat(raw)
            except ValueError:
                saved = None
            if saved is not None:
                # the EARLIEST start wins: the clock can never be reset
                self.start = saved if self.start is None \
                    else min(self.start, saved)
        for key, value in dict(snap.get("minutes", {})).items():
            try:
                observed = float(value)
            except (TypeError, ValueError):
                continue
            # maximum per date: replaying a day cannot double-count it
            self.minutes[str(key)] = max(self.minutes.get(str(key), 0.0),
                                         observed)
        self.final_report_done = bool(snap.get("final_report_done", False)) \
            or self.final_report_done
