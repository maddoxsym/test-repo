"""News engine — aggregation, point-in-time state, and effectiveness tracking.

Three responsibilities:

1. **Aggregate** providers concurrently, dedupe across them, and persist.
2. **Serve point-in-time state** — a decision at time *T* may only see events
   received before *T*. Enforced by querying on ``received_ts_utc``.
3. **Measure whether news actually helps.** The engine records how gated and
   ungated trades performed and reduces its own influence when gating is not
   paying for itself. News influence is evidence-driven, like everything else.

News never produces a trade direction on its own. It modulates risk, size, and
confidence — the "positive headline ⇒ buy" reflex is deliberately absent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config.schema import NewsConfig
from ..database.repositories import NewsRepository
from ..utils.logging import get_logger
from ..utils.numeric import clamp, safe_div
from ..utils.timeutil import iso, now_utc
from .base import HeadlineDeduplicator, NewsEvent, NewsImpact, NewsProvider, ProviderStatus
from .providers.api_providers import CryptoPanicProvider, NewsApiProvider
from .providers.macro_calendar import MacroCalendarProvider
from .providers.rss_provider import RssNewsProvider

log = get_logger(__name__)


@dataclass(slots=True)
class NewsState:
    """The news picture at one instant, as the risk engine sees it."""

    risk_level: float = 0.0            # 0 calm … 1 major event window
    blocks_entry: bool = False
    size_factor: float = 1.0
    directional_bias: float = 0.0      # a *feature*, never an instruction
    high_impact_events: int = 0
    total_events: int = 0
    degraded: bool = False
    influence: float = 1.0
    reasons: list[str] = field(default_factory=list)
    top_headlines: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.degraded:
            return "degraded"
        if self.blocks_entry:
            return "blocked"
        if self.risk_level >= 0.6:
            return "elevated"
        if self.risk_level >= 0.3:
            return "moderate"
        return "calm"

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk_level": round(self.risk_level, 3),
            "blocks_entry": self.blocks_entry,
            "size_factor": round(self.size_factor, 3),
            "directional_bias": round(self.directional_bias, 3),
            "high_impact_events": self.high_impact_events,
            "total_events": self.total_events,
            "degraded": self.degraded,
            "influence": round(self.influence, 3),
            "label": self.label,
            "reasons": self.reasons,
        }


class NewsEngine:
    """Polls providers, stores events, and derives point-in-time news state."""

    def __init__(
        self,
        config: NewsConfig,
        repository: NewsRepository,
        *,
        experiment_id: str,
    ) -> None:
        self.config = config
        self.repo = repository
        self.experiment_id = experiment_id
        self.providers: list[NewsProvider] = []
        self.last_poll: datetime | None = None
        self._influence = 1.0
        self._recent: list[NewsEvent] = []
        self._deduplicator = HeadlineDeduplicator()

        if config.enabled:
            self._build_providers()

    def _build_providers(self) -> None:
        for entry in self.config.providers:
            if not entry.enabled:
                continue
            provider: NewsProvider | None = None
            if entry.name == "rss":
                provider = RssNewsProvider(entry.feeds)
            elif entry.name == "cryptopanic":
                provider = CryptoPanicProvider()
            elif entry.name == "newsapi":
                provider = NewsApiProvider()
            elif entry.name == "macro_calendar":
                provider = MacroCalendarProvider()
            else:
                log.warning("NEWS", f"Unknown news provider {entry.name!r} — skipping")
                continue

            if provider.requires_key and not provider.is_configured():
                log.info(
                    "NEWS",
                    f"Provider '{provider.name}' skipped: no API key set (this is optional)",
                )
                continue
            if not provider.is_configured():
                log.info("NEWS", f"Provider '{provider.name}' skipped: not configured")
                continue
            self.providers.append(provider)

        if self.providers:
            log.info(
                "NEWS",
                f"News engine ready with {len(self.providers)} provider(s): "
                f"{', '.join(p.name for p in self.providers)}",
            )
        else:
            log.warning("NEWS", "NEWS DATA DEGRADED — no news providers are available")

    # --- polling ---------------------------------------------------------

    async def poll(self) -> list[NewsEvent]:
        """Fetch from every provider concurrently and persist new events."""
        if not self.providers:
            return []

        results = await asyncio.gather(
            *(self._safe_fetch(p) for p in self.providers), return_exceptions=False
        )
        self.last_poll = now_utc()

        fresh: list[NewsEvent] = []
        for events in results:
            for event in events:
                # Cross-provider dedupe by headline similarity: the same story
                # from ten outlets is one event, even though no two of them
                # phrase it identically.
                cluster, is_new_story = self._deduplicator.resolve(event.headline)
                if not is_new_story:
                    continue
                row = event.to_row()
                row["duplicate_cluster_id"] = cluster
                if self.repo.record(row):
                    fresh.append(event)

        if fresh:
            self._recent = (fresh + self._recent)[:200]
            high_impact = [e for e in fresh if e.impact is NewsImpact.HIGH]
            for event in high_impact[:3]:
                log.info("NEWS", f"HIGH impact BTC-relevant event: {event.headline[:120]}")
            log.debug("NEWS", f"Stored {len(fresh)} new events ({len(high_impact)} high impact)")

        return fresh

    async def _safe_fetch(self, provider: NewsProvider) -> list[NewsEvent]:
        """A provider failure degrades news; it never propagates to the caller."""
        try:
            return await provider.fetch()
        except Exception as exc:  # noqa: BLE001 - providers must never break the bot
            provider.record_failure(f"{type(exc).__name__}: {exc}")
            log.warning("NEWS", f"Provider '{provider.name}' failed: {type(exc).__name__}: {exc}")
            return []

    # --- state -----------------------------------------------------------

    def state_at(self, decision_time: datetime | None = None) -> NewsState:
        """News state as it was *known* at ``decision_time``.

        The point-in-time guarantee: events are filtered on when they were
        received, so a backtest or a replayed decision cannot use information
        that had not arrived yet.
        """
        if not self.config.enabled:
            return NewsState(influence=0.0)

        moment = decision_time or now_utc()
        rows = self.repo.known_before(iso(moment), limit=150)

        state = NewsState(influence=self._influence, degraded=self.is_degraded())
        if not rows:
            if state.degraded:
                state.reasons.append("no news providers available")
                state.size_factor = self.config.degraded_confidence_factor
            return state

        max_age = self.config.max_age_minutes
        fresh_rows = []
        for row in rows:
            age = _age_minutes(row.get("received_ts_utc"), moment)
            if age is not None and age <= max_age:
                fresh_rows.append((row, age))

        if not fresh_rows:
            return state

        state.total_events = len(fresh_rows)
        risk = 0.0
        weighted_sentiment = 0.0
        weight_total = 0.0

        for row, age in fresh_rows:
            impact = NewsImpact(row.get("impact", "low"))
            relevance = float(row.get("btc_relevance") or 0.0)
            confidence = float(row.get("confidence") or 0.5)
            # Decay linearly with age: a two-hour-old headline matters less
            # than a two-minute-old one.
            recency = clamp(1.0 - age / max_age, 0.0, 1.0)
            weight = impact.weight * relevance * confidence * recency

            risk = max(risk, weight)
            if impact is NewsImpact.HIGH and recency > 0.5:
                state.high_impact_events += 1
                if len(state.top_headlines) < 5:
                    state.top_headlines.append(str(row.get("headline", ""))[:160])

            weighted_sentiment += float(row.get("sentiment") or 0.0) * weight
            weight_total += weight

        state.risk_level = clamp(risk, 0.0, 1.0) * self._influence
        state.directional_bias = clamp(safe_div(weighted_sentiment, weight_total), -1.0, 1.0)

        # A very recent high-impact event pauses new entries for a short window.
        if state.high_impact_events > 0 and self.config.high_impact_pause_minutes > 0:
            newest_high = min(
                (age for row, age in fresh_rows if row.get("impact") == "high"),
                default=None,
            )
            if newest_high is not None and newest_high <= self.config.high_impact_pause_minutes:
                state.blocks_entry = self._influence > 0.25
                state.reasons.append(
                    f"high-impact event {newest_high:.0f}m ago "
                    f"(pause window {self.config.high_impact_pause_minutes}m)"
                )

        if state.risk_level >= 0.5:
            state.size_factor = self.config.high_impact_size_factor
            state.reasons.append(f"elevated news risk {state.risk_level:.2f} → reduced size")
        elif state.degraded:
            state.size_factor = self.config.degraded_confidence_factor
            state.reasons.append("news degraded → slightly reduced size")

        return state

    def is_degraded(self) -> bool:
        if not self.providers:
            return True
        return all(p.status.degraded for p in self.providers)

    def provider_status(self) -> list[ProviderStatus]:
        return [p.status for p in self.providers]

    def health_summary(self) -> dict[str, Any]:
        return {
            "degraded": self.is_degraded(),
            "influence": round(self._influence, 3),
            "last_poll": iso(self.last_poll) if self.last_poll else None,
            "providers": [
                {
                    "name": status.name,
                    "available": status.available,
                    "events": status.events_fetched,
                    "failures": status.consecutive_failures,
                    "last_error": status.last_error,
                }
                for status in self.provider_status()
            ],
        }

    def recent_events(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.repo.recent(limit)

    # --- effectiveness ---------------------------------------------------

    @property
    def influence(self) -> float:
        return self._influence

    def evaluate_effectiveness(self, trades: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Check whether news gating is actually improving results.

        Compares trades taken while news risk was elevated against those taken in
        calm conditions. If elevated-news trades do *better*, the filter is
        costing money and its influence is reduced. This is the mechanism the
        brief asks for: track experimentally whether news helps, and shrink its
        role when it does not.
        """
        if not self.config.adaptive_influence or len(trades) < 40:
            return None

        gated = [t for t in trades if str(t.get("news_state") or "calm") in {"elevated", "blocked"}]
        ungated = [t for t in trades if str(t.get("news_state") or "calm") not in {"elevated", "blocked"}]
        if len(gated) < 10 or len(ungated) < 10:
            return None

        gated_expectancy = sum(float(t.get("r_multiple") or 0.0) for t in gated) / len(gated)
        ungated_expectancy = sum(float(t.get("r_multiple") or 0.0) for t in ungated) / len(ungated)

        previous = self._influence
        # If trading through elevated news outperformed calm conditions by a
        # clear margin, the filter is hurting: reduce its influence.
        if gated_expectancy > ungated_expectancy + 0.1:
            self._influence = clamp(
                self._influence - 0.15, self.config.influence_floor, self.config.influence_ceiling
            )
            decision = "reduced"
        elif ungated_expectancy > gated_expectancy + 0.1:
            self._influence = clamp(
                self._influence + 0.1, self.config.influence_floor, self.config.influence_ceiling
            )
            decision = "increased"
        else:
            decision = "unchanged"

        record = {
            "experiment_id": self.experiment_id,
            "ts_utc": iso(now_utc()),
            "gated_trades": len(gated),
            "ungated_trades": len(ungated),
            "gated_expectancy": gated_expectancy,
            "ungated_expectancy": ungated_expectancy,
            "influence": self._influence,
            "decision": decision,
        }
        self.repo.record_effectiveness(record)

        if decision != "unchanged":
            log.info(
                "LEARNING",
                f"News influence {decision}: {previous:.2f} → {self._influence:.2f} "
                f"(elevated {gated_expectancy:+.3f}R vs calm {ungated_expectancy:+.3f}R)",
            )
        return record

    def restore_influence(self) -> None:
        """Reload the learned influence after a restart."""
        self._influence = self.repo.latest_influence(self.experiment_id, default=1.0)


def _age_minutes(timestamp: str | None, reference: datetime) -> float | None:
    if not timestamp:
        return None
    try:
        from ..utils.timeutil import parse_iso

        return max(0.0, (reference - parse_iso(timestamp)).total_seconds() / 60.0)
    except (ValueError, TypeError):
        return None
