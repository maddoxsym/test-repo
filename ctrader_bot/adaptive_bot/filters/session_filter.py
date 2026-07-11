"""
Session tracking and entry-window gating.

TIMEZONE MODEL (read this):
Session hours in config.py are expressed in UTC.  The main cBot converts
cTrader Server.Time to UTC using cfg.server_utc_offset_hours before
anything here is called — for most cTrader brokers the server clock IS
UTC, so the default offset of 0 is correct, but verify it once against
your Skilling demo (compare the platform clock with an online UTC clock)
and adjust the offset if needed.  All candle times flow through the same
conversion, so sessions, news windows and candles always agree.

Each session (Asia / London / New York) can be switched off individually.
Session highs/lows are tracked per day from completed candles and feed the
liquidity engine (session-liquidity sweeps and targets).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Optional, Sequence, Tuple

from ..core.config import Config
from ..core.models import Candle, SessionName


@dataclass
class SessionRange:
    name: SessionName
    day: date
    high: Optional[float] = None
    low: Optional[float] = None


class SessionManager:

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._ranges: Dict[Tuple[date, SessionName], SessionRange] = {}

    def session_at(self, t: datetime) -> SessionName:
        h = t.hour
        c = self.cfg
        in_london = c.london_start <= h < c.london_end
        in_ny = c.ny_start <= h < c.ny_end
        if in_london and in_ny:
            return SessionName.OVERLAP
        if in_london:
            return SessionName.LONDON
        if in_ny:
            return SessionName.NEW_YORK
        if c.asia_start <= h < c.asia_end:
            return SessionName.ASIA
        return SessionName.OFF_HOURS

    def session_enabled(self, session: SessionName) -> bool:
        c = self.cfg
        if session == SessionName.ASIA:
            return c.asia_enabled
        if session == SessionName.LONDON:
            return c.london_enabled
        if session == SessionName.NEW_YORK:
            return c.newyork_enabled
        if session == SessionName.OVERLAP:
            return c.london_enabled or c.newyork_enabled
        return False

    # -- session range tracking ------------------------------------------------
    def update_ranges(self, candle: Candle) -> None:
        """Feed each completed base candle to build session highs/lows."""
        t = candle.time
        name = self.session_at(t)
        if name == SessionName.OFF_HOURS:
            return
        keys = [name]
        if name == SessionName.OVERLAP:
            keys = [SessionName.LONDON, SessionName.NEW_YORK]
        for key in keys:
            k = (t.date(), key)
            r = self._ranges.get(k)
            if r is None:
                r = SessionRange(key, t.date())
                self._ranges[k] = r
            r.high = candle.high if r.high is None else max(r.high, candle.high)
            r.low = candle.low if r.low is None else min(r.low, candle.low)

    def rebuild_from(self, candles: Sequence[Candle]) -> None:
        self._ranges.clear()
        for c in candles:
            self.update_ranges(c)

    def marks_for(self, day: date) -> Dict[str, Optional[float]]:
        out: Dict[str, Optional[float]] = {}
        for name in (SessionName.ASIA, SessionName.LONDON, SessionName.NEW_YORK):
            r = self._ranges.get((day, name))
            out[f"{name.value.lower()}_high"] = r.high if r else None
            out[f"{name.value.lower()}_low"] = r.low if r else None
        return out

    def asian_range(self, day: date) -> Optional[Tuple[float, float]]:
        r = self._ranges.get((day, SessionName.ASIA))
        if r and r.high is not None and r.low is not None:
            return (r.low, r.high)
        return None

    # -- entry-time gating ---------------------------------------------------
    def entry_window_check(self, t: datetime) -> Tuple[bool, str]:
        """Return (allowed, reason). Calendar rules in UTC."""
        c = self.cfg
        wd = t.weekday()  # Mon=0 ... Sun=6
        if wd >= 5:
            return False, "weekend"
        if wd == 4 and t.hour >= c.friday_last_entry_hour:
            return False, f"late Friday (after {c.friday_last_entry_hour}:00 UTC)"
        if wd == 0 and t.hour < c.monday_first_entry_hour:
            return False, "early Monday instability window"
        lo, hi = c.avoid_rollover
        if lo <= t.hour < hi:
            return False, f"daily rollover window {lo}-{hi} UTC"
        sess = self.session_at(t)
        if sess == SessionName.OFF_HOURS:
            return False, "off-hours / illiquid"
        if not self.session_enabled(sess):
            return False, f"session {sess.value} disabled in config"
        return True, sess.value

    def near_weekend_flat(self, t: datetime) -> bool:
        return t.weekday() == 4 and t.hour >= self.cfg.friday_flat_hour
