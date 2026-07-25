"""News provider interface and the event model.

Providers are adapters: each knows how to fetch from one legitimate source and
normalise the result into :class:`NewsEvent`. A provider that is unavailable
(no API key, network failure, malformed response) returns an empty list and
marks itself degraded — it never raises into the trading loop.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..utils.timeutil import iso, now_utc, to_utc


class NewsCategory(str, Enum):
    BITCOIN = "bitcoin"
    CRYPTO_REGULATION = "crypto_regulation"
    EXCHANGE_INCIDENT = "exchange_incident"
    ETF_INSTITUTIONAL = "etf_institutional"
    SECURITY_HACK = "security_hack"
    STABLECOIN = "stablecoin"
    CRYPTO_COMPANY = "crypto_company"
    FED_POLICY = "fed_policy"
    INFLATION_CPI = "inflation_cpi"
    INTEREST_RATES = "interest_rates"
    EMPLOYMENT = "employment"
    MACRO_RELEASE = "macro_release"
    GEOPOLITICAL = "geopolitical"
    OTHER = "other"


class NewsImpact(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def weight(self) -> float:
        return {"low": 0.2, "medium": 0.55, "high": 1.0}[self.value]


@dataclass(frozen=True, slots=True)
class NewsEvent:
    """One normalised news or macro event.

    ``received_ts`` is the field the point-in-time guard filters on: a decision
    may only use events the system had actually *received* before it, regardless
    of when they were published.
    """

    event_id: str
    provider: str
    source: str
    headline: str
    published_ts: datetime
    received_ts: datetime
    btc_relevance: float
    category: NewsCategory
    sentiment: float          # -1 bearish … +1 bullish
    impact: NewsImpact
    confidence: float
    url: str | None = None
    duplicate_cluster_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def freshness_minutes(self) -> float:
        return max(0.0, (now_utc() - to_utc(self.received_ts)).total_seconds() / 60.0)

    @property
    def age_minutes(self) -> float:
        return max(0.0, (now_utc() - to_utc(self.published_ts)).total_seconds() / 60.0)

    def to_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "provider": self.provider,
            "source": self.source,
            "headline": self.headline[:500],
            "url": self.url,
            "published_ts_utc": iso(self.published_ts),
            "received_ts_utc": iso(self.received_ts),
            "btc_relevance": self.btc_relevance,
            "category": self.category.value,
            "sentiment": self.sentiment,
            "impact": self.impact.value,
            "freshness_minutes": self.freshness_minutes,
            "confidence": self.confidence,
            "duplicate_cluster_id": self.duplicate_cluster_id,
            "raw": self.raw,
        }


def make_event_id(provider: str, headline: str, published: datetime) -> str:
    """Stable ID so the same article from one provider is never stored twice."""
    raw = f"{provider}|{normalise_headline(headline)}|{int(to_utc(published).timestamp() // 3600)}"
    return "news_" + hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()


_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "as", "at",
        "by", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that",
        "from", "after", "before", "amid", "says", "say", "said", "new", "will", "has",
        "have", "had", "into", "over", "up", "down", "out", "about", "more", "than",
    }
)

#: Suffixes stripped so "approves", "approved" and "approval" collapse together.
_SUFFIXES = ("ations", "ation", "ings", "ing", "ies", "ed", "es", "als", "al", "s")

#: Token-overlap threshold above which two headlines are the same story.
DUPLICATE_SIMILARITY_THRESHOLD = 0.5


def normalise_headline(headline: str) -> str:
    return " ".join(_WORD_RE.findall(headline.lower()))


def _stem(word: str) -> str:
    """Very light suffix stripping — enough for headline morphology.

    Not a linguistic stemmer, and deliberately so: an aggressive stemmer merges
    genuinely different stories, which is a worse failure than missing a
    duplicate.
    """
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def headline_tokens(headline: str) -> frozenset[str]:
    """Significant, stemmed tokens of a headline."""
    return frozenset(
        _stem(word)
        for word in normalise_headline(headline).split()
        if word not in _STOPWORDS and len(word) > 2
    )


def cluster_id(headline: str) -> str:
    """Deterministic signature for a headline's token set.

    Used as the identifier of a *new* cluster. Matching a headline to an
    already-seen cluster is similarity-based — see :class:`HeadlineDeduplicator`
    — because two outlets rarely choose the same words for the same story.
    """
    signature = " ".join(sorted(headline_tokens(headline)))
    return "clu_" + hashlib.blake2b(signature.encode("utf-8"), digest_size=8).hexdigest()


def similarity(first: frozenset[str], second: frozenset[str]) -> float:
    """Jaccard overlap of two token sets."""
    if not first or not second:
        return 0.0
    intersection = len(first & second)
    if intersection == 0:
        return 0.0
    return intersection / len(first | second)


class HeadlineDeduplicator:
    """Groups headlines describing the same story into one cluster.

    Ten outlets reporting one ETF approval is one event, not ten. Exact hashing
    cannot see that — "SEC approves Bitcoin ETF" and "Bitcoin ETF approved by
    SEC" share no word order — so clusters are matched by token overlap against
    recently-seen stories instead.
    """

    __slots__ = ("_clusters", "_threshold", "_capacity")

    def __init__(
        self,
        *,
        threshold: float = DUPLICATE_SIMILARITY_THRESHOLD,
        capacity: int = 400,
    ) -> None:
        self._clusters: list[tuple[str, frozenset[str]]] = []
        self._threshold = threshold
        self._capacity = capacity

    def resolve(self, headline: str) -> tuple[str, bool]:
        """Return ``(cluster_id, is_new_story)`` for ``headline``."""
        tokens = headline_tokens(headline)
        if not tokens:
            return (cluster_id(headline), True)

        best_id: str | None = None
        best_score = 0.0
        for existing_id, existing_tokens in reversed(self._clusters):
            score = similarity(tokens, existing_tokens)
            if score > best_score:
                best_id, best_score = existing_id, score

        if best_id is not None and best_score >= self._threshold:
            return (best_id, False)

        new_id = cluster_id(headline)
        self._clusters.append((new_id, tokens))
        if len(self._clusters) > self._capacity:
            del self._clusters[: len(self._clusters) - self._capacity]
        return (new_id, True)

    def __len__(self) -> int:
        return len(self._clusters)


@dataclass(slots=True)
class ProviderStatus:
    """Health of one provider — surfaced on the dashboard."""

    name: str
    available: bool = True
    last_success: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    events_fetched: int = 0

    @property
    def degraded(self) -> bool:
        return not self.available or self.consecutive_failures > 0


class NewsProvider(ABC):
    """Base class for a news source adapter."""

    name: str = "provider"
    requires_key: bool = False

    def __init__(self) -> None:
        self.status = ProviderStatus(name=self.name)

    @abstractmethod
    async def fetch(self) -> list[NewsEvent]:
        """Fetch recent events. Must never raise — return ``[]`` on failure."""

    def is_configured(self) -> bool:
        """Whether the provider has what it needs (e.g. an API key)."""
        return True

    def record_success(self, count: int) -> None:
        self.status.available = True
        self.status.last_success = now_utc()
        self.status.consecutive_failures = 0
        self.status.events_fetched += count
        self.status.last_error = None

    def record_failure(self, error: str) -> None:
        self.status.consecutive_failures += 1
        self.status.last_error = error[:300]
        # One blip is not an outage; several in a row is.
        if self.status.consecutive_failures >= 3:
            self.status.available = False
