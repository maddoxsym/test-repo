"""
News protection — HONEST LIMITATION FIRST:

cTrader Python cBots have no reliable built-in economic calendar, and this
bot makes NO network calls and uses NO API keys.  News protection here is
therefore SCHEDULE-BASED AND MANUAL — the bot never fabricates events and
never pretends to have live coverage:

  * NFP        : blocked automatically via its regular schedule (first
                 Friday of the month at nfp_hour_utc:nfp_minute_utc).
                 Occasionally the BLS shifts the date — around holidays,
                 verify manually.
  * FOMC / CPI / central-bank speeches: blocked ONLY if you maintain the
                 date lists in config.py ("YYYY-MM-DDTHH:MM", UTC).
  * Extra manual blackout windows: "start/end" ISO pairs in UTC.

Every rejection caused by news is logged with the exact event/window that
caused it.  protection_complete() always returns False so the setup scorer
can never award the full news-safety score to manual-only protection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from ..core.config import Config


@dataclass
class NewsEvent:
    time: datetime
    title: str
    category: str        # "NFP" / "FOMC" / "CPI" / "SPEECH" / "MANUAL"


def _parse_iso_utc(s: str, tzinfo) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(s.strip())
        if t.tzinfo is None:
            t = t.replace(tzinfo=tzinfo)
        return t
    except ValueError:
        return None


def first_friday(year: int, month: int) -> int:
    """Day-of-month of the first Friday."""
    d = datetime(year, month, 1)
    offset = (4 - d.weekday()) % 7      # Friday = 4
    return 1 + offset


class NewsFilter:

    def __init__(self, cfg: Config, tzinfo):
        """tzinfo: timezone used for all bot times (UTC)."""
        self.cfg = cfg
        self.tz = tzinfo
        self._events: List[NewsEvent] = []
        self._windows: List[Tuple[datetime, datetime, str]] = []
        self._malformed: List[str] = []
        self._load()

    def _load(self) -> None:
        c = self.cfg
        sources = (("FOMC", c.fomc_events, c.block_fomc),
                   ("CPI", c.cpi_events, c.block_cpi),
                   ("SPEECH", c.speech_events, c.block_speeches))
        for category, entries, enabled in sources:
            if not enabled:
                continue
            for s in entries:
                t = _parse_iso_utc(s, self.tz)
                if t is None:
                    self._malformed.append(f"{category}: {s}")
                    continue
                self._events.append(NewsEvent(t, f"{category} (configured)",
                                              category))
        for w in c.manual_blackouts:
            try:
                a, b = w.split("/")
                t0 = _parse_iso_utc(a, self.tz)
                t1 = _parse_iso_utc(b, self.tz)
                if t0 and t1 and t1 > t0:
                    self._windows.append((t0, t1, "manual blackout"))
                else:
                    self._malformed.append(f"MANUAL: {w}")
            except ValueError:
                self._malformed.append(f"MANUAL: {w}")

    def malformed_entries(self) -> List[str]:
        """Config entries that could not be parsed (report at startup)."""
        return list(self._malformed)

    def _nfp_event_for(self, t: datetime) -> Optional[NewsEvent]:
        if not self.cfg.block_nfp:
            return None
        day = first_friday(t.year, t.month)
        nfp = datetime(t.year, t.month, day, self.cfg.nfp_hour_utc,
                       self.cfg.nfp_minute_utc, tzinfo=self.tz)
        return NewsEvent(nfp, "NFP (first-Friday schedule)", "NFP")

    def blackout(self, t: datetime) -> Tuple[bool, str]:
        """(blocked, reason) for time t. t must be UTC (tz-aware)."""
        c = self.cfg
        if not c.news_enabled:
            return False, "news protection disabled"
        for t0, t1, label in self._windows:
            if t0 <= t <= t1:
                return True, (f"{label} {t0:%Y-%m-%d %H:%M}-"
                              f"{t1:%H:%M} UTC")
        before = timedelta(minutes=c.news_block_before_min)
        after = timedelta(minutes=c.news_block_after_min)
        candidates = list(self._events)
        nfp = self._nfp_event_for(t)
        if nfp is not None:
            candidates.append(nfp)
            # also consider next month's NFP when t is near month end
            if t.month == 12:
                nxt = t.replace(year=t.year + 1, month=1, day=1)
            else:
                nxt = t.replace(month=t.month + 1, day=1)
            day = first_friday(nxt.year, nxt.month)
            candidates.append(NewsEvent(
                datetime(nxt.year, nxt.month, day, c.nfp_hour_utc,
                         c.nfp_minute_utc, tzinfo=self.tz),
                "NFP (first-Friday schedule)", "NFP"))
        for ev in candidates:
            if ev.time - before <= t <= ev.time + after:
                return True, (f"{ev.title} at {ev.time:%Y-%m-%d %H:%M} UTC "
                              f"(blocking {c.news_block_before_min}m before / "
                              f"{c.news_block_after_min}m after)")
        return False, "no scheduled news in window"

    def protection_complete(self) -> bool:
        """Manual/schedule-based protection is NEVER complete — this
        deliberately caps the news-safety score and is logged at startup."""
        return False

    def upcoming(self, t: datetime, within_hours: float = 24) -> List[NewsEvent]:
        horizon = t + timedelta(hours=within_hours)
        out = [e for e in self._events if t <= e.time <= horizon]
        nfp = self._nfp_event_for(t)
        if nfp and t <= nfp.time <= horizon:
            out.append(nfp)
        return sorted(out, key=lambda e: e.time)
