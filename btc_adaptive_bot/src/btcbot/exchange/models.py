"""Typed exchange data models (OKX API v5).

These are built from *what the exchange actually returned*, never from assumed
defaults. In particular :class:`InstrumentSpec` carries the discovered contract
parameters (``ctVal``/``ctMult``/``lotSz``/…) of the selected X-Perp, and every
contract↔base-quantity conversion in the system goes through its methods —
nothing downstream hardcodes a contract size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from enum import Enum
from typing import Any

from ..utils.numeric import round_step_down, round_to_tick, to_decimal
from ..utils.timeutil import is_candle_closed


class InstType(str, Enum):
    """OKX instrument types this system recognises. Only SWAP is traded."""

    SPOT = "SPOT"
    SWAP = "SWAP"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PosSide(str, Enum):
    """Position side. ``net`` in net mode; ``long``/``short`` in long/short mode."""

    LONG = "long"
    SHORT = "short"
    NET = "net"


class TdMode(str, Enum):
    """Trade/margin mode. This system uses isolated only — no cross fallback."""

    ISOLATED = "isolated"
    CROSS = "cross"


class PositionMode(str, Enum):
    """Account-level position mode, read from ``/api/v5/account/config``."""

    NET = "net_mode"
    LONG_SHORT = "long_short_mode"


class OrderType(str, Enum):
    """OKX ``ordType``. Time-in-force is folded into the order type on OKX."""

    MARKET = "market"
    LIMIT = "limit"
    POST_ONLY = "post_only"
    IOC = "ioc"
    FOK = "fok"


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
    list — OKX returns candles newest-first and flags unfinished bars with
    ``confirm == "0"``.
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
    def from_okx_row(cls, row: list[str], timeframe: str) -> Candle:
        """Build from an OKX candle row (REST and WS share the layout).

        Layout: ``[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]`` —
        ``confirm`` is ``"0"`` while the bar is still forming. Older/history
        rows may omit trailing fields, which is tolerated.
        """
        turnover = float(row[7]) if len(row) > 7 else 0.0
        confirmed = (str(row[8]) == "1") if len(row) > 8 else False
        return cls(
            open_ms=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            turnover=turnover,
            timeframe=timeframe,
            confirmed=confirmed,
        )


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Trading rules and contract parameters for one instrument, as reported
    by ``/api/v5/public/instruments``. Values are never assumed."""

    inst_id: str
    inst_type: InstType
    base_ccy: str            # underlying base (BTC), from uly/ctValCcy
    quote_ccy: str           # quote leg of the underlying (USDT/USDC/USD)
    settle_ccy: str
    ct_type: str             # "linear" | "inverse" ("" for spot)
    ct_val: Decimal          # face value of one contract
    ct_val_ccy: str          # currency of the face value
    ct_mult: Decimal
    state: str               # "live" is tradable
    tick_size: Decimal
    lot_size: Decimal        # order size increment, in contracts
    min_size: Decimal        # minimum order size, in contracts
    max_lmt_size: Decimal | None
    max_mkt_size: Decimal | None
    max_leverage: Decimal
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_tradable(self) -> bool:
        return self.state == "live"

    @property
    def is_derivative(self) -> bool:
        return self.inst_type is InstType.SWAP

    @property
    def is_linear(self) -> bool:
        return self.ct_type == "linear"

    # Compatibility aliases used by generic code paths.
    @property
    def symbol(self) -> str:
        return self.inst_id

    @property
    def qty_step(self) -> Decimal:
        return self.lot_size

    @property
    def min_order_qty(self) -> Decimal:
        return self.min_size

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    # --- contract ↔ base-quantity conversion -----------------------------

    def contract_base_value(self) -> Decimal:
        """Base-currency quantity represented by one contract (linear).

        For the linear X-Perp, ``ctValCcy`` is the base currency, so one
        contract represents ``ctVal × ctMult`` of it.
        """
        return self.ct_val * self.ct_mult

    def contracts_from_base(self, base_qty: float | Decimal) -> Decimal:
        """Convert a base-currency quantity to contracts, rounded **down** to
        the lot size. Rounding down keeps risk at or below the intended level."""
        per_contract = self.contract_base_value()
        if per_contract <= 0:
            raise ValueError(f"invalid contract value {self.ct_val} × {self.ct_mult}")
        raw = to_decimal(base_qty) / per_contract
        lots = (raw / self.lot_size).to_integral_value(rounding=ROUND_FLOOR)
        return lots * self.lot_size

    def base_from_contracts(self, contracts: float | Decimal) -> Decimal:
        return to_decimal(contracts) * self.contract_base_value()

    def notional_usd(self, contracts: float | Decimal, price: float | Decimal) -> Decimal:
        """Quote-currency notional of a linear-contract position at ``price``."""
        return self.base_from_contracts(contracts) * to_decimal(price)

    # --- rounding + bounds (in contracts) --------------------------------

    def round_qty(self, contracts: float | Decimal) -> Decimal:
        """Round a contract quantity down to a valid lot."""
        return round_step_down(contracts, self.lot_size)

    def round_price(self, price: float | Decimal) -> Decimal:
        return round_to_tick(price, self.tick_size)

    def qty_within_bounds(self, contracts: Decimal) -> tuple[bool, str]:
        """Validate a rounded contract quantity against exchange limits."""
        if contracts <= 0:
            return (False, "quantity rounds to zero at the exchange lot size")
        if contracts < self.min_size:
            return (False, f"quantity {contracts} below minimum {self.min_size} contracts")
        if self.max_mkt_size is not None and contracts > self.max_mkt_size:
            return (False, f"quantity {contracts} above market-order maximum {self.max_mkt_size}")
        return (True, "")

    @classmethod
    def from_response(cls, item: dict[str, Any]) -> InstrumentSpec:
        """Parse one ``/api/v5/public/instruments`` entry."""
        inst_type = InstType(item.get("instType", "SWAP"))
        uly = item.get("uly") or item.get("instFamily") or ""
        base, _, quote = uly.partition("-")
        if inst_type is InstType.SPOT:
            base = item.get("baseCcy", base)
            quote = item.get("quoteCcy", quote)
            capabilities = {
                Capability.LONG,
                Capability.MARKET_ORDER,
                Capability.LIMIT_ORDER,
            }
        else:
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
            inst_id=item["instId"],
            inst_type=inst_type,
            base_ccy=base,
            quote_ccy=quote,
            settle_ccy=item.get("settleCcy", ""),
            ct_type=item.get("ctType", ""),
            ct_val=to_decimal(item.get("ctVal") or "0"),
            ct_val_ccy=item.get("ctValCcy", ""),
            ct_mult=to_decimal(item.get("ctMult") or "1"),
            state=item.get("state", "unknown"),
            tick_size=to_decimal(item.get("tickSz") or "0.1"),
            lot_size=to_decimal(item.get("lotSz") or "1"),
            min_size=to_decimal(item.get("minSz") or "1"),
            max_lmt_size=to_decimal(item["maxLmtSz"]) if item.get("maxLmtSz") else None,
            max_mkt_size=to_decimal(item["maxMktSz"]) if item.get("maxMktSz") else None,
            max_leverage=to_decimal(item.get("lever") or "1"),
            capabilities=frozenset(capabilities),
            raw=item,
        )


