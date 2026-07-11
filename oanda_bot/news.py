"""
NewsSentry -- real-time economic-calendar guard.

Pulls the free Forex Factory weekly calendar feed and blocks NEW trades in a
window around high-impact events for the currencies that move each
instrument (EUR_USD -> EUR+USD, XAU_USD -> USD).

If the feed cannot be refreshed and the cache has gone stale, the sentry
"fails closed" by default: it refuses new trades rather than trading blind
through a news release.  Existing positions are never touched by this guard.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from .config import NewsConfig

log = logging.getLogger("news")

RELEVANT_CURRENCIES = {
    "EUR_USD": {"EUR", "USD"},
    "XAU_USD": {"USD"},        # gold trades against the dollar
}


@dataclass
class CalendarEvent:
    title: str
    currency: str
    impact: str
    time: float                # unix epoch seconds UTC


@dataclass
class NewsVerdict:
    blocked: bool
    reason: str = ""
    event: Optional[CalendarEvent] = None


class NewsSentry:
    def __init__(self, cfg: NewsConfig):
        self.cfg = cfg
        self._events: list[CalendarEvent] = []
        self._last_fetch: float = 0.0
        self._last_success: float = 0.0

    # ------------------------------------------------------------------

    def _refresh(self, now: float) -> None:
        if now - self._last_fetch < self.cfg.refresh_minutes * 60:
            return
        self._last_fetch = now
        try:
            resp = requests.get(self.cfg.feed_url, timeout=10,
                                headers={"User-Agent": "oanda-bot/1.0"})
            resp.raise_for_status()
            raw = resp.json()
            events = []
            for item in raw:
                try:
                    ts = datetime.fromisoformat(item["date"]).timestamp()
                    events.append(CalendarEvent(
                        title=str(item.get("title", "?")),
                        currency=str(item.get("country", "")).upper(),
                        impact=str(item.get("impact", "")).title(),
                        time=ts,
                    ))
                except (KeyError, ValueError):
                    continue
            self._events = events
            self._last_success = now
            log.info("news calendar refreshed: %d events", len(events))
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            log.warning("news feed refresh failed: %s", exc)

    def _cache_is_stale(self, now: float) -> bool:
        return (now - self._last_success) > self.cfg.cache_stale_hours * 3600

    # ------------------------------------------------------------------

    def check(self, instrument: str, now: Optional[float] = None) -> NewsVerdict:
        """Returns a verdict on whether NEW trades on `instrument` are allowed."""
        if not self.cfg.enabled:
            return NewsVerdict(False, "news guard disabled")
        now = now if now is not None else time.time()
        self._refresh(now)

        if not self._events and self._cache_is_stale(now):
            if self.cfg.fail_closed:
                return NewsVerdict(True, "news feed unavailable (failing closed)")
            return NewsVerdict(False, "news feed unavailable (failing open)")

        currencies = RELEVANT_CURRENCIES.get(instrument, {"USD"})
        before = self.cfg.block_before_min * 60
        after = self.cfg.block_after_min * 60
        for ev in self._events:
            if ev.impact not in self.cfg.impacts_blocked:
                continue
            if ev.currency not in currencies:
                continue
            if ev.time - before <= now <= ev.time + after:
                dt = datetime.fromtimestamp(ev.time, tz=timezone.utc)
                return NewsVerdict(
                    True,
                    f"{ev.impact}-impact {ev.currency} event "
                    f"'{ev.title}' at {dt:%H:%M} UTC",
                    ev,
                )
        return NewsVerdict(False, "no blocking news events")

    def upcoming(self, within_hours: float = 12,
                 now: Optional[float] = None) -> list[CalendarEvent]:
        now = now if now is not None else time.time()
        self._refresh(now)
        horizon = now + within_hours * 3600
        return sorted(
            (e for e in self._events
             if now <= e.time <= horizon and e.impact in self.cfg.impacts_blocked),
            key=lambda e: e.time)
