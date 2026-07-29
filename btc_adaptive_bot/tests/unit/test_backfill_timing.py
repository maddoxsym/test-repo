"""Historical backfill is pagination, never candle scheduling.

The bug these tests pin down, from the live log:

    16:39:09 Backfilling 5 timeframe(s)
    16:40:03   1m: 1500 candles     <- next 1-minute boundary
    16:45:01   5m: 1500 candles     <- next 5-minute boundary
    17:00:02  15m: 1500 candles     <- next 15-minute boundary
    18:00:01  60m: 1500 candles     <- next 1-hour boundary

Each timeframe completed exactly one interval after it started. Nothing slept
for the timeframe; the paging loop simply had no progress guard. When the
exchange could not supply the requested number of bars, the cursor stopped
advancing — every further request returned the same single boundary candle —
and the loop could only make progress when a *new candle closed*.

So these tests assert the property that actually matters: **backfill terminates
on the exchange's data, in bounded time, no matter how little the exchange
returns.** Every test uses a stub whose recent window is deliberately shorter
than the request, which is the exact condition that used to hang.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from btcbot.exchange.models import Candle
from btcbot.market_data.historical import (
    BACKFILL_DEADLINE_SECONDS,
    MAX_BACKFILL_PAGES,
    PAGE_RETRY_BACKOFF,
    BackfillReport,
    HistoricalDataManager,
)
from btcbot.utils.errors import ApiError, BackfillError, RateLimitError, TransportError
from btcbot.utils.timeutil import interval_ms, interval_seconds, now_ms

SYMBOL = "BTC-USDT-SWAP"
TIMEFRAMES = ["1", "5", "15", "60", "240"]
BARS = 1500


class StubExchange:
    """OKX candle paging, faithfully — including the window that caused the bug.

    ``recent_window`` is how many bars the exchange will serve. Setting it below
    the requested bar count reproduces the original hang exactly: the cursor
    bottoms out and every further page returns the same one candle.
    """

    def __init__(
        self,
        timeframe: str,
        *,
        recent_window: int = 1440,
        fail_first: list[Exception] | None = None,
    ) -> None:
        self.timeframe = timeframe
        self.step = interval_ms(timeframe)
        self.recent_window = recent_window
        self.calls = 0
        self._failures = list(fail_first or [])
        self.newest_open = (now_ms() // self.step) * self.step   # still forming

    async def get_klines(
        self, inst_id, interval, *, start_ms=None, end_ms=None, limit=300
    ) -> list[Candle]:
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        after = (end_ms + 1) if end_ms is not None else None
        top = (
            self.newest_open
            if after is None
            else min(self.newest_open, ((after - 1) // self.step) * self.step)
        )
        floor = self.newest_open - self.recent_window * self.step
        out: list[Candle] = []
        for i in range(limit):
            ts = top - i * self.step
            if ts < floor:
                break
            out.append(
                Candle(
                    open_ms=ts, open=1.0, high=2.0, low=0.5, close=1.5,
                    volume=10.0, turnover=15.0, timeframe=interval,
                    confirmed=ts < self.newest_open,
                )
            )
        if start_ms is not None:
            out = [c for c in out if c.open_ms >= start_ms]
        return sorted(out, key=lambda c: c.open_ms)


class MemoryRepo:
    """Cache keyed by (symbol, timeframe, open_ms) — the dedup contract."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, int], tuple] = {}
        self.saves = 0

    def save_klines(self, symbol, timeframe, rows) -> None:
        self.saves += 1
        for row in rows:
            self.rows[(symbol, timeframe, int(row[0]))] = row

    def load_klines(self, symbol, timeframe, *, start_ms=None, end_ms=None):
        out = []
        for (sym, tf, open_ms), row in self.rows.items():
            if sym != symbol or tf != timeframe:
                continue
            if start_ms is not None and open_ms < start_ms:
                continue
            if end_ms is not None and open_ms > end_ms:
                continue
            out.append(
                {
                    "open_ms": open_ms, "open": row[1], "high": row[2], "low": row[3],
                    "close": row[4], "volume": row[5], "turnover": row[6],
                }
            )
        return sorted(out, key=lambda r: r["open_ms"])

    def kline_range(self, symbol, timeframe):
        opens = [k[2] for k in self.rows if k[0] == symbol and k[1] == timeframe]
        return (min(opens), max(opens)) if opens else (None, None)


