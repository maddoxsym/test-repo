"""OKX API v5 REST client — EEA host, demo environment only.

Two classes live here:

* :class:`OkxDemoClient` — the full client. Its constructor refuses any host
  outside :data:`~btcbot.exchange.endpoints.ALLOWED_DEMO_HOSTS`, and **every**
  request it emits — public or authenticated — passes through the single
  header builder :meth:`OkxDemoClient._finalize_headers`, which
  unconditionally injects ``x-simulated-trading: 1``. There is no second
  header-building path and no flag to disable the demo switch.
* :class:`LiveEnvironmentNegativeControlProbe` — a deliberately crippled
  read-only probe used by the demo guard. It sends the credentials once,
  **without** the demo header, to one read-only endpoint, and **success is
  treated as a failure** by its caller: a key that authenticates against the
  live environment is not demo-scoped, and the system refuses to trade with
  it.

There is no order, transfer, withdrawal, or deposit code anywhere except the
order methods on :class:`OkxDemoClient`, which cannot point at a live host and
cannot omit the demo header.
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
    ENVIRONMENT_MISMATCH_CODE,
    NEGATIVE_CONTROL_PATH,
    SIMULATED_TRADING_HEADER,
    SIMULATED_TRADING_VALUE,
    Paths,
    is_allowed_authenticated_host,
    to_okx_bar,
)
from .models import (
    AccountConfig,
    Candle,
    ExchangePosition,
    Execution,
    FeeRates,
    FundingRate,
    InstrumentSpec,
    InstType,
    LeverageInfo,
    OpenOrder,
    OrderRequest,
    OrderResult,
    Ticker,
    WalletBalance,
)
from .signing import build_query_string, okx_timestamp, serialize_body, sign_request

log = get_logger(__name__)

# OKX error codes that indicate a transient condition worth retrying.
RETRYABLE_CODES = frozenset({50004, 50011, 50013, 50026})
# Timestamp drift/expiry — triggers a clock re-sync before the retry.
TIMESTAMP_ERROR_CODES = frozenset({50102})
# Authentication failures (invalid key / signature / passphrase).
AUTH_FAILURE_CODES = frozenset({50111, 50113, 50119, 50100, 50103, 50104, 50105})

CANDLES_MAX_LIMIT = 300       # documented maximum for /api/v5/market/candles
HISTORY_CANDLES_MAX_LIMIT = 100


class OkxDemoClient:
    """Async REST client bound to the OKX EEA host, demo environment."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
        base_url: str = DEMO_REST_HOST,
        timeout_seconds: float = 15.0,
        max_retries: int = 4,
        backoff_base_seconds: float = 0.75,
    ) -> None:
        normalized = base_url.rstrip("/")
        if not is_allowed_authenticated_host(normalized):
            # The structural guarantee. Raised before any network activity.
            raise MainnetRejectedError(
                f"refusing to construct an exchange client for host {normalized!r}. "
                f"Only the OKX EEA demo host is permitted: {sorted(ALLOWED_DEMO_HOSTS)}. "
                "This system has no real-money trading mode."
            )
        self.base_url = normalized
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "btc-adaptive-bot/2.0 (okx demo research)"},
        )
        # Measured (server_time - local_time); applied to every signed request
        # so a drifting local clock does not produce timestamp rejections. The
        # *magnitude* of the drift is also a safety signal: see clock_drift_ms.
        self._clock_offset_ms = 0
        self._clock_synced = False
        self._consecutive_errors = 0

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret and self._passphrase)

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    @property
    def clock_offset_ms(self) -> int:
        return self._clock_offset_ms

    @property
    def clock_synced(self) -> bool:
        return self._clock_synced

    def clock_drift_exceeds(self, max_drift_ms: int) -> bool:
        """Whether the measured local↔server clock drift exceeds the budget.

        The caller (circuit breakers / orchestrator) pauses authenticated
        trading when this is True — a machine with a wandering clock cannot be
        trusted to stamp orders.
        """
        return self._clock_synced and abs(self._clock_offset_ms) > max_drift_ms

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OkxDemoClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # --- request plumbing ------------------------------------------------

    def _finalize_headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        """THE single choke point for request headers.

        Every request this client emits — public or authenticated — receives
        its headers from here, and this method unconditionally sets the demo
        environment switch. Removing or bypassing this is the one edit that
        could point the system at the live environment, which is why the
        safety audit greps for exactly this pattern and the demo guard
        verifies it at runtime via :meth:`demo_header_enforced`.
        """
        final = dict(headers or {})
        final[SIMULATED_TRADING_HEADER] = SIMULATED_TRADING_VALUE
        return final

    def demo_header_enforced(self) -> bool:
        """Runtime self-check used by the demo guard (signal 2).

        Builds headers through the same code path every request uses and
        asserts the demo switch is present and correct.
        """
        built = self._finalize_headers({"Content-Type": "application/json"})
        return built.get(SIMULATED_TRADING_HEADER) == SIMULATED_TRADING_VALUE

    def _timestamp(self) -> str:
        return okx_timestamp(now_ms_value=now_ms() + self._clock_offset_ms)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | list[Any] | None = None,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        """Send one v5 request with retries and error mapping."""
        if authenticated and not self.has_credentials:
            raise ApiError(-1, "authenticated request attempted without credentials", path)

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                if authenticated:
                    assert self._api_key and self._api_secret and self._passphrase  # guarded
                    signed = sign_request(
                        api_key=self._api_key,
                        api_secret=self._api_secret,
                        passphrase=self._passphrase,
                        method=method,
                        path=path,
                        timestamp=self._timestamp(),
                        params=params,
                        body=body,
                    )
                    url = f"{path}?{signed.query_string}" if signed.query_string else path
                    response = await self._client.request(
                        method,
                        url,
                        headers=self._finalize_headers(signed.headers),
                        content=signed.body,
                    )
                else:
                    query = build_query_string(params)
                    url = f"{path}?{query}" if query else path
                    content = serialize_body(body)
                    headers = {"Content-Type": "application/json"} if content else {}
                    response = await self._client.request(
                        method, url, headers=self._finalize_headers(headers), content=content
                    )

                if response.status_code == 429:
                    self._consecutive_errors += 1
                    raise RateLimitError(f"rate limited on {path}")
                # OKX returns auth errors with HTTP 401 and a JSON body carrying
                # the specific code — parse the body rather than failing on the
                # status, so the error message names the actual cause.
                if response.status_code not in (200, 401):
                    if response.status_code == 403:
                        self._consecutive_errors += 1
                        raise TransportError(_transport_hint_403(path))
                    response.raise_for_status()
                payload = response.json()

            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = TransportError(_transport_hint(exc, path))
                self._consecutive_errors += 1
            except RateLimitError as exc:
                last_error = exc
            else:
                code = int(payload.get("code", -1))
                if code == 0:
                    self._consecutive_errors = 0
                    return payload
                if code in TIMESTAMP_ERROR_CODES and attempt < self._max_retries:
                    # Our timestamp was rejected: re-measure the drift once and
                    # retry. Persistent drift is caught by clock_drift_exceeds.
                    log.warning("OKX", "Timestamp rejected (50102) — re-syncing clock and retrying")
                    try:
                        await self.sync_clock()
                    except (ApiError, TransportError, RateLimitError):
                        pass
                    last_error = ApiError(code, str(payload.get("msg", "")), path)
                elif code in RETRYABLE_CODES and attempt < self._max_retries:
                    last_error = ApiError(code, str(payload.get("msg", "")), path)
                else:
                    self._consecutive_errors += 1
                    raise ApiError(code, str(payload.get("msg", "")), path)

            if attempt < self._max_retries:
                await asyncio.sleep(self._backoff_base * (2**attempt))

        assert last_error is not None
        raise last_error

    @staticmethod
    def _data(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return payload.get("data", []) or []

    # --- public market data ----------------------------------------------

    async def sync_clock(self) -> int:
        """Measure and store the offset between OKX's clock and ours."""
        local_before = now_ms()
        payload = await self._request("GET", Paths.SERVER_TIME)
        local_after = now_ms()
        data = self._data(payload)
        server_ms = int(data[0]["ts"]) if data and data[0].get("ts") else 0
        if server_ms <= 0:
            return self._clock_offset_ms
        # Compensate for round-trip: assume the server timestamp was taken midway.
        self._clock_offset_ms = server_ms - (local_before + local_after) // 2
        self._clock_synced = True
        if abs(self._clock_offset_ms) > 1000:
            log.warning(
                "OKX",
                f"Local clock differs from OKX by {self._clock_offset_ms}ms — compensating. "
                "Consider enabling NTP time sync.",
                offset_ms=self._clock_offset_ms,
            )
        return self._clock_offset_ms

    async def get_instruments(
        self, inst_type: InstType = InstType.SWAP, *, inst_id: str | None = None
    ) -> list[InstrumentSpec]:
        payload = await self._request(
            "GET",
            Paths.INSTRUMENTS,
            params={"instType": inst_type.value, "instId": inst_id},
        )
        return [InstrumentSpec.from_response(item) for item in self._data(payload)]

    async def get_klines(
        self,
        inst_id: str,
        interval: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = CANDLES_MAX_LIMIT,
    ) -> list[Candle]:
        """Fetch candles with ``open_ms`` in ``[start_ms, end_ms]``, oldest first.

        OKX pages *backwards*: ``after`` returns rows strictly older than the
        given ts. The recent endpoint only holds a bounded window, so when a
        request reaches further back and comes home empty, the deep-history
        endpoint is tried automatically. The newest row may be an *unclosed*
        candle (``confirm == "0"``); deciding what is safe to use is the
        caller's job via :meth:`Candle.is_closed`.
        """
        bar = to_okx_bar(interval)
        params: dict[str, Any] = {
            "instId": inst_id,
            "bar": bar,
            # `after` is exclusive: +1 keeps the candle opening exactly at end_ms.
            "after": (end_ms + 1) if end_ms is not None else None,
            "limit": min(limit, CANDLES_MAX_LIMIT),
        }
        payload = await self._request("GET", Paths.CANDLES, params=params)
        rows = payload.get("data", []) or []

        if not rows and end_ms is not None:
            # Beyond the recent window — fall back to deep history.
            params["limit"] = min(limit, HISTORY_CANDLES_MAX_LIMIT)
            payload = await self._request("GET", Paths.HISTORY_CANDLES, params=params)
            rows = payload.get("data", []) or []

        candles = [Candle.from_okx_row(row, interval) for row in rows]
        if start_ms is not None:
            candles = [c for c in candles if c.open_ms >= start_ms]
        candles.sort(key=lambda c: c.open_ms)
        return candles

    async def get_ticker(self, inst_id: str) -> Ticker:
        payload = await self._request("GET", Paths.TICKER, params={"instId": inst_id})
        data = self._data(payload)
        if not data:
            raise ApiError(-1, f"no ticker returned for {inst_id}", Paths.TICKER)
        item = data[0]
        return Ticker.from_response(item, ts_ms=int(item.get("ts") or now_ms()))

    async def get_orderbook(self, inst_id: str, *, depth: int = 50) -> dict[str, Any]:
        payload = await self._request(
            "GET", Paths.ORDERBOOK, params={"instId": inst_id, "sz": depth}
        )
        data = self._data(payload)
        return data[0] if data else {}

    async def get_funding_rate(self, inst_id: str) -> FundingRate:
        payload = await self._request("GET", Paths.FUNDING_RATE, params={"instId": inst_id})
        data = self._data(payload)
        if not data:
            raise ApiError(-1, f"no funding rate returned for {inst_id}", Paths.FUNDING_RATE)
        return FundingRate.from_response(data[0])

    async def get_mark_price(self, inst_id: str) -> float:
        payload = await self._request(
            "GET", Paths.MARK_PRICE, params={"instType": InstType.SWAP.value, "instId": inst_id}
        )
        data = self._data(payload)
        return float(data[0].get("markPx") or 0.0) if data else 0.0

    # --- authenticated: account ------------------------------------------

    async def get_account_config(self) -> AccountConfig:
        payload = await self._request("GET", Paths.ACCOUNT_CONFIG, authenticated=True)
        data = self._data(payload)
        if not data:
            raise ApiError(-1, "account config response was empty", Paths.ACCOUNT_CONFIG)
        return AccountConfig.from_response(data[0])

    async def get_wallet_balance(self) -> WalletBalance:
        payload = await self._request("GET", Paths.BALANCE, authenticated=True)
        data = self._data(payload)
        if not data:
            raise ApiError(-1, "balance response contained no accounts", Paths.BALANCE)
        return WalletBalance.from_response(data[0], ts_ms=int(data[0].get("uTime") or now_ms()))

    async def get_positions(self, inst_id: str | None = None) -> list[ExchangePosition]:
        payload = await self._request(
            "GET",
            Paths.POSITIONS,
            params={"instType": InstType.SWAP.value, "instId": inst_id},
            authenticated=True,
        )
        return [ExchangePosition.from_response(item) for item in self._data(payload)]

    async def set_leverage(
        self, inst_id: str, leverage: str, *, mgn_mode: str, pos_side: str | None = None
    ) -> dict[str, Any]:
        """Request a leverage setting. Callers must confirm via get_leverage_info —
        set-without-confirm is never trusted before an entry order."""
        payload = await self._request(
            "POST",
            Paths.SET_LEVERAGE,
            body={
                "instId": inst_id,
                "lever": leverage,
                "mgnMode": mgn_mode,
                "posSide": pos_side,
            },
            authenticated=True,
        )
        data = self._data(payload)
        return data[0] if data else {}

    async def get_leverage_info(self, inst_id: str, *, mgn_mode: str) -> list[LeverageInfo]:
        payload = await self._request(
            "GET",
            Paths.LEVERAGE_INFO,
            params={"instId": inst_id, "mgnMode": mgn_mode},
            authenticated=True,
        )
        return [LeverageInfo.from_response(item) for item in self._data(payload)]

    async def get_max_size(self, inst_id: str, *, td_mode: str) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            Paths.MAX_SIZE,
            params={"instId": inst_id, "tdMode": td_mode},
            authenticated=True,
        )
        data = self._data(payload)
        return data[0] if data else {}

    async def get_fee_rates(self, inst_id: str) -> FeeRates:
        payload = await self._request(
            "GET",
            Paths.TRADE_FEE,
            params={"instType": InstType.SWAP.value, "instId": inst_id},
            authenticated=True,
        )
        data = self._data(payload)
        if not data:
            raise ApiError(-1, "trade-fee response was empty", Paths.TRADE_FEE)
        return FeeRates.from_response(data[0])

    async def get_funding_bills(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Funding-fee bills (bill type 8) — realised funding paid/received."""
        payload = await self._request(
            "GET",
            Paths.BILLS,
            params={"instType": InstType.SWAP.value, "type": "8", "limit": limit},
            authenticated=True,
        )
        return self._data(payload)

    # --- authenticated: trading ------------------------------------------

    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Submit an order to the demo account.

        Callers must have passed the full validation pipeline first; this
        method deliberately performs no sizing or leverage logic of its own.
        OKX carries per-order results inside ``data`` with their own
        ``sCode``/``sMsg`` — both envelope levels are checked.
        """
        try:
            payload = await self._request(
                "POST", Paths.ORDER, body=request.to_payload(), authenticated=True
            )
        except ApiError as exc:
            # Outer code 1 = all orders failed; the useful reason is per-item.
            raise exc
        data = self._data(payload)
        item = data[0] if data else {}
        s_code = int(item.get("sCode") or 0)
        if s_code != 0:
            raise ApiError(s_code, str(item.get("sMsg", "")), Paths.ORDER)
        return OrderResult(
            client_order_id=item.get("clOrdId", request.client_order_id),
            exchange_order_id=item.get("ordId", ""),
            accepted=True,
            s_code=s_code,
            s_msg=str(item.get("sMsg", "")),
            raw=payload,
        )

    async def cancel_order(
        self,
        inst_id: str,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if not order_id and not client_order_id:
            raise ApiError(-1, "cancel requires order_id or client_order_id", Paths.CANCEL_ORDER)
        payload = await self._request(
            "POST",
            Paths.CANCEL_ORDER,
            body={"instId": inst_id, "ordId": order_id, "clOrdId": client_order_id},
            authenticated=True,
        )
        data = self._data(payload)
        item = data[0] if data else {}
        s_code = int(item.get("sCode") or 0)
        if s_code != 0:
            raise ApiError(s_code, str(item.get("sMsg", "")), Paths.CANCEL_ORDER)
        return item

    async def cancel_all(self, inst_id: str) -> dict[str, Any]:
        """Cancel every pending order on ``inst_id``.

        OKX has no single cancel-all call; pending orders are listed and then
        cancelled in documented batches of up to 20.
        """
        pending = await self.get_open_orders(inst_id)
        cancelled = 0
        failures: list[str] = []
        for start in range(0, len(pending), 20):
            batch = pending[start : start + 20]
            payload = await self._request(
                "POST",
                Paths.CANCEL_BATCH_ORDERS,
                body=[{"instId": inst_id, "ordId": order.order_id} for order in batch],
                authenticated=True,
            )
            for item in self._data(payload):
                if int(item.get("sCode") or 0) == 0:
                    cancelled += 1
                else:
                    failures.append(f"{item.get('ordId')}: {item.get('sMsg')}")
        if failures:
            log.warning("OKX", f"cancel_all: {len(failures)} cancellations failed: {failures[:3]}")
        return {"cancelled": cancelled, "failed": failures}

    async def get_open_orders(self, inst_id: str) -> list[OpenOrder]:
        payload = await self._request(
            "GET",
            Paths.ORDERS_PENDING,
            params={"instType": InstType.SWAP.value, "instId": inst_id},
            authenticated=True,
        )
        return [OpenOrder.from_response(item) for item in self._data(payload)]

    async def get_order_history(self, inst_id: str, *, limit: int = 50) -> list[OpenOrder]:
        payload = await self._request(
            "GET",
            Paths.ORDERS_HISTORY,
            params={"instType": InstType.SWAP.value, "instId": inst_id, "limit": limit},
            authenticated=True,
        )
        return [OpenOrder.from_response(item) for item in self._data(payload)]

    async def get_executions(
        self, inst_id: str, *, limit: int = 100, start_ms: int | None = None
    ) -> list[Execution]:
        payload = await self._request(
            "GET",
            Paths.FILLS,
            params={
                "instType": InstType.SWAP.value,
                "instId": inst_id,
                "limit": limit,
                "begin": start_ms,
            },
            authenticated=True,
        )
        return [Execution.from_response(item) for item in self._data(payload)]


class LiveEnvironmentNegativeControlProbe:
    """Read-only probe whose job is to *fail*.

    Sends the supplied credentials once, **without** the ``x-simulated-trading``
    header, to a single read-only endpoint on the EEA host. A demo-scoped key
    must be rejected with OKX error 50101 ("APIKey does not match current
    environment"). If the call *succeeds*, the key can act on the live
    environment and the demo guard hard-refuses to trade with it.

    This class has no order method, no transfer method, and no way to send
    anything other than the one GET below. It is a safety assertion, not a
    trading path — and it is the only place in the codebase that deliberately
    builds a request without the demo header.
    """

    __slots__ = ("_api_key", "_api_secret", "_passphrase", "_timeout")

    def __init__(
        self, api_key: str, api_secret: str, passphrase: str, *, timeout_seconds: float = 10.0
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._timeout = timeout_seconds

    async def credentials_are_rejected(self) -> tuple[bool, str]:
        """``(rejected, detail)`` — ``rejected=True`` is the safe outcome."""
        signed = sign_request(
            api_key=self._api_key,
            api_secret=self._api_secret,
            passphrase=self._passphrase,
            method="GET",
            path=NEGATIVE_CONTROL_PATH,
            timestamp=okx_timestamp(now_ms_value=now_ms()),
            params=None,
        )
        url = f"{DEMO_REST_HOST}{NEGATIVE_CONTROL_PATH}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
                # NOTE: deliberately no x-simulated-trading header — that IS the test.
                response = await client.get(url, headers=signed.headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # Could not reach the host. We cannot assert the key is demo-scoped,
            # so this is inconclusive — and inconclusive means "do not trade".
            return (False, f"negative control inconclusive: {type(exc).__name__}: {exc}")

        try:
            payload = response.json()
        except ValueError:
            return (
                False,
                f"negative control inconclusive: unparseable response ({response.status_code})",
            )

        code = int(payload.get("code", -1))
        if code == 0:
            return (
                False,
                "THESE CREDENTIALS AUTHENTICATE ON THE OKX LIVE ENVIRONMENT. They are not "
                "demo-scoped keys. Create an API key from within OKX's Demo Trading area instead.",
            )
        if code == ENVIRONMENT_MISMATCH_CODE:
            return (
                True,
                f"live environment rejected the key as expected (code={code}: environment mismatch)",
            )
        if code == 50119:
            # "API key doesn't exist" — the key genuinely does not exist in the
            # live environment, which equally proves it cannot act there.
            return (True, f"live environment does not know this key (code={code})")
        return (
            False,
            f"negative control inconclusive: unexpected code={code} ({payload.get('msg')})",
        )


def _transport_hint_403(path: str) -> str:
    host = DEMO_REST_HOST.removeprefix("https://")
    return (
        f"OKX returned HTTP 403 for {path}.\n"
        "  A 403 while reaching OKX usually means one of:\n"
        f"   1. You are behind an HTTP proxy or firewall that blocks {host}\n"
        "      (check HTTPS_PROXY / HTTP_PROXY in your shell).\n"
        "   2. Your IP is in a region OKX refuses to serve.\n"
        "   3. Your account is not eligible to use the platform from this location.\n"
        "  This system does not attempt to work around any of these."
    )


def _transport_hint(exc: Exception, path: str) -> str:
    """Turn a transport failure into something the operator can act on."""
    detail = f"{type(exc).__name__} on {path}: {exc}"
    if "403" in str(exc) or isinstance(exc, httpx.ProxyError):
        return f"{detail}\n{_transport_hint_403(path)}"
    return detail