@dataclass(frozen=True, slots=True)
class Ticker:
    inst_id: str
    last_price: float
    bid_price: float
    ask_price: float
    volume_24h: float
    turnover_24h: float
    price_24h_pct: float
    ts_ms: int

    # Compatibility alias for exchange-independent consumers.
    @property
    def symbol(self) -> str:
        return self.inst_id

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

    @classmethod
    def from_response(cls, item: dict[str, Any], *, ts_ms: int) -> Ticker:
        last = float(item.get("last") or 0.0)
        open_24h = float(item.get("open24h") or 0.0)
        pct = ((last - open_24h) / open_24h) if open_24h > 0 else 0.0
        return cls(
            inst_id=item.get("instId", ""),
            last_price=last,
            bid_price=float(item.get("bidPx") or 0.0),
            ask_price=float(item.get("askPx") or 0.0),
            volume_24h=float(item.get("vol24h") or 0.0),
            turnover_24h=float(item.get("volCcy24h") or 0.0),
            price_24h_pct=pct,
            ts_ms=ts_ms,
        )


@dataclass(frozen=True, slots=True)
class AccountConfig:
    """Account-level configuration from ``/api/v5/account/config``.

    ``position_mode`` decides how orders must be shaped (``posSide`` vs
    ``reduceOnly``); the system adapts to it rather than changing it.
    """

    uid: str
    account_level: str
    position_mode: PositionMode
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, item: dict[str, Any]) -> AccountConfig:
        pos_mode_raw = item.get("posMode", "net_mode")
        try:
            pos_mode = PositionMode(pos_mode_raw)
        except ValueError:
            pos_mode = PositionMode.NET
        return cls(
            uid=str(item.get("uid", "")),
            account_level=str(item.get("acctLv", "")),
            position_mode=pos_mode,
            raw=item,
        )