def manager(stub, repo=None, *, request_delay_seconds: float = 0.0) -> HistoricalDataManager:
    return HistoricalDataManager(
        stub, repo or MemoryRepo(), symbol=SYMBOL,
        request_delay_seconds=request_delay_seconds,
    )


class TestBackfillNeverWaitsForTheTimeframe:
    """The reported bug, one test per timeframe from the log."""

    @pytest.mark.parametrize("timeframe", TIMEFRAMES)
    async def test_backfill_finishes_far_faster_than_one_candle(self, timeframe):
        stub = StubExchange(timeframe, recent_window=1440)   # < BARS: the hang case
        started = time.monotonic()
        report = await asyncio.wait_for(
            manager(stub).backfill_series(timeframe, bars=BARS), timeout=10
        )
        elapsed = time.monotonic() - started

        assert report.count > 0
        # The headline assertion: nowhere near one candle interval.
        assert elapsed < interval_seconds(timeframe), (
            f"{timeframe}m backfill took {elapsed:.1f}s — at least one candle interval"
        )
        assert elapsed < 5.0, "backfill should complete in seconds"

    @pytest.mark.parametrize("timeframe", TIMEFRAMES)
    async def test_the_walk_terminates_when_the_exchange_runs_out(self, timeframe):
        """The exact original hang: cursor pinned, same candle returned forever."""
        stub = StubExchange(timeframe, recent_window=200)
        report = await asyncio.wait_for(
            manager(stub).backfill_series(timeframe, bars=BARS), timeout=10
        )

        assert 0 < report.count <= 201, "everything the exchange had, closed only"
        assert not report.complete, "it must not pretend it got 1500"
        assert "no candles older than the cursor" in report.stop_reason
        assert stub.calls <= MAX_BACKFILL_PAGES

    async def test_no_page_is_ever_requested_more_than_the_cap(self):
        stub = StubExchange("1", recent_window=50)
        await manager(stub).backfill_series("1", bars=BARS)
        assert stub.calls <= MAX_BACKFILL_PAGES

    async def test_total_time_for_all_five_timeframes_is_seconds(self):
        """The whole backfill, as the live log measured it."""
        started = time.monotonic()
        total = 0
        for timeframe in TIMEFRAMES:
            report = await manager(StubExchange(timeframe)).backfill_series(
                timeframe, bars=BARS
            )
            total += report.count
        elapsed = time.monotonic() - started

        assert total > 5_000
        assert elapsed < 10.0, f"all five timeframes took {elapsed:.1f}s"


class TestNoCandleSchedulerIsInvoked:
    """Requirement: backfill must not call any next-candle waiting helper."""

    def test_the_module_references_no_candle_wait_helper(self):
        import inspect

        from btcbot.market_data import historical

        source = inspect.getsource(historical)
        for forbidden in (
            "wait_until_next_candle",
            "seconds_until_next",
            "next_candle_close",
            "candle_close_ms",
        ):
            assert forbidden not in source, f"backfill references {forbidden}"

    def test_no_sleep_is_derived_from_the_timeframe(self):
        """Every delay in the module is a fixed constant, not interval-derived."""
        import inspect

        from btcbot.market_data import historical

        source = inspect.getsource(historical)
        for line in source.splitlines():
            if "asyncio.sleep(" in line:
                assert "interval" not in line and "step" not in line, (
                    f"a sleep is derived from the timeframe: {line.strip()}"
                )

    @pytest.mark.parametrize("timeframe", TIMEFRAMES)
    async def test_sleeps_are_identical_across_timeframes(self, timeframe, monkeypatch):
        """A 4h backfill must sleep exactly as much as a 1m one."""
        slept: list[float] = []
        real_sleep = asyncio.sleep

        async def recording_sleep(seconds, *args, **kwargs):
            slept.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr(
            "btcbot.market_data.historical.asyncio.sleep", recording_sleep
        )
        stub = StubExchange(timeframe, recent_window=900)
        await manager(stub, request_delay_seconds=0.05).backfill_series(
            timeframe, bars=BARS
        )

        assert slept, "the inter-page courtesy delay should still happen"
        # No individual sleep, and no total, approaches a candle interval.
        assert max(slept) == pytest.approx(0.05)
        assert sum(slept) < interval_seconds(timeframe)

    def test_the_deadline_backstop_is_below_the_smallest_candle(self):
        """Even the last-resort budget cannot be mistaken for a candle wait."""
        assert interval_seconds("1") > BACKFILL_DEADLINE_SECONDS


