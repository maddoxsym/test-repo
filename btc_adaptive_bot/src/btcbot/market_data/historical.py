"""Historical data acquisition: paged download, gap repair, local cache.

Two weeks of live evidence is not enough to judge a strategy, so Layer 1 needs a
long BTC history spanning genuinely different regimes. This module fetches it in
pages (OKX caps recent-candle responses at 300 and deep history at 100 — the
client handles the endpoint switch), stores it in SQLite, and repairs gaps
rather than silently backtesting over holes.

Backfill is *historical pagination*. It is not, and must never become, live
candle scheduling
------------------------------------------------------------------------------

These are two different jobs and they were once entangled here, with a
spectacular symptom: backfilling 1500 bars took **one full candle interval per
timeframe** — 1m finished at the next minute boundary, 5m at the next
five-minute boundary, 1h an hour later.

Nothing slept for the timeframe. The cause was subtler and worse: the paging
loop had no progress guard. Its only exits were "collected enough", "empty
page" and "reached start_ms". When the exchange could not supply the requested
number of bars — its recent-candles window is shorter than 1500 for small
timeframes — the cursor stopped moving: each further request returned the same
single boundary candle, ``oldest == cursor_end``, no exit condition fired, and
the loop span at the inter-request delay. It could then only make progress when
a *new candle closed* and shifted the window forward, which is exactly why
completion landed on candle boundaries.

So the paging loop now guarantees termination on the exchange's data, never on
the clock:

* **no-progress guard** — a page that does not reach further back than the
  cursor ends the walk immediately (this is the bug above);
* **no-new-data guard** — a page containing nothing we do not already hold ends
  the walk;
* **hard page cap** — bounded by the bars requested, never unbounded;
* **wall-clock budget** — a final backstop measured in seconds, deliberately
  smaller than the smallest supported timeframe, so "waited for a candle" is
  not a reachable state;
* **bounded retry/backoff** — applied only after an actual API error or rate
  limit, never speculatively.

Whatever the exchange returns, backfill finishes in seconds. Waiting for a
candle to close is the live stream's job, and it happens in ``ws.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic

from ..database.repositories import MarketRepository
from ..exchange.models import Candle
from ..exchange.rest import OkxDemoClient
from ..utils.errors import ApiError, BackfillError, RateLimitError, TransportError
from ..utils.logging import get_logger
from ..utils.timeutil import interval_ms, now_ms
from .candles import build_closed_only

log = get_logger(__name__)

PAGE_LIMIT = 300  # documented maximum for /api/v5/market/candles

# Retry delays after a *real* failure (rate limit, transport error). Bounded and
# short: the point is to ride out a 429, not to wait for anything.
PAGE_RETRY_BACKOFF: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0)

# Wall-clock backstop for one timeframe's backfill. Deliberately below 60s —
# the smallest supported candle — so no code path can ever be mistaken for
# "waited for the timeframe". Reaching it is a bug, and it is logged as one.
BACKFILL_DEADLINE_SECONDS = 45.0

# Absolute ceiling on pages per timeframe, on top of the computed estimate.
MAX_BACKFILL_PAGES = 40


@dataclass(slots=True)
class BackfillReport:
    """What one timeframe's backfill actually did.

    Returned rather than logged-and-discarded so the orchestrator can decide
    whether market data is good enough to trade on, and say why not when it
    is not.
    """

    symbol: str
    timeframe: str
    requested: int
    candles: list[Candle] = field(default_factory=list)
    from_cache: int = 0
    fetched: int = 0
    pages: int = 0
    elapsed_seconds: float = 0.0
    stop_reason: str = ""

    @property
    def count(self) -> int:
        return len(self.candles)

    @property
    def complete(self) -> bool:
        return self.count >= self.requested

    def describe(self) -> str:
        return (
            f"{self.timeframe}m — {self.count} candles "
            f"({self.from_cache} cached, {self.fetched} fetched, {self.pages} page(s)) "
            f"in {self.elapsed_seconds:.1f}s"
        )


@dataclass(slots=True)
class DownloadReport:
    symbol: str
    timeframe: str
    requested_bars: int
    downloaded: int
    cached: int
    gaps_found: int
    gaps_repaired: int
    start_ms: int
    end_ms: int

    def describe(self) -> str:
        return (
            f"{self.symbol} {self.timeframe}: {self.cached} bars cached "
            f"(+{self.downloaded} new, {self.gaps_repaired}/{self.gaps_found} gaps repaired)"
        )


class HistoricalDataManager:
    """Downloads and caches historical klines."""

    def __init__(
        self,
        client: OkxDemoClient,
        repository: MarketRepository,
        *,
        symbol: str,
        request_delay_seconds: float = 0.12,
    ) -> None:
        self._client = client
        self._repo = repository
        self.symbol = symbol
        self._delay = request_delay_seconds

    async def ensure_history(
        self, timeframe: str, *, days: int, repair_gaps: bool = True
    ) -> DownloadReport:
        """Ensure ``days`` of history exist locally, downloading what is missing."""
        step = interval_ms(timeframe)
        end_ms = now_ms()
        start_ms = end_ms - days * 86_400_000
        requested_bars = max(1, (end_ms - start_ms) // step)

        cached_lo, cached_hi = self._repo.kline_range(self.symbol, timeframe)
        downloaded = 0

        if cached_lo is None or cached_hi is None:
            downloaded += await self._download_range(timeframe, start_ms, end_ms)
        else:
            if cached_lo > start_ms + step:
                downloaded += await self._download_range(timeframe, start_ms, cached_lo)
            if cached_hi < end_ms - step:
                downloaded += await self._download_range(timeframe, cached_hi, end_ms)

        gaps_found = gaps_repaired = 0
        if repair_gaps:
            gaps = self.find_gaps(timeframe, start_ms=start_ms, end_ms=end_ms)
            gaps_found = len(gaps)
            for gap_start, gap_end in gaps[:50]:  # bound the repair work per pass
                repaired = await self._download_range(timeframe, gap_start, gap_end + step)
                if repaired:
                    gaps_repaired += 1

        cached = len(self._repo.load_klines(self.symbol, timeframe, start_ms=start_ms, end_ms=end_ms))
        report = DownloadReport(
            symbol=self.symbol,
            timeframe=timeframe,
            requested_bars=int(requested_bars),
            downloaded=downloaded,
            cached=cached,
            gaps_found=gaps_found,
            gaps_repaired=gaps_repaired,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        log.info("DATA", report.describe())
        return report

    async def _download_range(self, timeframe: str, start_ms: int, end_ms: int) -> int:
        """Page backwards through a range and persist every closed candle."""
        step = interval_ms(timeframe)
        total = 0
        cursor_end = end_ms
        guard = 0
        max_pages = 2000  # hard stop against a pathological loop

        while cursor_end > start_ms and guard < max_pages:
            guard += 1
            page_start = max(start_ms, cursor_end - PAGE_LIMIT * step)
            try:
                candles = await self._client.get_klines(
                    self.symbol,
                    timeframe,
                    start_ms=page_start,
                    end_ms=cursor_end,
                    limit=PAGE_LIMIT,
                )
            except (ApiError, TransportError) as exc:
                log.warning("DATA", f"History page failed ({timeframe} @ {page_start}): {exc}")
                break

            # Drop the still-forming candle: it would poison every backtest.
            closed = build_closed_only(candles)
            if not closed:
                break

            self._repo.save_klines(
                self.symbol,
                timeframe,
                [
                    (c.open_ms, c.open, c.high, c.low, c.close, c.volume, c.turnover)
                    for c in closed
                ],
            )
            total += len(closed)

            oldest = min(c.open_ms for c in closed)
            # Same no-progress guard as backfill_series: a page that does not
            # reach further back than the cursor means there is no more history.
            if oldest >= cursor_end or oldest <= page_start or len(candles) < 2:
                break
            cursor_end = oldest
            await asyncio.sleep(self._delay)

        return total

    def find_gaps(
        self, timeframe: str, *, start_ms: int | None = None, end_ms: int | None = None
    ) -> list[tuple[int, int]]:
        """Missing ``(start_ms, end_ms)`` ranges in the cached history."""
        rows = self._repo.load_klines(self.symbol, timeframe, start_ms=start_ms, end_ms=end_ms)
        if len(rows) < 2:
            return []
        step = interval_ms(timeframe)
        gaps: list[tuple[int, int]] = []
        for previous, current in zip(rows, rows[1:], strict=False):
            expected = previous["open_ms"] + step
            if current["open_ms"] > expected:
                gaps.append((expected, current["open_ms"] - step))
        return gaps

    def load(
        self, timeframe: str, *, start_ms: int | None = None, end_ms: int | None = None
    ) -> list[Candle]:
        """Load cached candles as :class:`Candle` objects, oldest first.

        Cached rows are always closed candles (unclosed ones are never written),
        so they are marked ``confirmed=True``.
        """
        rows = self._repo.load_klines(self.symbol, timeframe, start_ms=start_ms, end_ms=end_ms)
        return [
            Candle(
                open_ms=int(row["open_ms"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                turnover=float(row["turnover"]),
                timeframe=timeframe,
                confirmed=True,
            )
            for row in rows
        ]

    async def _fetch_page(
        self, timeframe: str, *, start_ms: int, end_ms: int, limit: int
    ) -> list[Candle]:
        """One page, with bounded backoff after an actual failure.

        The backoff schedule is fixed, short and error-triggered. Nothing here
        is derived from the timeframe: a 4h backfill retries on exactly the
        same schedule as a 1m one.
        """
        last_error: Exception | None = None
        for attempt, delay in enumerate((0.0, *PAGE_RETRY_BACKOFF), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self._client.get_klines(
                    self.symbol, timeframe, start_ms=start_ms, end_ms=end_ms, limit=limit
                )
            except RateLimitError as exc:
                last_error = exc
                log.debug(
                    "BACKFILL", f"{timeframe} page rate-limited (attempt {attempt}) — backing off"
                )
            except (ApiError, TransportError) as exc:
                last_error = exc
                log.debug("BACKFILL", f"{timeframe} page failed (attempt {attempt}): {exc}")
        raise BackfillError(
            f"{self.symbol} {timeframe}: page request failed after "
            f"{len(PAGE_RETRY_BACKOFF) + 1} attempts — {last_error}"
        ) from last_error

    def cached_series(self, timeframe: str, *, bars: int) -> list[Candle]:
        """The closed candles already stored for the most recent ``bars`` window."""
        step = interval_ms(timeframe)
        end_ms = now_ms()
        return self.load(timeframe, start_ms=end_ms - (bars + 5) * step, end_ms=end_ms)

    async def backfill_series(
        self,
        timeframe: str,
        *,
        bars: int,
        deadline_seconds: float = BACKFILL_DEADLINE_SECONDS,
    ) -> BackfillReport:
        """Fetch the most recent ``bars`` closed candles for the live store.

        Pure historical pagination: it asks the exchange for pages as fast as
        the rate limit allows and stops as soon as the exchange stops yielding
        candles it does not already have. It never waits for a candle to close,
        and its termination does not depend on the clock — see the module
        docstring for the bug that made it look as though it did.

        Restart behaviour: whatever is already cached for the window is loaded
        first, and paging begins from the newest cached candle, so a restart
        fetches only what has happened since rather than all ``bars`` again.
        """
        started = monotonic()
        step = interval_ms(timeframe)
        end_ms = now_ms()
        start_ms = end_ms - (bars + 5) * step

        # --- reuse the cache -------------------------------------------
        cached = {c.open_ms: c for c in self.load(timeframe, start_ms=start_ms, end_ms=end_ms)}
        collected: dict[int, Candle] = dict(cached)
        newest_cached = max(collected) if collected else None
        last_closed_open = self._last_closed_open(timeframe, now=end_ms)

        if len(collected) >= bars and newest_cached is not None and newest_cached >= last_closed_open:
            elapsed = monotonic() - started
            log.info(
                "BACKFILL",
                f"{timeframe}m complete — {len(collected)} candles from cache, "
                f"0 fetched in {elapsed:.1f}s",
            )
            return BackfillReport(
                symbol=self.symbol, timeframe=timeframe, requested=bars,
                candles=self._tail(collected, bars), from_cache=len(cached),
                fetched=0, pages=0, elapsed_seconds=elapsed,
            )

        # Only the missing tail is needed when the cache already covers the span.
        needed_from = newest_cached if (newest_cached and len(cached) >= bars) else start_ms
        estimated_pages = max(1, -(-(bars - len(cached)) // PAGE_LIMIT)) if len(cached) < bars else 1
        max_pages = min(MAX_BACKFILL_PAGES, max(2, estimated_pages + 2))

        cursor_end = end_ms
        pages = 0
        fetched = 0
        stop_reason = "target reached"

        while len(collected) < bars or newest_cached is None or cursor_end > needed_from:
            if pages >= max_pages:
                stop_reason = f"page cap ({max_pages}) reached"
                break
            if monotonic() - started > deadline_seconds:
                # A backstop, not a schedule. Reaching it means something is
                # wrong with pagination, and it is reported as such.
                stop_reason = f"deadline ({deadline_seconds:.0f}s) reached"
                log.warning(
                    "BACKFILL",
                    f"{timeframe}m hit the {deadline_seconds:.0f}s budget after {pages} page(s) — "
                    "returning what was fetched",
                )
                break

            pages += 1
            page = await self._fetch_page(
                timeframe,
                start_ms=max(start_ms, needed_from),
                end_ms=cursor_end,
                limit=PAGE_LIMIT,
            )
            closed = build_closed_only(page)
            if not closed:
                stop_reason = "exchange returned no further candles"
                log.info("BACKFILL", f"{timeframe}m page {pages}/{max_pages} — 0 candles (end of data)")
                break

            new = [c for c in closed if c.open_ms not in collected]
            for candle in closed:
                collected[candle.open_ms] = candle
            fetched += len(new)
            log.info(
                "BACKFILL",
                f"{timeframe}m page {pages}/{max_pages} — {len(new)} candles "
                f"({len(collected)}/{bars} total)",
            )

            oldest = min(c.open_ms for c in closed)
            # THE guard. A page that does not reach further back than the cursor
            # means the exchange has no more history for this window; without
            # this the loop re-requested the same boundary candle forever and
            # could only advance when a new candle closed.
            if oldest >= cursor_end:
                stop_reason = "exchange has no candles older than the cursor"
                break
            if not new:
                stop_reason = "page contained nothing new"
                break
            if oldest <= start_ms:
                stop_reason = "reached the start of the requested window"
                break
            if len(collected) >= bars:
                stop_reason = "target reached"
                break

            cursor_end = oldest
            await asyncio.sleep(self._delay)   # rate-limit courtesy, not a candle wait

        result = self._tail(collected, bars)
        if result:
            self._repo.save_klines(
                self.symbol,
                timeframe,
                [(c.open_ms, c.open, c.high, c.low, c.close, c.volume, c.turnover) for c in result],
            )
        elapsed = monotonic() - started
        log.info(
            "BACKFILL",
            f"{timeframe}m complete — {len(result)} candles "
            f"({len(cached)} cached, {fetched} fetched) in {elapsed:.1f}s [{stop_reason}]",
        )
        return BackfillReport(
            symbol=self.symbol, timeframe=timeframe, requested=bars, candles=result,
            from_cache=len(cached), fetched=fetched, pages=pages,
            elapsed_seconds=elapsed, stop_reason=stop_reason,
        )

    @staticmethod
    def _tail(collected: dict[int, Candle], bars: int) -> list[Candle]:
        """Newest ``bars`` candles, ordered oldest-first and de-duplicated.

        ``collected`` is keyed by ``open_ms`` for one symbol/timeframe, so
        duplicate opens collapse by construction.
        """
        return sorted(collected.values(), key=lambda c: c.open_ms)[-bars:]

    @staticmethod
    def _last_closed_open(timeframe: str, *, now: int) -> int:
        """Open time of the most recently *closed* candle."""
        step = interval_ms(timeframe)
        forming_open = (now // step) * step
        return forming_open - step

    async def recover_missing(self, timeframe: str, since_ms: int) -> list[Candle]:
        """Re-fetch candles after an outage so the live series has no hole."""
        try:
            candles = await self._client.get_klines(
                self.symbol,
                timeframe,
                start_ms=since_ms,
                end_ms=now_ms(),
                limit=PAGE_LIMIT,
            )
        except (ApiError, TransportError) as exc:
            log.warning("DATA", f"Missing-candle recovery failed for {timeframe}: {exc}")
            return []
        closed = build_closed_only(candles)
        if closed:
            self._repo.save_klines(
                self.symbol,
                timeframe,
                [(c.open_ms, c.open, c.high, c.low, c.close, c.volume, c.turnover) for c in closed],
            )
            log.info("RECOVERY", f"Recovered {len(closed)} missing {timeframe} candles")
        return closed
