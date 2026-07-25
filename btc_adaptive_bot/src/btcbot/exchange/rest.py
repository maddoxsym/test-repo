"""Bybit V5 REST client — demo host only.

Two classes live here:

* :class:`BybitDemoClient` — the full client. Its constructor refuses any host
  outside :data:`~btcbot.exchange.endpoints.ALLOWED_DEMO_HOSTS`, so an
  authenticated real-money client cannot be built at all.
* :class:`MainnetNegativeControlProbe` — a deliberately crippled read-only probe
  used by the demo guard. It has exactly one method, hits exactly one read-only
  endpoint, and **success is treated as a failure** by its caller.

There is no order, transfer, withdrawal, or deposit code anywhere except on
:class:`BybitDemoClient`, which cannot point at a real-money host.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..utils.errors import (
    ApiError,
    MainnetRejectedError,
    RateLimitError,
    TransportError,
)
from ..utils.logging import get_logger
from ..utils.timeutil import now_ms
from .endpoints import (
    ALLOWED_DEMO_HOSTS,
    DEMO_REST_HOST,
    NEGATIVE_CONTROL_HOST,
    NEGATIVE_CONTROL_PATH,
    Paths,
    is_allowed_authenticated_host,
)
from .models import (
    Candle,
    Category,
    ExchangePosition,
    Execution,
    InstrumentSpec,
    OpenOrder,
    OrderRequest,
    OrderResult,
    Ticker,
    WalletBalance,
)
from .signing import build_query_string, serialize_body, sign_request

log = get_logger(__name__)

# retCodes that indicate the route itself does not exist on this host.
ROUTE_NOT_FOUND_CODES = frozenset({10404, 10405})
# retCodes indicating an authentication problem (used by the negative control).
AUTH_FAILURE_CODES = frozenset({10003, 10004, 10005, 10002, 33004, 10010})
RETRYABLE_CODES = frozenset({10016, 10006, 10429})

KLINE_MAX_LIMIT = 1000  # documented maximum


class BybitDemoClient:
    """Async REST client bound to the Bybit demo host."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str = DEMO_REST_HOST,
        recv_window_ms: int = 5000,
        timeout_seconds: float = 15.0,
        max_retries: int = 4,
        backoff_base_seconds: float = 0.75,
    ) -> None:
        normalized = base_url.rstrip("/")
        if not is_allowed_authenticated_host(normalized):
            # The structural guarantee. Raised before any network activity.
            raise MainnetRejectedError(
                f"refusing to construct an exchange client for host {normalized!r}. "
                f"Only the Bybit demo host is permitted: {sorted(ALLOWED_DEMO_HOSTS)}. "
                "This system has no real-money trading mode."
            )
        self.base_url = normalized
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window = recv_window_ms
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "btc-adaptive-bot/1.0 (demo research)"},
        )
        # Measured (server_time - local_time); applied to every signed request so
        # a drifting local clock does not produce signature timestamp rejections.
        self._clock_offset_ms = 0
        self._consecutive_errors = 0

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    @property
    def clock_offset_ms(self) -> int:
        return self._clock_offset_ms

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BybitDemoClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # --- request plumbing ------------------------------------------------

    def _timestamp(self) -> int:
        return now_ms() + self._clock_offset_ms

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        authenticated: bool = False,
        allow_route_missing: bool = False,
    ) -> dict[str, Any]:
        """Send one V5 request with retries and error mapping.

        ``allow_route_missing`` lets the demo guard probe an endpoint's existence
        without treating "route not found" as an exception.
        """
        if authenticated and not self.has_credentials:
            raise ApiError(-1, "authenticated request attempted without credentials", path)

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                if authenticated:
                    assert self._api_key and self._api_secret  # guarded above
                    signed = sign_request(
                        api_key=self._api_key,
                        api_secret=self._api_secret,
                        method=method,
                        timestamp_ms=self._timestamp(),
                        recv_window=self._recv_window,
                        params=params,
                        body=body,
                    )
                    url = f"{path}?{signed.query_string}" if signed.query_string else path
                    response = await self._client.request(
                        method, url, headers=signed.headers, content=signed.body
                    )
                else:
                    query = build_query_string(params)
                    url = f"{path}?{query}" if query else path
                    content = serialize_body(body)
                    headers = {"Content-Type": "application/json"} if content else None
                    response = await self._client.request(
                        method, url, headers=headers, content=content
                    )

                if response.status_code == 403:
                    # Documented: US / Mainland China IPs are blocked by Bybit.
                    self._consecutive_errors += 1
                    raise TransportError(
                        f"Bybit returned 403 for {path}. Bybit documents that requests from "
                        "US or Mainland China IP addresses are refused, and access also "
                        "depends on your account's eligibility for the platform."
                    )
                if response.status_code == 429:
                    self._consecutive_errors += 1
                    raise RateLimitError(f"rate limited on {path}")
                response.raise_for_status()
                payload = response.json()

            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = TransportError(_transport_hint(exc, path))
                self._consecutive_errors += 1
            except RateLimitError as exc:
                last_error = exc
            else:
                ret_code = int(payload.get("retCode", -1))
                if ret_code == 0:
                    self._consecutive_errors = 0
                    return payload
                if ret_code in ROUTE_NOT_FOUND_CODES and allow_route_missing:
                    self._consecutive_errors = 0
                    return payload
                if ret_code in RETRYABLE_CODES and attempt < self._max_retries:
                    last_error = ApiError(ret_code, payload.get("retMsg", ""), path)
                else:
                    self._consecutive_errors += 1
                    raise ApiError(ret_code, payload.get("retMsg", ""), path)

            if attempt < self._max_retries:
                await asyncio.sleep(self._backoff_base * (2**attempt))

        assert last_error is not None
        raise last_error

    # --- public market data ----------------------------------------------

    async def sync_clock(self) -> int:
        """Measure and store the offset between Bybit's clock and ours."""
        local_before = now_ms()
        payload = await self._request("GET", Paths.SERVER_TIME)
        local_after = now_ms()
        result = payload.get("result", {})
        server_ms = int(result.get("timeNano", 0)) // 1_000_000 or int(payload.get("time", 0))
        if server_ms <= 0:
            return self._clock_offset_ms
        # Compensate for round-trip: assume the server timestamp was taken midway.
        self._clock_offset_ms = server_ms - (local_before + local_after) // 2
        if abs(self._clock_offset_ms) > 1000:
            log.warning(
                "BYBIT",
                f"Local clock differs from Bybit by {self._clock_offset_ms}ms — compensating. "
                "Consider enabling NTP time sync.",
                offset_ms=self._clock_offset_ms,
            )
        return self._clock_offset_ms

    async def get_instruments(
        self, category: Category, symbol: str | None = None
    ) -> list[InstrumentSpec]:
        payload = await self._request(
            "GET",
            Paths.INSTRUMENTS,
            params={"category": category.value, "symbol": symbol},
        )
        items = payload.get("result", {}).get("list", []) or []
        return [InstrumentSpec.from_response(item, category) for item in items]

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        *,
        category: Category = Category.SPOT,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 200,
    ) -> list[Candle]:
        """Fetch candles, oldest first.

        The API returns newest-first and its newest element may be an *unclosed*
        candle. We reverse to chronological order here; deciding what is safe to
        use is the caller's job via :meth:`Candle.is_closed`.
        """
        payload = await self._request(
            "GET",
            Paths.KLINE,
            params={
                "category": category.value,
                "symbol": symbol,
                "interval": interval,
                "start": start_ms,
                "end": end_ms,
                "limit": min(limit, KLINE_MAX_LIMIT),
            },
        )
        rows = payload.get("result", {}).get("list", []) or []
        candles = [Candle.from_rest(row, interval) for row in rows]
        candles.sort(key=lambda c: c.open_ms)
        return candles

    async def get_ticker(self, symbol: str, *, category: Category = Category.SPOT) -> Ticker:
        payload = await self._request(
            "GET", Paths.TICKERS, params={"category": category.value, "symbol": symbol}
        )
        items = payload.get("result", {}).get("list", []) or []
        if not items:
            raise ApiError(-1, f"no ticker returned for {symbol}", Paths.TICKERS)
        item = items[0]
        return Ticker(
            symbol=item.get("symbol", symbol),
            last_price=float(item.get("lastPrice") or 0.0),
            bid_price=float(item.get("bid1Price") or 0.0),
            ask_price=float(item.get("ask1Price") or 0.0),
            volume_24h=float(item.get("volume24h") or 0.0),
            turnover_24h=float(item.get("turnover24h") or 0.0),
            price_24h_pct=float(item.get("price24hPcnt") or 0.0),
            ts_ms=int(payload.get("time") or now_ms()),
        )

    async def get_orderbook(
        self, symbol: str, *, category: Category = Category.SPOT, depth: int = 50
    ) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            Paths.ORDERBOOK,
            params={"category": category.value, "symbol": symbol, "limit": depth},
        )
        return payload.get("result", {})

    # --- authenticated: account -----------------------------------------

    async def get_account_info(self) -> dict[str, Any]:
        payload = await self._request("GET", Paths.ACCOUNT_INFO, authenticated=True)
        return payload.get("result", {})

    async def get_api_key_info(self) -> dict[str, Any]:
        payload = await self._request("GET", Paths.QUERY_API, authenticated=True)
        return payload.get("result", {})

    async def get_wallet_balance(self, account_type: str = "UNIFIED") -> WalletBalance:
        payload = await self._request(
            "GET",
            Paths.WALLET_BALANCE,
            params={"accountType": account_type},
            authenticated=True,
        )
        items = payload.get("result", {}).get("list", []) or []
        if not items:
            raise ApiError(-1, "wallet balance response contained no accounts", Paths.WALLET_BALANCE)
        account = items[0]
        coins = {}
        for coin in account.get("coin", []) or []:
            name = coin.get("coin")
            if name:
                coins[name] = {
                    key: float(value)
                    for key, value in coin.items()
                    if key != "coin" and _is_number(value)
                }
        return WalletBalance(
            account_type=account.get("accountType", account_type),
            total_equity=float(account.get("totalEquity") or 0.0),
            total_available=float(account.get("totalAvailableBalance") or 0.0),
            total_wallet_balance=float(account.get("totalWalletBalance") or 0.0),
            unrealized_pnl=float(account.get("totalPerpUPL") or 0.0),
            coins=coins,
            ts_ms=int(payload.get("time") or now_ms()),
        )

    async def probe_demo_endpoint(self) -> dict[str, Any]:
        """Probe the demo-only funds endpoint **without moving any funds**.

        A zero amount is deliberately used: we care only about whether the route
        exists on this host. ``allow_route_missing`` means a "not found" reply is
        returned as data for the guard to inspect, rather than raising.
        """
        return await self._request(
            "POST",
            Paths.DEMO_APPLY_MONEY,
            body={"adjustType": 0, "utaDemoApplyMoney": [{"coin": "USDT", "amountStr": "0"}]},
            authenticated=True,
            allow_route_missing=True,
        )

    async def request_demo_funds(self, coin: str, amount: str) -> dict[str, Any]:
        """Top up the demo account (opt-in, demo-only endpoint).

        Documented maxima per request: BTC 15, ETH 200, USDT 100000, USDC 100000.
        Rate limit: 1 request per minute.
        """
        payload = await self._request(
            "POST",
            Paths.DEMO_APPLY_MONEY,
            body={"adjustType": 0, "utaDemoApplyMoney": [{"coin": coin, "amountStr": amount}]},
            authenticated=True,
        )
        return payload.get("result", {})

    # --- authenticated: trading -----------------------------------------

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Submit an order to the demo account.

        Callers must have passed the full validation pipeline first; this method
        deliberately performs no sizing logic of its own.
        """
        payload = await self._request(
            "POST", Paths.ORDER_CREATE, body=request.to_payload(), authenticated=True
        )
        result = payload.get("result", {}) or {}
        return OrderResult(
            client_order_id=result.get("orderLinkId", request.client_order_id),
            exchange_order_id=result.get("orderId", ""),
            accepted=True,
            raw=payload,
        )

    async def cancel_order(
        self,
        symbol: str,
        *,
        category: Category,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if not order_id and not client_order_id:
            raise ApiError(-1, "cancel requires order_id or client_order_id", Paths.ORDER_CANCEL)
        payload = await self._request(
            "POST",
            Paths.ORDER_CANCEL,
            body={
                "category": category.value,
                "symbol": symbol,
                "orderId": order_id,
                "orderLinkId": client_order_id,
            },
            authenticated=True,
        )
        return payload.get("result", {})

    async def cancel_all(self, symbol: str, *, category: Category) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            Paths.ORDER_CANCEL_ALL,
            body={"category": category.value, "symbol": symbol},
            authenticated=True,
        )
        return payload.get("result", {})

    async def get_open_orders(self, symbol: str, *, category: Category) -> list[OpenOrder]:
        payload = await self._request(
            "GET",
            Paths.ORDER_REALTIME,
            params={"category": category.value, "symbol": symbol},
            authenticated=True,
        )
        items = payload.get("result", {}).get("list", []) or []
        return [OpenOrder.from_response(item) for item in items]

    async def get_order_history(
        self, symbol: str, *, category: Category, limit: int = 50
    ) -> list[OpenOrder]:
        payload = await self._request(
            "GET",
            Paths.ORDER_HISTORY,
            params={"category": category.value, "symbol": symbol, "limit": limit},
            authenticated=True,
        )
        items = payload.get("result", {}).get("list", []) or []
        return [OpenOrder.from_response(item) for item in items]

    async def get_executions(
        self, symbol: str, *, category: Category, limit: int = 100, start_ms: int | None = None
    ) -> list[Execution]:
        payload = await self._request(
            "GET",
            Paths.EXECUTION_LIST,
            params={
                "category": category.value,
                "symbol": symbol,
                "limit": limit,
                "startTime": start_ms,
            },
            authenticated=True,
        )
        items = payload.get("result", {}).get("list", []) or []
        return [Execution.from_response(item) for item in items]

    async def get_positions(self, symbol: str, *, category: Category) -> list[ExchangePosition]:
        """Derivatives positions. Not applicable to spot, which holds coins instead."""
        if category is Category.SPOT:
            return []
        payload = await self._request(
            "GET",
            Paths.POSITION_LIST,
            params={"category": category.value, "symbol": symbol},
            authenticated=True,
        )
        items = payload.get("result", {}).get("list", []) or []
        return [ExchangePosition.from_response(item) for item in items]


class MainnetNegativeControlProbe:
    """Read-only probe whose job is to *fail*.

    Sends the supplied credentials once to a single read-only endpoint on the
    mainnet host. If they authenticate, the key can act on real money and the
    demo guard hard-refuses to trade with it.

    This class has no order method, no transfer method, and no way to send
    anything other than the one GET below. It is a safety assertion, not a
    trading path.
    """

    __slots__ = ("_api_key", "_api_secret", "_recv_window", "_timeout")

    def __init__(self, api_key: str, api_secret: str, *, recv_window_ms: int = 5000,
                 timeout_seconds: float = 10.0) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window = recv_window_ms
        self._timeout = timeout_seconds

    async def credentials_are_rejected(self) -> tuple[bool, str]:
        """``(rejected, detail)`` — ``rejected=True`` is the safe outcome."""
        signed = sign_request(
            api_key=self._api_key,
            api_secret=self._api_secret,
            method="GET",
            timestamp_ms=now_ms(),
            recv_window=self._recv_window,
            params=None,
        )
        url = f"{NEGATIVE_CONTROL_HOST}{NEGATIVE_CONTROL_PATH}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
                response = await client.get(url, headers=signed.headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # Could not reach the host. We cannot assert the key is demo-scoped,
            # so this is inconclusive — and inconclusive means "do not trade".
            return (False, f"negative control inconclusive: {type(exc).__name__}: {exc}")

        if response.status_code == 403:
            # Region-blocked. The key was not accepted, but we also learned nothing.
            return (False, "negative control inconclusive: mainnet host returned 403 (IP region block)")

        try:
            payload = response.json()
        except ValueError:
            return (False, f"negative control inconclusive: unparseable response ({response.status_code})")

        ret_code = int(payload.get("retCode", -1))
        if ret_code == 0:
            return (
                False,
                "THESE CREDENTIALS AUTHENTICATE ON THE BYBIT MAINNET HOST. They are not "
                "demo-only keys. Create a key from within Bybit's Demo Trading area instead.",
            )
        if ret_code in AUTH_FAILURE_CODES:
            return (True, f"mainnet rejected the key as expected (retCode={ret_code})")
        return (False, f"negative control inconclusive: unexpected retCode={ret_code} ({payload.get('retMsg')})")


def _transport_hint(exc: Exception, path: str) -> str:
    """Turn a transport failure into something the operator can act on.

    A bare "ProxyError: 403" tells the user nothing. A 403 reaching Bybit has
    three realistic causes, and naming them saves a long debugging session.
    """
    detail = f"{type(exc).__name__} on {path}: {exc}"
    text = str(exc)
    if "403" in text or isinstance(exc, httpx.ProxyError):
        return (
            f"{detail}\n"
            "  A 403 while reaching Bybit usually means one of:\n"
            "   1. You are behind an HTTP proxy or firewall that blocks api-demo.bybit.com\n"
            "      (check HTTPS_PROXY / HTTP_PROXY in your shell).\n"
            "   2. Your IP is in a region Bybit refuses — Bybit documents that requests\n"
            "      from US and Mainland China IP addresses are rejected.\n"
            "   3. Your account is not eligible to use the platform from this location.\n"
            "  This system does not attempt to work around any of these."
        )
    return detail


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
