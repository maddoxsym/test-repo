"""Typed exchange data models.

These are built from *what the exchange actually returned*, never from assumed
defaults. In particular :class:`InstrumentSpec` carries a ``capabilities`` set
derived from the live instruments-info response, and the executor consults it
instead of hardcoding "spot means long-only" anywhere downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from ..utils.numeric import round_step_down, round_to_tick, to_decimal
from ..utils.timeutil import is_candle_closed


class Category(str, Enum):
    SPOT = "spot"
    LINEAR = "linear"
    INVERSE = "inverse"


class Side(str, Enum):
    BUY = "Buy"
    SELL = "Sell"


class OrderType(str, Enum):
    MARKET = "Market"
    LIMIT = "Limit"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    POST_ONLY = "PostOnly"


class Capability(str, Enum):
    """Capabilities discovered at runtime, not assumed."""

    LONG = "long"
    SHORT = "short"
    LEVERAGE = "leverage"
    MARKET_ORDER = "market_order"
    LIMIT_ORDER = "limit_order"
    REDUCE_ONLY = "reduce_only"
    ATTACHED_TPSL = "attached_tpsl"


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLCV bar.

    ``open_ms`` is the bar's opening instant. Whether the bar may be used by
    strategy logic is decided by :meth:`is_closed`, never by its position in a
    list — Bybit's kline endpoint returns the in-progress candle first.
    """

    open_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float
    timeframe: str
    confirmed: bool = False

    def is_closed(self, *, now_ms_value: int | None = None) -> bool:
        if self.confirmed:
            return True
        return is_candle_closed(self.open_ms, self.timeframe, now=now_ms_value)

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @classmethod
    def from_rest(cls, row: list[str], timeframe: str) -> Candle:
        """Build from a ``/v5/market/kline`` list element.

        Layout per docs: ``[startTime, open, high, low, close, volume, turnover]``.
        """
        return cls(
            open_ms=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            turnover=float(row[6]),
            timeframe=timeframe,
            confirmed=False,
        )

    @classmethod
    def from_ws(cls, payload: dict[str, Any], timeframe: str) -> Candle:
        """Build from a ``kline.{interval}.{symbol}`` websocket message."""
        return cls(
            open_ms=int(payload["start"]),
            open=float(payload["open"]),
            high=float(payload["high"]),
            low=float(payload["low"]),
            close=float(payload["close"]),
            volume=float(payload["volume"]),
            turnover=float(payload.get("turnover", 0.0)),
            timeframe=timeframe,
            confirmed=bool(payload.get("confirm", False)),
        )


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Trading rules for one instrument, as reported by the exchange."""

    symbol: str
    category: Category
    base_coin: str
    quote_coin: str
    status: str
    tick_size: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    max_order_qty: Decimal
    min_order_amt: Decimal | None       # spot: minimum notional
    max_order_amt: Decimal | None
    max_market_order_qty: Decimal | None
    base_precision: Decimal | None
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    margin_trading: str = "none"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_tradable(self) -> bool:
        return self.status == "Trading"

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def round_qty(self, qty: float | Decimal) -> Decimal:
        """Round a quantity down to a valid step."""
        return round_step_down(qty, self.qty_step)

    def round_price(self, price: float | Decimal) -> Decimal:
        return round_to_tick(price, self.tick_size)

    def qty_within_bounds(self, qty: Decimal) -> tuple[bool, str]:
        """Validate a rounded quantity against exchange limits."""
        if qty <= 0:
            return (False, "quantity rounds to zero at the exchange step size")
        if qty < self.min_order_qty:
            return (False, f"quantity {qty} below minimum {self.min_order_qty}")
        if qty > self.max_order_qty:
            return (False, f"quantity {qty} above maximum {self.max_order_qty}")
        return (True, "")

    def notional_within_bounds(self, notional: Decimal) -> tuple[bool, str]:
        """Validate notional against spot's ``minOrderAmt``/``maxOrderAmt``.

        Bybit's own docs mark spot ``minOrderQty`` deprecated in favour of
        ``minOrderAmt``, so notional is the binding constraint for spot.
        """
        if self.min_order_amt is not None and notional < self.min_order_amt:
            return (False, f"notional {notional} below exchange minimum {self.min_order_amt}")
        if self.max_order_amt is not None and notional > self.max_order_amt:
            return (False, f"notional {notional} above exchange maximum {self.max_order_amt}")
        return (True, "")

    @classmethod
    def from_response(cls, item: dict[str, Any], category: Category) -> InstrumentSpec:
        """Parse an instruments-info entry, tolerating category field differences."""
        lot = item.get("lotSizeFilter", {}) or {}
        price_filter = item.get("priceFilter", {}) or {}

        tick_size = to_decimal(price_filter.get("tickSize") or "0.01")

        if category is Category.SPOT:
            # Spot describes size via basePrecision; qtyStep is absent.
            base_precision = to_decimal(lot.get("basePrecision") or "0.000001")
            qty_step = base_precision
            min_qty = to_decimal(lot.get("minOrderQty") or base_precision)
            max_limit = lot.get("maxLimitOrderQty") or lot.get("maxOrderQty") or "1000000"
            max_qty = to_decimal(max_limit)
            max_market = (
                to_decimal(lot["maxMarketOrderQty"]) if lot.get("maxMarketOrderQty") else None
            )
            min_amt = to_decimal(lot["minOrderAmt"]) if lot.get("minOrderAmt") else None
            max_amt = to_decimal(lot["maxOrderAmt"]) if lot.get("maxOrderAmt") else None
            # Spot has no short side and no leverage. Both are facts about the
            # product, discovered here rather than assumed at the call site.
            capabilities = {
                Capability.LONG,
                Capability.MARKET_ORDER,
                Capability.LIMIT_ORDER,
                Capability.ATTACHED_TPSL,
            }
        else:
            base_precision = None
            qty_step = to_decimal(lot.get("qtyStep") or "0.001")
            min_qty = to_decimal(lot.get("minOrderQty") or qty_step)
            max_qty = to_decimal(lot.get("maxOrderQty") or "1000000")
            max_market = (
                to_decimal(lot["maxMktOrderQty"]) if lot.get("maxMktOrderQty") else None
            )
            min_amt = None
            max_amt = None
            capabilities = {
                Capability.LONG,
                Capability.SHORT,
                Capability.LEVERAGE,
                Capability.MARKET_ORDER,
                Capability.LIMIT_ORDER,
                Capability.REDUCE_ONLY,
                Capability.ATTACHED_TPSL,
            }

        return cls(
            symbol=item["symbol"],
            category=category,
            base_coin=item.get("baseCoin", ""),
            quote_coin=item.get("quoteCoin", ""),
            status=item.get("status", "Unknown"),
            tick_size=tick_size,
            qty_step=qty_step,
            min_order_qty=min_qty,
            max_order_qty=max_qty,
            min_order_amt=min_amt,
            max_order_amt=max_amt,
            max_market_order_qty=max_market,
            base_precision=base_precision,
            capabilities=frozenset(capabilities),
            margin_trading=item.get("marginTrading", "none"),
            raw=item,
        )


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    last_price: float
    bid_price: float
    ask_price: float
    volume_24h: float
    turnover_24h: float
    price_24h_pct: float
    ts_ms: int

    @property
    def mid_price(self) -> float:
        if self.bid_price > 0 and self.ask_price > 0:
            return (self.bid_price + self.ask_price) / 2.0
        return self.last_price

    @property
    def spread(self) -> float:
        if self.bid_price > 0 and self.ask_price > 0:
            return max(0.0, self.ask_price - self.bid_price)
        return 0.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid_price
        return (self.spread / mid) * 10_000 if mid > 0 else 0.0


@dataclass(frozen=True, slots=True)
class WalletBalance:
    """Unified account balance, straight from ``/v5/account/wallet-balance``."""

    account_type: str
    total_equity: float
    total_available: float
    total_wallet_balance: float
    unrealized_pnl: float
    coins: dict[str, dict[str, float]]
    ts_ms: int

    def coin_balance(self, coin: str) -> float:
        return float(self.coins.get(coin, {}).get("walletBalance", 0.0))

    def coin_available(self, coin: str) -> float:
        entry = self.coins.get(coin, {})
        for key in ("availableToWithdraw", "free", "walletBalance"):
            value = entry.get(key)
            if value:
                return float(value)
        return 0.0


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """A validated order, ready to submit.

    Constructed only by the execution layer after the full sizing pipeline has
    passed. ``client_order_id`` carries attribution and is the duplicate key.
    """

    symbol: str
    category: Category
    side: Side
    order_type: OrderType
    qty: str                      # pre-formatted exchange string
    client_order_id: str
    price: str | None = None
    time_in_force: TimeInForce | None = None
    market_unit: str | None = None       # spot market orders: baseCoin | quoteCoin
    take_profit: str | None = None
    stop_loss: str | None = None
    reduce_only: bool | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "category": self.category.value,
            "symbol": self.symbol,
            "side": self.side.value,
            "orderType": self.order_type.value,
            "qty": self.qty,
            "orderLinkId": self.client_order_id,
        }
        if self.price is not None:
            payload["price"] = self.price
        if self.time_in_force is not None:
            payload["timeInForce"] = self.time_in_force.value
        if self.market_unit is not None:
            payload["marketUnit"] = self.market_unit
        if self.take_profit is not None:
            payload["takeProfit"] = self.take_profit
        if self.stop_loss is not None:
            payload["stopLoss"] = self.stop_loss
        if self.reduce_only is not None:
            payload["reduceOnly"] = self.reduce_only
        return payload


@dataclass(frozen=True, slots=True)
class OrderResult:
    client_order_id: str
    exchange_order_id: str
    accepted: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Execution:
    """A fill from ``/v5/execution/list`` or the private ``execution`` topic."""

    exec_id: str
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    price: float
    qty: float
    fee: float
    fee_currency: str
    is_maker: bool
    exec_ts_ms: int
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, item: dict[str, Any]) -> Execution:
        return cls(
            exec_id=item.get("execId", ""),
            order_id=item.get("orderId", ""),
            client_order_id=item.get("orderLinkId", ""),
            symbol=item.get("symbol", ""),
            side=Side(item.get("side", "Buy")),
            price=float(item.get("execPrice") or 0.0),
            qty=float(item.get("execQty") or 0.0),
            fee=float(item.get("execFee") or 0.0),
            fee_currency=item.get("feeCurrency", ""),
            is_maker=bool(item.get("isMaker", False)),
            exec_ts_ms=int(item.get("execTime") or 0),
            raw=item,
        )


@dataclass(frozen=True, slots=True)
class OpenOrder:
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    order_type: str
    qty: float
    filled_qty: float
    price: float
    status: str
    created_ms: int
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, item: dict[str, Any]) -> OpenOrder:
        return cls(
            order_id=item.get("orderId", ""),
            client_order_id=item.get("orderLinkId", ""),
            symbol=item.get("symbol", ""),
            side=Side(item.get("side", "Buy")),
            order_type=item.get("orderType", ""),
            qty=float(item.get("qty") or 0.0),
            filled_qty=float(item.get("cumExecQty") or 0.0),
            price=float(item.get("price") or 0.0),
            status=item.get("orderStatus", ""),
            created_ms=int(item.get("createdTime") or 0),
            raw=item,
        )


@dataclass(frozen=True, slots=True)
class ExchangePosition:
    """A derivatives position (only present when a derivatives category is live)."""

    symbol: str
    side: str
    size: float
    entry_price: float
    unrealized_pnl: float
    leverage: float
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, item: dict[str, Any]) -> ExchangePosition:
        return cls(
            symbol=item.get("symbol", ""),
            side=item.get("side", "None"),
            size=float(item.get("size") or 0.0),
            entry_price=float(item.get("avgPrice") or 0.0),
            unrealized_pnl=float(item.get("unrealisedPnl") or 0.0),
            leverage=float(item.get("leverage") or 1.0),
            raw=item,
        )
