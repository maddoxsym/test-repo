"""Historical data acquisition: paged download, gap repair, local cache.

Two weeks of live evidence is not enough to judge a strategy, so Layer 1 needs a
long BTC history spanning genuinely different regimes. This module fetches it in
pages (OKX caps recent-candle responses at 300 and deep history at 100 — the
client handles the endpoint switch), stores it in SQLite, and repairs gaps
rather than silently backtesting over holes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..database.repositories import MarketRepository
from ..exchange.models import Candle
from ..exchange.rest import OkxDemoClient
from ..utils.errors import ApiError, TransportError
from ..utils.logging import get_logger
from ..utils.timeutil import interval_ms, now_ms
from .candles import build_closed_only

log = get_logger(__name__)

PAGE_LIMIT = 300  # documented maximum for /api/v5/market/candles


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
            if oldest <= page_start or len(candles) < 2:
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

    async def backfill_series(self, timeframe: str, *, bars: int) -> list[Candle]:
        """Fetch the most recent ``bars`` closed candles for the live store."""
        step = interval_ms(timeframe)
        end_ms = now_ms()
        start_ms = end_ms - (bars + 5) * step
        collected: dict[int, Candle] = {}
        cursor_end = end_ms

        while len(collected) < bars:
            try:
                page = await self._client.get_klines(
                    self.symbol,
                    timeframe,
                    start_ms=start_ms,
                    end_ms=cursor_end,
                    limit=min(PAGE_LIMIT, bars + 10),
                )
            except (ApiError, TransportError) as exc:
                log.warning("DATA", f"Backfill failed for {timeframe}: {exc}")
                break
            closed = build_closed_only(page)
            if not closed:
                break
            for candle in closed:
                collected[candle.open_ms] = candle
            oldest = min(c.open_ms for c in closed)
            if oldest <= start_ms:
                break
            cursor_end = oldest
            await asyncio.sleep(self._delay)

        result = sorted(collected.values(), key=lambda c: c.open_ms)[-bars:]
        if result:
            self._repo.save_klines(
                self.symbol,
                timeframe,
                [(c.open_ms, c.open, c.high, c.low, c.close, c.volume, c.turnover) for c in result],
            )
        return result

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