class TestRateLimitBackoff:
    async def test_a_rate_limit_is_retried_and_succeeds(self):
        stub = StubExchange("15", fail_first=[RateLimitError("429")])
        report = await manager(stub).backfill_series("15", bars=300)

        assert report.count > 0
        assert stub.calls > 1, "the rate-limited page was retried"

    async def test_transport_and_api_errors_are_retried(self):
        stub = StubExchange(
            "15",
            fail_first=[
                TransportError("connection reset"),
                ApiError(50011, "rate limited", "/api/v5/market/candles"),
            ],
        )
        report = await manager(stub).backfill_series("15", bars=300)
        assert report.count > 0

    async def test_persistent_failure_raises_rather_than_returning_short(self):
        """Requirement 14: report the exact error, never silently continue."""
        failures = [RateLimitError("429")] * (len(PAGE_RETRY_BACKOFF) + 5)
        stub = StubExchange("15", fail_first=failures)

        with pytest.raises(BackfillError) as exc:
            await manager(stub).backfill_series("15", bars=300)

        assert "429" in str(exc.value)
        assert SYMBOL in str(exc.value)

    async def test_the_retry_schedule_is_bounded_and_short(self):
        assert len(PAGE_RETRY_BACKOFF) <= 6
        assert sum(PAGE_RETRY_BACKOFF) < interval_seconds("1")

    async def test_retries_do_not_scale_with_the_timeframe(self):
        """A 4h page retries on the same schedule as a 1m page."""
        counts = {}
        for timeframe in ("1", "240"):
            stub = StubExchange(timeframe, fail_first=[RateLimitError("429")] * 2)
            await manager(stub).backfill_series(timeframe, bars=300)
            counts[timeframe] = stub.calls
        assert counts["1"] == counts["240"]


class TestCacheReuseOnRestart:
    async def test_a_second_run_reuses_stored_candles(self):
        repo = MemoryRepo()
        first = await manager(StubExchange("15"), repo).backfill_series("15", bars=600)
        assert first.fetched > 0
        assert first.from_cache == 0

        stub = StubExchange("15")
        second = await manager(stub, repo).backfill_series("15", bars=600)

        assert second.from_cache >= 600, "stored candles were not reused"
        assert second.fetched == 0, "a restart re-downloaded everything"
        assert stub.calls == 0, "no API call was needed at all"
        assert second.count == first.count

    async def test_only_missing_candles_are_fetched_after_a_gap(self):
        """Restart after downtime: fetch the tail, not all 600 bars again."""
        repo = MemoryRepo()
        first = await manager(StubExchange("15"), repo).backfill_series("15", bars=600)
        assert first.fetched >= 600

        # Simulate downtime: the newest 20 candles were never stored.
        newest = sorted(k[2] for k in repo.rows)[-20:]
        for open_ms in newest:
            del repo.rows[(SYMBOL, "15", open_ms)]

        stub = StubExchange("15")
        report = await manager(stub, repo).backfill_series("15", bars=600)

        assert report.count >= 600, "the gap was not filled"
        # One page covers a 20-candle gap; re-downloading all 600 would take 2+.
        assert report.pages <= 2, f"{report.pages} pages for a 20-candle gap"
        assert report.fetched < 600, (
            f"re-downloaded {report.fetched} candles for a 20-candle gap"
        )

    async def test_the_cache_is_written_after_a_download(self):
        repo = MemoryRepo()
        await manager(StubExchange("60"), repo).backfill_series("60", bars=400)
        assert repo.saves >= 1
        assert len(repo.load_klines(SYMBOL, "60")) >= 400

    async def test_cached_series_exposes_what_is_stored(self):
        repo = MemoryRepo()
        await manager(StubExchange("60"), repo).backfill_series("60", bars=400)
        assert len(manager(StubExchange("60"), repo).cached_series("60", bars=400)) >= 400


