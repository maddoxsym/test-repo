"""Optional API-key news providers.

Both are strictly optional. When the corresponding environment variable is
unset, :meth:`is_configured` returns ``False`` and the engine skips the provider
without any error — the system runs on RSS plus the macro calendar alone.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from ...config.loader import optional_env
from ...utils.logging import get_logger
from ...utils.timeutil import ms_to_dt, now_utc, to_utc
from ..base import NewsEvent, NewsProvider, cluster_id, make_event_id
from ..scoring import (
    classify_category,
    score_confidence,
    score_impact,
    score_relevance,
    score_sentiment,
)

log = get_logger(__name__)


class CryptoPanicProvider(NewsProvider):
    """CryptoPanic aggregator (free tier available).

    Uses the provider's own bullish/bearish vote counts when present, which is a
    genuine crowd signal, and falls back to headline lexicon scoring otherwise.
    """

    name = "cryptopanic"
    requires_key = True
    ENDPOINT = "https://cryptopanic.com/api/v1/posts/"

    def __init__(self, *, timeout_seconds: float = 12.0) -> None:
        super().__init__()
        self._api_key = optional_env("CRYPTOPANIC_API_KEY")
        self._timeout = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def fetch(self) -> list[NewsEvent]:
        if not self._api_key:
            return []
        params = {"auth_token": self._api_key, "currencies": "BTC", "public": "true"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
                response = await client.get(self.ENDPOINT, params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.record_failure(f"{type(exc).__name__}: {exc}")
            return []

        received = now_utc()
        events: list[NewsEvent] = []
        for item in payload.get("results", [])[:40]:
            headline = (item.get("title") or "").strip()
            if not headline:
                continue
            published = _parse_iso(item.get("published_at")) or received
            relevance = max(0.7, score_relevance(headline))  # already BTC-filtered
            category = classify_category(headline)

            votes = item.get("votes") or {}
            positive = int(votes.get("positive", 0)) + int(votes.get("liked", 0))
            negative = int(votes.get("negative", 0)) + int(votes.get("disliked", 0))
            total_votes = positive + negative
            sentiment = (
                (positive - negative) / total_votes if total_votes >= 3
                else score_sentiment(headline)
            )

            events.append(
                NewsEvent(
                    event_id=make_event_id(self.name, headline, published),
                    provider=self.name,
                    source=(item.get("source") or {}).get("domain", "cryptopanic"),
                    headline=headline,
                    published_ts=published,
                    received_ts=received,
                    btc_relevance=relevance,
                    category=category,
                    sentiment=float(sentiment),
                    impact=score_impact(headline, relevance, category),
                    confidence=score_confidence(headline, relevance, self.name),
                    url=item.get("url"),
                    duplicate_cluster_id=cluster_id(headline),
                    raw={"votes": votes},
                )
            )

        self.record_success(len(events))
        return events


class NewsApiProvider(NewsProvider):
    """NewsAPI.org — broad coverage, requires a key."""

    name = "newsapi"
    requires_key = True
    ENDPOINT = "https://newsapi.org/v2/everything"

    def __init__(self, *, timeout_seconds: float = 12.0) -> None:
        super().__init__()
        self._api_key = optional_env("NEWSAPI_API_KEY")
        self._timeout = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def fetch(self) -> list[NewsEvent]:
        if not self._api_key:
            return []
        params = {
            "q": "bitcoin OR cryptocurrency OR federal reserve OR inflation",
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": "40",
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
                response = await client.get(
                    self.ENDPOINT, params=params, headers={"X-Api-Key": self._api_key}
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.record_failure(f"{type(exc).__name__}: {exc}")
            return []

        received = now_utc()
        events: list[NewsEvent] = []
        for item in payload.get("articles", []):
            headline = (item.get("title") or "").strip()
            if not headline:
                continue
            relevance = score_relevance(headline)
            if relevance < 0.5:
                continue
            published = _parse_iso(item.get("publishedAt")) or received
            category = classify_category(headline)
            events.append(
                NewsEvent(
                    event_id=make_event_id(self.name, headline, published),
                    provider=self.name,
                    source=(item.get("source") or {}).get("name", "newsapi"),
                    headline=headline,
                    published_ts=published,
                    received_ts=received,
                    btc_relevance=relevance,
                    category=category,
                    sentiment=score_sentiment(headline),
                    impact=score_impact(headline, relevance, category),
                    confidence=score_confidence(headline, relevance, self.name),
                    url=item.get("url"),
                    duplicate_cluster_id=cluster_id(headline),
                    raw={},
                )
            )

        self.record_success(len(events))
        return events


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        pass
    try:
        return ms_to_dt(int(value))
    except (TypeError, ValueError):
        return None
