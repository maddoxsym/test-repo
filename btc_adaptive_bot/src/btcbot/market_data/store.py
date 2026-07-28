"""Live market-data store: candles, ticker, orderbook, trade flow, and health.

One instance holds the current view of the market. It is written by the
WebSocket handlers and read by the feature engine, strategies, and the shadow
engine. Every stream carries a ``last_updated`` timestamp, and
:meth:`MarketDataStore.health` reports whether the system is safe to trade on.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..exchange.models import Candle, Ticker
from ..utils.logging import get_logger
from ..utils.numeric import safe_div
from ..utils.timeutil import now_ms
from .candles import CandleSeries

log = get_logger(__name__)


@dataclass(slots=True)
class OrderBookState:
    """Top-of-book plus aggregated depth, maintained from snapshot+delta frames."""

    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    last_update_ms: int = 0
    resets: int = 0
    valid: bool = False

    def apply(self, data: dict[str, Any], msg_type: str) -> None:
        """Apply an OKX ``books`` frame (``action`` snapshot/update).

        OKX levels are ``[price, size, liquidatedOrders, orderCount]``; a size
        of 0 deletes the level.
        """
        if msg_type == "snapshot":
            self.bids.clear()
            self.asks.clear()
            self.resets += 1
            self.valid = True
        for level in data.get("bids", []) or []:
            price, size = float(level[0]), float(level[1])
            if size == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = size
        for level in data.get("asks", []) or []:
            price, size = float(level[0]), float(level[1])
            if size == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = size
        self.last_update_ms = now_ms()

    @property
    def best_bid(self) -> float:
        return max(self.bids) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return min(self.asks) if self.asks else 0.0

    @property
    def spread(self) -> float:
        bid, ask = self.best_bid, self.best_ask
        return max(0.0, ask - bid) if bid and ask else 0.0

    @property
    def mid(self) -> float:
        bid, ask = self.best_bid, self.best_ask
        return (bid + ask) / 2.0 if bid and ask else 0.0

    def imbalance(self, levels: int = 10) -> float:
        """Order-book imbalance in ``[-1, 1]``; positive means bid-heavy.

        Only meaningful while the book is valid — the caller must check
        :attr:`valid`, because a mid-reconnect book produces garbage.
        """
        if not self.valid or not self.bids or not self.asks:
            return 0.0
        top_bids = sorted(self.bids.items(), key=lambda kv: -kv[0])[:levels]
        top_asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:levels]
        bid_volume = sum(size for _, size in top_bids)
        ask_volume = sum(size for _, size in top_asks)
        total = bid_volume + ask_volume
        return safe_div(bid_volume - ask_volume, total)


@dataclass(slots=True)
class TradeFlow:
    """Rolling window of public trades, for trade-flow imbalance."""

    window: deque[tuple[int, str, float]] = field(default_factory=lambda: deque(maxlen=2000))
    last_update_ms: int = 0

    def add(self, trades: list[dict[str, Any]]) -> None:
        """Ingest OKX ``trades`` items: ``{ts, side, sz, px, tradeId}``."""
        for trade in trades:
            try:
                self.window.append(
                    (
                        int(trade.get("ts", 0)),
                        str(trade.get("side", "")).lower(),
                        float(trade.get("sz", 0.0)),
                    )
                )
            except (TypeError, ValueError):
                continue
        self.last_update_ms = now_ms()

    def imbalance(self, lookback_seconds: int = 60) -> float:
        """Buy/sell volume imbalance in ``[-1, 1]`` over the recent window."""
        if not self.window:
            return 0.0
        cutoff = now_ms() - lookback_seconds * 1000
        buys = sells = 0.0
        for ts, side, volume in reversed(self.window):
            if ts < cutoff:
                break
            if side == "buy":
                buys += volume
            elif side == "sell":
                sells += volume
        total = buys + sells
        return safe_div(buys - sells, total)

    def volume(self, lookback_seconds: int = 60) -> float:
        cutoff = now_ms() - lookback_seconds * 1000
        return sum(v for ts, _, v in reversed(self.window) if ts >= cutoff)


@dataclass(slots=True)
class DataHealth:
    """A trade/no-trade verdict derived from stream freshness."""

    healthy: bool
    reasons: list[str] = field(default_factory=list)
    ticker_age: float = 0.0
    kline_age: float = 0.0
    orderbook_age: float = 0.0

    def describe(self) -> str:
        if self.healthy:
            return "healthy"
        return "; ".join(self.reasons)


class MarketDataStore:
    """The live view of one symbol across all configured timeframes."""

    def __init__(
        self,
        symbol: str,
        timeframes: list[str],
        *,
        candle_buffer: int = 1500,
        staleness_budgets: dict[str, float] | None = None,
    ) -> None:
        self.symbol = symbol
        self.timeframes = timeframes
        self.series: dict[str, CandleSeries] = {
            tf: CandleSeries(symbol, tf, max_size=candle_buffer) for tf in timeframes
        }
        self.orderbook = OrderBookState()
        self.trade_flow = TradeFlow()
        self.ticker: Ticker | None = None
        self.ticker_updated_ms: int = 0
        self._budgets = staleness_budgets or {"ticker": 25.0, "kline": 180.0, "orderbook": 45.0}
        self._last_price: float = 0.0
        self._new_bar_callbacks: list[Any] = []

    # --- writes ----------------------------------------------------------

    def update_candle(self, timeframe: str, candle: Candle) -> bool:
        series = self.series.get(timeframe)
        if series is None:
            return False
        is_new = series.upsert(candle)
        if candle.close > 0:
            self._last_price = candle.close
        return is_new

    def update_ticker(self, ticker: Ticker) -> None:
        self.ticker = ticker
        self.ticker_updated_ms = now_ms()
        if ticker.last_price > 0:
            self._last_price = ticker.last_price

    def update_orderbook(self, data: dict[str, Any], msg_type: str) -> None:
        self.orderbook.apply(data, msg_type)

    def update_trades(self, trades: list[dict[str, Any]]) -> None:
        self.trade_flow.add(trades)

    # --- reads -----------------------------------------------------------

    @property
    def last_price(self) -> float:
        """Best available current price: book mid, then ticker, then last close."""
        mid = self.orderbook.mid
        if mid > 0:
            return mid
        if self.ticker and self.ticker.last_price > 0:
            return self.ticker.last_price
        return self._last_price

    @property
    def spread(self) -> float:
        book_spread = self.orderbook.spread
        if book_spread > 0:
            return book_spread
        return self.ticker.spread if self.ticker else 0.0

    @property
    def spread_bps(self) -> float:
        price = self.last_price
        return (self.spread / price) * 10_000 if price > 0 else 0.0

    def closed_candles(self, timeframe: str, limit: int | None = None) -> list[Candle]:
        series = self.series.get(timeframe)
        return series.closed(limit=limit) if series else []

    def last_closed(self, timeframe: str) -> Candle | None:
        series = self.series.get(timeframe)
        return series.last_closed() if series else None

    def bars_available(self, timeframe: str) -> int:
        series = self.series.get(timeframe)
        return len(series.closed()) if series else 0

    # --- health ----------------------------------------------------------

    def health(self) -> DataHealth:
        """Whether the data is fresh enough to make trading decisions on."""
        now = now_ms()
        reasons: list[str] = []

        ticker_age = (
            (now - self.ticker_updated_ms) / 1000.0 if self.ticker_updated_ms else float("inf")
        )
        if ticker_age > self._budgets["ticker"]:
            reasons.append(
                f"ticker stale ({_fmt_age(ticker_age)} > {self._budgets['ticker']:.0f}s budget)"
            )

        kline_age = float("inf")
        fastest = self.timeframes[0] if self.timeframes else None
        if fastest:
            last = self.last_closed(fastest)
            if last is not None:
                kline_age = (now - last.open_ms) / 1000.0
        if kline_age > self._budgets["kline"]:
            reasons.append(
                f"candles stale ({_fmt_age(kline_age)} > {self._budgets['kline']:.0f}s budget)"
            )

        book_age = (
            (now - self.orderbook.last_update_ms) / 1000.0
            if self.orderbook.last_update_ms
            else float("inf")
        )
        # The order book is a nice-to-have: only microstructure strategies need
        # it, so a stale book degrades those rather than halting all trading.
        if book_age > self._budgets["orderbook"]:
            reasons.append(f"orderbook stale ({_fmt_age(book_age)})")

        critical = [r for r in reasons if r.startswith(("ticker", "candles"))]
        return DataHealth(
            healthy=not critical,
            reasons=reasons,
            ticker_age=ticker_age,
            kline_age=kline_age,
            orderbook_age=book_age,
        )

    def coverage_report(self) -> dict[str, dict[str, Any]]:
        return {
            tf: {
                "bars": len(series.closed()),
                "coverage": round(series.coverage_ratio(), 4),
                "gaps": len(series.missing_ranges()),
            }
            for tf, series in self.series.items()
        }

    def snapshot(self) -> dict[str, Any]:
        """Compact state for the dashboard."""
        health = self.health()
        return {
            "symbol": self.symbol,
            "last_price": self.last_price,
            "spread": self.spread,
            "spread_bps": round(self.spread_bps, 2),
            "best_bid": self.orderbook.best_bid,
            "best_ask": self.orderbook.best_ask,
            "orderbook_imbalance": round(self.orderbook.imbalance(), 4),
            "trade_flow_imbalance": round(self.trade_flow.imbalance(), 4),
            "volume_24h": self.ticker.volume_24h if self.ticker else 0.0,
            "healthy": health.healthy,
            "health_detail": health.describe(),
            "bars": {tf: len(s.closed()) for tf, s in self.series.items()},
        }


def _fmt_age(seconds: float) -> str:
    if seconds == float("inf"):
        return "never updated"
    return f"{seconds:.0f}s"