class TestDeduplicationAndCompleteness:
    async def test_overlapping_pages_do_not_duplicate_candles(self):
        """OKX pages overlap by one candle; opens must collapse."""
        report = await manager(StubExchange("5")).backfill_series("5", bars=BARS)
        opens = [c.open_ms for c in report.candles]
        assert len(opens) == len(set(opens)), "duplicate open timestamps survived"

    async def test_candles_are_ordered_oldest_first(self):
        report = await manager(StubExchange("5")).backfill_series("5", bars=BARS)
        opens = [c.open_ms for c in report.candles]
        assert opens == sorted(opens)

    async def test_the_series_is_contiguous(self):
        report = await manager(StubExchange("5")).backfill_series("5", bars=500)
        step = interval_ms("5")
        opens = [c.open_ms for c in report.candles]
        assert all(b - a == step for a, b in zip(opens, opens[1:], strict=False))

    async def test_the_forming_candle_is_excluded(self):
        """Requirement 10: only completed candles reach strategies."""
        stub = StubExchange("15")
        report = await manager(stub).backfill_series("15", bars=BARS)

        assert all(c.confirmed for c in report.candles)
        assert all(c.is_closed() for c in report.candles)
        assert stub.newest_open not in [c.open_ms for c in report.candles], (
            "the still-forming candle was included"
        )

    async def test_dedup_is_per_symbol_and_timeframe(self):
        """The same open time in two timeframes is two different candles."""
        repo = MemoryRepo()
        await manager(StubExchange("5"), repo).backfill_series("5", bars=200)
        await manager(StubExchange("15"), repo).backfill_series("15", bars=200)

        assert len(repo.load_klines(SYMBOL, "5")) >= 200
        assert len(repo.load_klines(SYMBOL, "15")) >= 200
        keys = {(k[0], k[1]) for k in repo.rows}
        assert keys == {(SYMBOL, "5"), (SYMBOL, "15")}

    async def test_a_replayed_page_adds_nothing(self):
        """Idempotence: running backfill twice yields the same series."""
        repo = MemoryRepo()
        first = await manager(StubExchange("15"), repo).backfill_series("15", bars=400)
        second = await manager(StubExchange("15"), repo).backfill_series("15", bars=400)
        assert [c.open_ms for c in first.candles] == [c.open_ms for c in second.candles]


class TestReportShape:
    async def test_the_report_carries_progress_detail(self):
        report = await manager(StubExchange("15")).backfill_series("15", bars=600)

        assert isinstance(report, BackfillReport)
        assert report.timeframe == "15"
        assert report.requested == 600
        assert report.complete
        assert report.pages >= 1
        assert report.elapsed_seconds >= 0.0
        assert "15m" in report.describe()

    async def test_progress_is_logged_per_page_and_at_completion(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        await manager(StubExchange("15")).backfill_series("15", bars=600)

        text = caplog.text
        assert "15m page 1/" in text, "no per-page progress line"
        assert "15m complete" in text, "no completion line"
        assert "candles" in text


class TestOtherPathsAlsoTerminate:
    async def test_ensure_history_does_not_spin_on_a_short_window(self):
        """`_download_range` carries the same guard."""
        stub = StubExchange("60", recent_window=100)
        report = await asyncio.wait_for(
            manager(stub).ensure_history("60", days=400, repair_gaps=False), timeout=10
        )
        assert report.cached >= 0
        assert stub.calls <= MAX_BACKFILL_PAGES * 4
