"""Candle series with a hard look-ahead guard.

This is the most safety-critical data structure in the research stack. Every
backtest result and every live signal depends on strategies being unable to see
a candle that had not closed at the decision instant.

Two mechanisms enforce that:

1. :meth:`CandleSeries.closed` never returns an unclosed candle.
2. :class:`ReplayCursor` caps visibility during backtests. Reading past the
   cursor raises :class:`~btcbot.utils.errors.LookAheadError` rather than
   returning data — a bug must be loud, not subtly profitable.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence

import numpy as np

from ..exchange.models import Candle
from ..utils.errors import LookAheadError
from ..utils.timeutil import interval_ms, is_candle_closed


class CandleSeries:
    """Ordered, de-duplicated candles for one symbol/timeframe."""

    __slots__ = ("timeframe", "symbol", "_candles", "_open_times", "_max_size")

    def __init__(self, symbol: str, timeframe: str, *, max_size: int = 1500) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self._max_size = max_size
        self._candles: list[Candle] = []
        self._open_times: list[int] = []

    def __len__(self) -> int:
        return len(self._candles)

    def __bool__(self) -> bool:
        return bool(self._candles)

    # --- mutation --------------------------------------------------------

    def upsert(self, candle: Candle) -> bool:
        """Insert or update a candle. Returns True when a *new* bar was added.

        Updating the in-progress bar in place is what lets the live engine track
        the forming candle without ever exposing it as closed.
        """
        index = bisect_left(self._open_times, candle.open_ms)
        if index < len(self._open_times) and self._open_times[index] == candle.open_ms:
            self._candles[index] = candle
            return False
        self._open_times.insert(index, candle.open_ms)
        self._candles.insert(index, candle)
        self._trim()
        return True

    def extend(self, candles: Sequence[Candle]) -> int:
        added = 0
        for candle in candles:
            if self.upsert(candle):
                added += 1
        return added

    def _trim(self) -> None:
        overflow = len(self._candles) - self._max_size
        if overflow > 0:
            del self._candles[:overflow]
            del self._open_times[:overflow]

    # --- safe reads ------------------------------------------------------

    def closed(self, *, now_ms_value: int | None = None, limit: int | None = None) -> list[Candle]:
        """Every candle known to have closed. **The only read strategies use.**"""
        result = [c for c in self._candles if c.is_closed(now_ms_value=now_ms_value)]
        if limit is not None:
            return result[-limit:]
        return result

    def last_closed(self, *, now_ms_value: int | None = None) -> Candle | None:
        for candle in reversed(self._candles):
            if candle.is_closed(now_ms_value=now_ms_value):
                return candle
        return None

    def forming(self, *, now_ms_value: int | None = None) -> Candle | None:
        """The in-progress candle, if any.

        Exposed for display and for tracking intrabar excursions of an *open*
        position. Strategy entry logic must not use it.
        """
        if not self._candles:
            return None
        last = self._candles[-1]
        return None if last.is_closed(now_ms_value=now_ms_value) else last

    def all_candles(self) -> list[Candle]:
        """Every stored candle including any unclosed one — for backfill logic only."""
        return list(self._candles)

    def get(self, open_ms: int) -> Candle | None:
        index = bisect_left(self._open_times, open_ms)
        if index < len(self._open_times) and self._open_times[index] == open_ms:
            return self._candles[index]
        return None

    def slice_until(self, end_ms_exclusive: int) -> list[Candle]:
        """All candles that had *closed* strictly before ``end_ms_exclusive``."""
        step = interval_ms(self.timeframe)
        cutoff = end_ms_exclusive - step
        index = bisect_right(self._open_times, cutoff)
        return self._candles[:index]

    # --- gap handling ----------------------------------------------------

    def missing_ranges(self, *, now_ms_value: int | None = None) -> list[tuple[int, int]]:
        """Contiguous ``(start_ms, end_ms)`` gaps in the closed history.

        Crypto trades continuously, so a gap means we missed data, not that the
        market was shut. The historical manager repairs these from REST.
        """
        closed = self.closed(now_ms_value=now_ms_value)
        if len(closed) < 2:
            return []
        step = interval_ms(self.timeframe)
        gaps: list[tuple[int, int]] = []
        for previous, current in zip(closed, closed[1:], strict=False):
            expected = previous.open_ms + step
            if current.open_ms > expected:
                gaps.append((expected, current.open_ms - step))
        return gaps

    def coverage_ratio(self, *, now_ms_value: int | None = None) -> float:
        """Fraction of expected bars actually present across the stored span."""
        closed = self.closed(now_ms_value=now_ms_value)
        if len(closed) < 2:
            return 1.0
        step = interval_ms(self.timeframe)
        span = closed[-1].open_ms - closed[0].open_ms
        expected = span // step + 1
        return len(closed) / expected if expected > 0 else 1.0

    # --- numpy views for indicators --------------------------------------

    def arrays(self, *, now_ms_value: int | None = None, limit: int | None = None) -> dict[str, np.ndarray]:
        """OHLCV as numpy arrays built **only** from closed candles."""
        closed = self.closed(now_ms_value=now_ms_value, limit=limit)
        if not closed:
            empty = np.array([], dtype=float)
            return {
                "open_ms": np.array([], dtype=np.int64),
                "open": empty, "high": empty, "low": empty,
                "close": empty, "volume": empty, "turnover": empty,
            }
        return {
            "open_ms": np.fromiter((c.open_ms for c in closed), dtype=np.int64, count=len(closed)),
            "open": np.fromiter((c.open for c in closed), dtype=float, count=len(closed)),
            "high": np.fromiter((c.high for c in closed), dtype=float, count=len(closed)),
            "low": np.fromiter((c.low for c in closed), dtype=float, count=len(closed)),
            "close": np.fromiter((c.close for c in closed), dtype=float, count=len(closed)),
            "volume": np.fromiter((c.volume for c in closed), dtype=float, count=len(closed)),
            "turnover": np.fromiter((c.turnover for c in closed), dtype=float, count=len(closed)),
        }


class ReplayCursor:
    """Bounded view over historical candles for leak-free backtesting.

    The backtester advances the cursor one bar at a time. A strategy asking for
    "the last N closed candles" gets exactly what it would have had at that
    moment in history — and any attempt to index beyond the cursor raises.
    """

    __slots__ = ("_candles", "_index", "timeframe", "symbol")

    def __init__(self, symbol: str, timeframe: str, candles: Sequence[Candle]) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self._candles = list(candles)
        self._index = -1

    def __len__(self) -> int:
        return len(self._candles)

    @property
    def index(self) -> int:
        """Index of the most recently *closed* bar (-1 before the first)."""
        return self._index

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self._candles) - 1

    def advance(self) -> Candle | None:
        """Close one more bar and return it."""
        if self.exhausted:
            return None
        self._index += 1
        return self._candles[self._index]

    def current(self) -> Candle | None:
        """The bar that just closed — the newest thing a strategy may see."""
        if self._index < 0:
            return None
        return self._candles[self._index]

    def next_bar(self) -> Candle | None:
        """The bar *after* the cursor.

        Reserved for the execution simulator, which must fill an order at the
        next bar's prices — a strategy is structurally unable to call this
        because it is only handed a :class:`ReplayView`.
        """
        if self._index + 1 >= len(self._candles):
            return None
        return self._candles[self._index + 1]

    def history(self, limit: int | None = None) -> list[Candle]:
        """Closed bars up to and including the cursor."""
        if self._index < 0:
            return []
        window = self._candles[: self._index + 1]
        return window[-limit:] if limit is not None else window

    def peek(self, offset: int) -> Candle:
        """Look back ``offset`` bars. Positive/forward offsets are refused."""
        if offset < 0:
            raise LookAheadError(
                f"look-ahead attempt: requested offset {offset} beyond the replay cursor "
                f"at index {self._index}"
            )
        target = self._index - offset
        if target < 0:
            raise IndexError(f"only {self._index + 1} bars available, asked to look back {offset}")
        return self._candles[target]


class ReplayView:
    """Read-only façade handed to strategies during a backtest.

    Deliberately narrow: it exposes history and nothing else, so a strategy
    cannot reach the future even by accident.
    """

    __slots__ = ("_cursor",)

    def __init__(self, cursor: ReplayCursor) -> None:
        self._cursor = cursor

    @property
    def symbol(self) -> str:
        return self._cursor.symbol

    @property
    def timeframe(self) -> str:
        return self._cursor.timeframe

    def __len__(self) -> int:
        return self._cursor.index + 1

    def history(self, limit: int | None = None) -> list[Candle]:
        return self._cursor.history(limit)

    def current(self) -> Candle | None:
        return self._cursor.current()

    def peek(self, offset: int) -> Candle:
        return self._cursor.peek(offset)

    def arrays(self, limit: int | None = None) -> dict[str, np.ndarray]:
        candles = self._cursor.history(limit)
        if not candles:
            empty = np.array([], dtype=float)
            return {
                "open_ms": np.array([], dtype=np.int64),
                "open": empty, "high": empty, "low": empty,
                "close": empty, "volume": empty, "turnover": empty,
            }
        return {
            "open_ms": np.fromiter((c.open_ms for c in candles), dtype=np.int64, count=len(candles)),
            "open": np.fromiter((c.open for c in candles), dtype=float, count=len(candles)),
            "high": np.fromiter((c.high for c in candles), dtype=float, count=len(candles)),
            "low": np.fromiter((c.low for c in candles), dtype=float, count=len(candles)),
            "close": np.fromiter((c.close for c in candles), dtype=float, count=len(candles)),
            "volume": np.fromiter((c.volume for c in candles), dtype=float, count=len(candles)),
            "turnover": np.fromiter((c.turnover for c in candles), dtype=float, count=len(candles)),
        }


def assert_closed(candle: Candle, *, now_ms_value: int | None = None) -> Candle:
    """Guard helper: raise unless ``candle`` has genuinely closed."""
    if not candle.is_closed(now_ms_value=now_ms_value):
        raise LookAheadError(
            f"attempted to use an unclosed {candle.timeframe} candle opened at {candle.open_ms}"
        )
    return candle


def candles_are_closed(candles: Sequence[Candle], *, now_ms_value: int | None = None) -> bool:
    return all(c.is_closed(now_ms_value=now_ms_value) for c in candles)


def build_closed_only(candles: Sequence[Candle], *, now_ms_value: int | None = None) -> list[Candle]:
    """Filter to closed candles.

    Applied to every REST candle response, because OKX's newest element may be
    the still-forming candle.
    """
    return [c for c in candles if c.is_closed(now_ms_value=now_ms_value)]


def is_candle_complete(open_ms: int, timeframe: str, *, now: int | None = None) -> bool:
    """Public re-export used by tests and the data-health checks."""
    return is_candle_closed(open_ms, timeframe, now=now)
