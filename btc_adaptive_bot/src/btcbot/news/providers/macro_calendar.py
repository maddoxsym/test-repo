"""Scheduled macro-event calendar.

Major US macro releases follow published, highly regular schedules:

* **FOMC rate decisions** — eight per year, statement at 18:00 UTC (14:00 ET)
* **CPI** — monthly, 12:30 UTC (08:30 ET), typically the second week
* **Nonfarm payrolls** — first Friday of the month, 12:30 UTC
* **PPI / retail sales** — monthly, 12:30 UTC

This provider generates *scheduled-window* events from those rules, giving the
risk engine advance warning without depending on any third-party calendar API.

Two honesty notes, both surfaced in the event confidence:

* FOMC dates are only known exactly from the Fed's published calendar. This
  module models the recurring pattern, so its FOMC windows are approximate and
  carry lower confidence than the fixed-time monthly releases.
* These are *pending event* markers, not reports of outcomes. Sentiment is
  always 0.0 — the system reacts to elevated event risk, never to a guess about
  which way a release will land.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ...utils.timeutil import now_utc
from ..base import NewsCategory, NewsEvent, NewsImpact, NewsProvider, cluster_id, make_event_id


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    name: str
    when: datetime
    category: NewsCategory
    impact: NewsImpact
    confidence: float

    @property
    def minutes_until(self) -> float:
        return (self.when - now_utc()).total_seconds() / 60.0


class MacroCalendarProvider(NewsProvider):
    """Emits events for upcoming scheduled macro releases."""

    name = "macro_calendar"
    requires_key = False

    #: How far ahead an event is announced.
    LOOKAHEAD_HOURS = 48
    #: How long after the release the window stays "active".
    TRAILING_HOURS = 2

    async def fetch(self) -> list[NewsEvent]:
        try:
            scheduled = self.upcoming_events()
        except Exception as exc:  # noqa: BLE001 - calendar maths must never stop trading
            self.record_failure(f"{type(exc).__name__}: {exc}")
            return []

        received = now_utc()
        events = [
            NewsEvent(
                event_id=make_event_id(self.name, f"{item.name} {item.when:%Y-%m-%d %H:%M}", item.when),
                provider=self.name,
                source="scheduled_macro_calendar",
                headline=(
                    f"Scheduled macro event: {item.name} at {item.when:%Y-%m-%d %H:%M} UTC"
                ),
                published_ts=received,
                received_ts=received,
                btc_relevance=0.6,
                category=item.category,
                # A pending release has no direction until it prints.
                sentiment=0.0,
                impact=item.impact,
                confidence=item.confidence,
                url=None,
                duplicate_cluster_id=cluster_id(f"{item.name} {item.when:%Y%m%d}"),
                raw={
                    "scheduled_utc": item.when.isoformat(),
                    "minutes_until": round(item.minutes_until, 1),
                    "scheduled_event": True,
                },
            )
            for item in scheduled
        ]
        self.record_success(len(events))
        return events

    def upcoming_events(self, *, reference: datetime | None = None) -> list[ScheduledEvent]:
        """Scheduled events inside the announcement window."""
        now = reference or now_utc()
        horizon = now + timedelta(hours=self.LOOKAHEAD_HOURS)
        trailing = now - timedelta(hours=self.TRAILING_HOURS)

        candidates: list[ScheduledEvent] = []
        for month_offset in (0, 1):
            year, month = _shift_month(now.year, now.month, month_offset)
            candidates.extend(self._month_events(year, month))

        return sorted(
            (e for e in candidates if trailing <= e.when <= horizon), key=lambda e: e.when
        )

    def _month_events(self, year: int, month: int) -> list[ScheduledEvent]:
        events: list[ScheduledEvent] = []

        # Nonfarm payrolls — first Friday, 12:30 UTC. A firm rule.
        first_friday = _nth_weekday(year, month, weekday=4, n=1)
        if first_friday:
            events.append(
                ScheduledEvent(
                    "US Nonfarm Payrolls",
                    first_friday.replace(hour=12, minute=30),
                    NewsCategory.EMPLOYMENT,
                    NewsImpact.HIGH,
                    confidence=0.85,
                )
            )

        # CPI — around the second Tuesday/Wednesday, 12:30 UTC. Day varies, so
        # confidence is lower than for payrolls.
        second_wednesday = _nth_weekday(year, month, weekday=2, n=2)
        if second_wednesday:
            events.append(
                ScheduledEvent(
                    "US CPI (inflation) release",
                    second_wednesday.replace(hour=12, minute=30),
                    NewsCategory.INFLATION_CPI,
                    NewsImpact.HIGH,
                    confidence=0.6,
                )
            )

        # PPI — usually the day after CPI.
        if second_wednesday:
            events.append(
                ScheduledEvent(
                    "US PPI release",
                    (second_wednesday + timedelta(days=1)).replace(hour=12, minute=30),
                    NewsCategory.MACRO_RELEASE,
                    NewsImpact.MEDIUM,
                    confidence=0.5,
                )
            )

        # FOMC — eight meetings a year, roughly every six weeks, decision at
        # 18:00 UTC on a Wednesday. Modelled by pattern, hence low confidence.
        if month in (1, 3, 5, 6, 7, 9, 11, 12):
            third_wednesday = _nth_weekday(year, month, weekday=2, n=3)
            if third_wednesday:
                events.append(
                    ScheduledEvent(
                        "FOMC rate decision (approximate)",
                        third_wednesday.replace(hour=18, minute=0),
                        NewsCategory.FED_POLICY,
                        NewsImpact.HIGH,
                        confidence=0.45,
                    )
                )

        return events


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    index = (month - 1) + offset
    return year + index // 12, index % 12 + 1


def _nth_weekday(year: int, month: int, *, weekday: int, n: int) -> datetime | None:
    """The ``n``-th ``weekday`` (Mon=0) of a month, as a UTC datetime."""
    count = 0
    days_in_month = calendar.monthrange(year, month)[1]
    for day in range(1, days_in_month + 1):
        current = datetime(year, month, day, tzinfo=UTC)
        if current.weekday() == weekday:
            count += 1
            if count == n:
                return current
    return None
