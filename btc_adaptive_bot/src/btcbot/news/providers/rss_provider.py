"""RSS provider — the no-API-key fallback.

Reads published RSS/Atom feeds that publishers offer for exactly this purpose.
This is not scraping: no HTML is parsed, no rate limits are circumvented, and
only feed URLs the operator lists in configuration are fetched.

Parsing uses the standard library's XML parser with entity resolution left at
its safe defaults, so a hostile feed cannot pull in external entities.
"""

from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from ...utils.logging import get_logger
from ...utils.timeutil import now_utc, to_utc
from ..base import (
    NewsEvent,
    NewsProvider,
    cluster_id,
    make_event_id,
)
from ..scoring import (
    classify_category,
    score_confidence,
    score_impact,
    score_relevance,
    score_sentiment,
)

log = get_logger(__name__)

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


class RssNewsProvider(NewsProvider):
    """Fetches and normalises a list of RSS/Atom feeds."""

    name = "rss"
    requires_key = False

    def __init__(self, feeds: list[str], *, timeout_seconds: float = 12.0, max_items: int = 25) -> None:
        super().__init__()
        self.feeds = feeds
        self._timeout = timeout_seconds
        self._max_items = max_items

    def is_configured(self) -> bool:
        return bool(self.feeds)

    async def fetch(self) -> list[NewsEvent]:
        if not self.feeds:
            return []

        events: list[NewsEvent] = []
        failures = 0

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=True,
            headers={"User-Agent": "btc-adaptive-bot/1.0 (research; RSS reader)"},
        ) as client:
            for url in self.feeds:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    events.extend(self._parse(response.text, url))
                except (httpx.HTTPError, ElementTree.ParseError, ValueError) as exc:
                    failures += 1
                    log.debug("NEWS", f"RSS feed failed ({url}): {type(exc).__name__}: {exc}")

        if failures == len(self.feeds):
            self.record_failure(f"all {failures} RSS feeds failed")
        else:
            self.record_success(len(events))
        return events

    def _parse(self, body: str, feed_url: str) -> list[NewsEvent]:
        root = ElementTree.fromstring(body)
        events: list[NewsEvent] = []
        received = now_utc()

        entries = root.findall(".//item") or root.findall(f".//{_ATOM_NS}entry")
        for entry in entries[: self._max_items]:
            headline = _text(entry, "title") or _text(entry, f"{_ATOM_NS}title")
            if not headline:
                continue

            link = _text(entry, "link") or _atom_link(entry)
            published_raw = (
                _text(entry, "pubDate")
                or _text(entry, "published")
                or _text(entry, f"{_ATOM_NS}published")
                or _text(entry, f"{_ATOM_NS}updated")
            )
            published = _parse_date(published_raw) or received

            relevance = score_relevance(headline)
            if relevance <= 0.0:
                continue  # not crypto- or macro-relevant; do not store noise

            category = classify_category(headline)
            events.append(
                NewsEvent(
                    event_id=make_event_id(self.name, headline, published),
                    provider=self.name,
                    source=_domain(feed_url),
                    headline=headline.strip(),
                    published_ts=published,
                    received_ts=received,
                    btc_relevance=relevance,
                    category=category,
                    sentiment=score_sentiment(headline),
                    impact=score_impact(headline, relevance, category),
                    confidence=score_confidence(headline, relevance, feed_url),
                    url=link,
                    duplicate_cluster_id=cluster_id(headline),
                    raw={"feed": feed_url},
                )
            )
        return events


def _text(element: ElementTree.Element, tag: str) -> str | None:
    found = element.find(tag)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _atom_link(entry: ElementTree.Element) -> str | None:
    link = entry.find(f"{_ATOM_NS}link")
    if link is not None:
        return link.get("href")
    return None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return to_utc(parsedate_to_datetime(value))
    except (TypeError, ValueError):
        pass
    try:
        return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _domain(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return url[:60]