@dataclass(frozen=True, slots=True)
class WalletBalance:
    """Unified account balance from ``/api/v5/account/balance``."""

    total_equity: float          # totalEq — USD value of all equity
    total_available: float       # USD-approximate available balance
    unrealized_pnl: float
    coins: dict[str, dict[str, float]]
    ts_ms: int

    # Kept for report/dashboard compatibility.
    @property
    def total_wallet_balance(self) -> float:
        return self.total_equity

    def coin_balance(self, ccy: str) -> float:
        return float(self.coins.get(ccy, {}).get("eq", 0.0))

    def coin_available(self, ccy: str) -> float:
        entry = self.coins.get(ccy, {})
        for key in ("availEq", "availBal", "cashBal"):
            value = entry.get(key)
            if value:
                return float(value)
        return 0.0

    @classmethod
    def from_response(cls, item: dict[str, Any], *, ts_ms: int) -> WalletBalance:
        coins: dict[str, dict[str, float]] = {}
        available_usd = 0.0
        upl_total = 0.0
        for detail in item.get("details", []) or []:
            ccy = detail.get("ccy")
            if not ccy:
                continue
            numeric = {
                key: float(value)
                for key, value in detail.items()
                if key != "ccy" and _is_number(value)
            }
            coins[ccy] = numeric
            upl_total += numeric.get("upl", 0.0)
            # Approximate the USD value of this coin's available balance using
            # the ratio of its reported USD equity to its native equity.
            avail = numeric.get("availEq") or numeric.get("availBal") or 0.0
            eq = numeric.get("eq", 0.0)
            eq_usd = numeric.get("eqUsd", 0.0)
            if avail > 0:
                rate = (eq_usd / eq) if eq > 0 and eq_usd > 0 else 1.0
                available_usd += avail * rate
        return cls(
            total_equity=float(item.get("totalEq") or 0.0),
            total_available=available_usd,
            unrealized_pnl=upl_total,
            coins=coins,
            ts_ms=ts_ms,
        )


@dataclass(frozen=True, slots=True)
class LeverageInfo:
    """Confirmed leverage from ``/api/v5/account/leverage-info``."""

    inst_id: str
    margin_mode: str
    pos_side: str
    leverage: Decimal

    @classmethod
    def from_response(cls, item: dict[str, Any]) -> LeverageInfo:
        return cls(
            inst_id=item.get("instId", ""),
            margin_mode=item.get("mgnMode", ""),
            pos_side=item.get("posSide", ""),
            leverage=to_decimal(item.get("lever") or "1"),
        )


@dataclass(frozen=True, slots=True)
class FundingRate:
    """Current and next funding from ``/api/v5/public/funding-rate``."""

    inst_id: str
    funding_rate: float
    next_funding_rate: float | None
    funding_time_ms: int
    next_funding_time_ms: int

    @classmethod
    def from_response(cls, item: dict[str, Any]) -> FundingRate:
        next_rate = item.get("nextFundingRate")
        return cls(
            inst_id=item.get("instId", ""),
            funding_rate=float(item.get("fundingRate") or 0.0),
            next_funding_rate=float(next_rate) if next_rate not in (None, "") else None,
            funding_time_ms=int(item.get("fundingTime") or 0),
            next_funding_time_ms=int(item.get("nextFundingTime") or 0),
        )


@dataclass(frozen=True, slots=True)
class FeeRates:
    """Account fee schedule from ``/api/v5/account/trade-fee``.

    OKX reports fees as negative numbers when charged (``-0.0005`` = 0.05%
    cost) and positive for rebates. Stored here as *cost rates*: positive
    means the trade costs money, which is what every PnL model expects.
    """

    maker: float
    taker: float

    @classmethod
    def from_response(cls, item: dict[str, Any]) -> FeeRates:
        return cls(
            maker=-float(item.get("maker") or 0.0),
            taker=-float(item.get("taker") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """A validated order, ready to submit.

    Constructed only by the execution layer after the full sizing + leverage
    pipeline has passed. ``client_order_id`` carries attribution and is the
    duplicate key. ``sz`` is in **contracts**, pre-formatted.
    """

    inst_id: str
    td_mode: TdMode
    side: Side
    order_type: OrderType
    sz: str
    client_order_id: str
    pos_side: PosSide | None = None      # required in long/short mode
    price: str | None = None
    reduce_only: bool | None = None      # net mode: marks closing orders
    tp_trigger_price: str | None = None
    sl_trigger_price: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instId": self.inst_id,
            "tdMode": self.td_mode.value,
            "side": self.side.value,
            "ordType": self.order_type.value,
            "sz": self.sz,
            "clOrdId": self.client_order_id,
        }
        if self.pos_side is not None:
            payload["posSide"] = self.pos_side.value
        if self.price is not None:
            payload["px"] = self.price
        if self.reduce_only is not None:
            payload["reduceOnly"] = self.reduce_only
        if self.tp_trigger_price is not None or self.sl_trigger_price is not None:
            attach: dict[str, Any] = {}
            if self.tp_trigger_price is not None:
                attach["tpTriggerPx"] = self.tp_trigger_price
                attach["tpOrdPx"] = "-1"  # execute the take-profit at market
            if self.sl_trigger_price is not None:
                attach["slTriggerPx"] = self.sl_trigger_price
                attach["slOrdPx"] = "-1"  # execute the stop at market
            payload["attachAlgoOrds"] = [attach]
        return payload


@dataclass(frozen=True, slots=True)
class OrderResult:
    client_order_id: str
    exchange_order_id: str
    accepted: bool
    s_code: int = 0
    s_msg: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Execution:
    """A fill from ``/api/v5/trade/fills`` or the private ``orders`` channel."""

    exec_id: str
    order_id: str
    client_order_id: str
    inst_id: str
    side: Side
    pos_side: str
    price: float
    qty: float               # contracts
    fee: float               # cost-positive: >0 means the fill cost money
    fee_currency: str
    is_maker: bool
    exec_ts_ms: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.inst_id

    @classmethod
    def from_response(cls, item: dict[str, Any]) -> Execution:
        return cls(
            exec_id=item.get("tradeId", ""),
            order_id=item.get("ordId", ""),
            client_order_id=item.get("clOrdId", ""),
            inst_id=item.get("instId", ""),
            side=Side(item.get("side", "buy")),
            pos_side=item.get("posSide", ""),
            price=float(item.get("fillPx") or 0.0),
            qty=float(item.get("fillSz") or 0.0),
            # OKX reports fees negative-when-charged; flip to cost-positive.
            fee=-float(item.get("fee") or 0.0),
            fee_currency=item.get("feeCcy", ""),
            is_maker=item.get("execType", "") == "M",
            exec_ts_ms=int(item.get("ts") or 0),
            raw=item,
        )

    @classmethod
    def from_order_update(cls, item: dict[str, Any]) -> Execution:
        """Build from a private ``orders`` channel update carrying a fill.

        The WS order update names its fill fields differently from the REST
        fills endpoint (``fillFee``/``fillFeeCcy``/``fillTime``).
        """
        return cls(
            exec_id=item.get("tradeId", ""),
            order_id=item.get("ordId", ""),
            client_order_id=item.get("clOrdId", ""),
            inst_id=item.get("instId", ""),
            side=Side(item.get("side", "buy")),
            pos_side=item.get("posSide", ""),
            price=float(item.get("fillPx") or 0.0),
            qty=float(item.get("fillSz") or 0.0),
            fee=-float(item.get("fillFee") or 0.0),
            fee_currency=item.get("fillFeeCcy", ""),
            is_maker=item.get("execType", "") == "M",
            exec_ts_ms=int(item.get("fillTime") or item.get("uTime") or 0),
            raw=item,
        )


@dataclass(frozen=True, slots=True)
class OpenOrder:
    order_id: str
    client_order_id: str
    inst_id: str
    side: Side
    pos_side: str
    order_type: str
    qty: float               # contracts
    filled_qty: float
    price: float
    avg_price: float
    status: str              # live | partially_filled | filled | canceled
    leverage: float
    created_ms: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.inst_id

    @classmethod
    def from_response(cls, item: dict[str, Any]) -> OpenOrder:
        return cls(
            order_id=item.get("ordId", ""),
            client_order_id=item.get("clOrdId", ""),
            inst_id=item.get("instId", ""),
            side=Side(item.get("side", "buy")),
            pos_side=item.get("posSide", ""),
            order_type=item.get("ordType", ""),
            qty=float(item.get("sz") or 0.0),
            filled_qty=float(item.get("accFillSz") or 0.0),
            price=float(item.get("px") or 0.0),
            avg_price=float(item.get("avgPx") or 0.0),
            status=item.get("state", ""),
            leverage=float(item.get("lever") or 0.0),
            created_ms=int(item.get("cTime") or 0),
            raw=item,
        )


@dataclass(frozen=True, slots=True)
class ExchangePosition:
    """A perpetual-swap position from ``/api/v5/account/positions``.

    Carries the liquidation-relevant fields the protection layer needs:
    ``liq_price``, ``margin_ratio``, ``imr``/``mmr``, and the margin mode.
    """

    inst_id: str
    pos_side: str            # long | short | net
    contracts: float         # signed in net mode
    avg_price: float
    unrealized_pnl: float
    leverage: float
    liq_price: float | None
    margin_mode: str
    margin_ratio: float | None
    imr: float | None
    mmr: float | None
    mark_price: float | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.inst_id

    @property
    def size(self) -> float:
        return abs(self.contracts)

    @property
    def entry_price(self) -> float:
        return self.avg_price

    @property
    def direction(self) -> str:
        """LONG/SHORT/FLAT resolved across both position modes."""
        if self.pos_side == "long":
            return "LONG"
        if self.pos_side == "short":
            return "SHORT"
        if self.contracts > 0:
            return "LONG"
        if self.contracts < 0:
            return "SHORT"
        return "FLAT"

    @classmethod
    def from_response(cls, item: dict[str, Any]) -> ExchangePosition:
        def _opt(key: str) -> float | None:
            value = item.get(key)
            if value in (None, ""):
                return None
            return float(value)

        return cls(
            inst_id=item.get("instId", ""),
            pos_side=item.get("posSide", "net"),
            contracts=float(item.get("pos") or 0.0),
            avg_price=float(item.get("avgPx") or 0.0),
            unrealized_pnl=float(item.get("upl") or 0.0),
            leverage=float(item.get("lever") or 1.0),
            liq_price=_opt("liqPx"),
            margin_mode=item.get("mgnMode", ""),
            margin_ratio=_opt("mgnRatio"),
            imr=_opt("imr"),
            mmr=_opt("mmr"),
            mark_price=_opt("markPx"),
            raw=item,
        )


def _is_number(value: Any) -> bool:
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return False
        return value.strip() != ""
    return False
