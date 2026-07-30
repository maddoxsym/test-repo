"""OKX API v5 REST client — region-pinned, demo environment only.

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

The region (Global/UAE, EEA, US) comes from a
:class:`~btcbot.exchange.endpoints.DemoProfile` chosen by ``exchange.region``.
Every entry in that registry is a demo profile. Every OKX region serves demo
and live from the same REST host, so the host is *not* what keeps this on
demo — the unconditional header is, backed by the negative control.

There is no order, transfer, withdrawal, or deposit code anywhere except the
order methods on :class:`OkxDemoClient`, which cannot point at an unrecognised
host and cannot omit the demo header.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, NoReturn

import httpx

from ..utils.errors import (
    ApiError,
    MainnetRejectedError,
    OrderRejectedError,
    RateLimitError,
    TransportError,
)
from ..utils.logging import get_logger
from ..utils.timeutil import now_ms
from .endpoints import (
    ALLOWED_DEMO_HOSTS,
    DEFAULT_PROFILE,
    DEMO_PROFILES,
    ENVIRONMENT_MISMATCH_CODE,
    ITEM_DIAGNOSTIC_FIELDS,
    ITEM_LEVEL_ENVELOPE_CODES,
    KEY_NOT_FOUND_CODE,
    NEGATIVE_CONTROL_PATH,
    ORDER_OPERATION_PATHS,
    SIMULATED_TRADING_HEADER,
    SIMULATED_TRADING_VALUE,
    DemoProfile,
    Paths,
    is_allowed_authenticated_host,
    to_okx_bar,
)
from .models import (
    AccountConfig,
    AlgoOrder,
    Candle,
    ExchangePosition,
    Execution,
    FeeRates,
    FundingRate,
    InstrumentSpec,
    InstType,
    LeverageInfo,
    OpenOrder,
    OrderDetails,
    OrderRequest,
    OrderResult,
    Side,
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
# "Order does not exist" — a real answer from an order lookup, not a failure.
# Reconciliation must be able to ask about an order that was never accepted.
# Deliberately narrow: anything else (parameter errors, auth, instrument
# problems) still raises, because "not found" must not mask a real fault.
ORDER_NOT_FOUND_CODES = frozenset({51603})

CANDLES_MAX_LIMIT = 300       # documented maximum for /api/v5/market/candles
HISTORY_CANDLES_MAX_LIMIT = 100


def _item_code(item: dict[str, Any]) -> int:
    """An order item's ``sCode``. An unreadable code is treated as a failure."""
    raw = item.get("sCode")
    if raw in (None, ""):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _item_diagnostic(item: dict[str, Any]) -> str:
    """A sanitized, human-readable reason for one rejected order operation.

    Built from an allow-list of response fields (``ITEM_DIAGNOSTIC_FIELDS``)
    so nothing unexpected in a future OKX payload can reach a log. Requests —
    which carry the signature and auth headers — are never referenced here.
    """
    reason = str(item.get("sMsg") or "").strip() or "no reason supplied by the exchange"
    extras = [
        f"{field}={item[field]}"
        for field in ITEM_DIAGNOSTIC_FIELDS
        if field not in ("sCode", "sMsg") and item.get(field) not in (None, "")
    ]
    return f"{reason} ({', '.join(extras)})" if extras else reason


def _has_usable_items(payload: dict[str, Any]) -> bool:
    """Whether an order-operation envelope carries per-item results to inspect.

    Without this, an envelope code of 1 or 2 with a missing or malformed data
    array would be silently handed to a parser that has nothing to reject on.
    """
    data = payload.get("data")
    return bool(isinstance(data, list) and data and all(isinstance(i, dict) for i in data))


class OkxDemoClient:
    """Async REST client bound to one OKX region, demo environment."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
        profile: DemoProfile = DEFAULT_PROFILE,
        base_url: str | None = None,
        timeout_seconds: float = 15.0,
        max_retries: int = 4,
        backoff_base_seconds: float = 0.75,
    ) -> None:
        self.profile = profile
        normalized = (base_url or profile.rest_host).rstrip("/")
        if not is_allowed_authenticated_host(normalized):
            # The structural guarantee. Raised before any network activity.
            raise MainnetRejectedError(
                f"refusing to construct an exchange client for host {normalized!r}. "
                f"Only recognised OKX demo hosts are permitted: "
                f"{sorted(ALLOWED_DEMO_HOSTS)}. "
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
        item_level_errors: bool = False,
    ) -> dict[str, Any]:
        """Send one v5 request with retries and error mapping.

        ``item_level_errors`` is permitted only on the trade endpoints listed
        in :data:`ORDER_OPERATION_PATHS`. It lets envelope codes 1 (all failed)
        and 2 (partial) through to the endpoint parser *when a usable data
        array is present*, because on those endpoints the real rejection lives
        in each item's ``sCode``/``sMsg`` and the envelope ``msg`` is only
        "All operations failed". Everything else still raises here.
        """
        if authenticated and not self.has_credentials:
            raise ApiError(-1, "authenticated request attempted without credentials", path)
        if item_level_errors and path not in ORDER_OPERATION_PATHS:
            # A programming error, not a runtime condition: no non-order
            # endpoint may opt out of envelope-level rejection.
            raise ValueError(
                f"item-level error handling is not permitted on {path!r}; "
                f"it is limited to {sorted(ORDER_OPERATION_PATHS)}"
            )

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
                        raise TransportError(_transport_hint_403(path, self.base_url))
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
                if (
                    item_level_errors
                    and code in ITEM_LEVEL_ENVELOPE_CODES
                    and _has_usable_items(payload)
                ):
                    # Not a success: the caller's parser inspects every item and
                    # raises on the real sCode. Handing the payload over is the
                    # only way that detail survives. The error counter is left
                    # alone here and incremented by the parser if it rejects.
                    return payload
                if code in TIMESTAMP_ERROR_CODES and attempt < self._max_retries:
                    # Our timestamp was rejected: re-measure the drift once and
                    # retry. Persistent drift is caught by clock_drift_exceeds.
                    log.warning("OKX", "Timestamp rejected (50102) — re-syncing clock and retrying")
                    with contextlib.suppress(ApiError, TransportError, RateLimitError):
                        await self.sync_clock()
                    last_error = ApiError(code, str(payload.get("msg", "")), path)
                elif code in RETRYABLE_CODES and attempt < self._max_retries:
                    last_error = ApiError(code, str(payload.get("msg", "")), path)
                else:
                    self._consecutive_errors += 1
                    msg = str(payload.get("msg", ""))
                    if code == KEY_NOT_FOUND_CODE:
                        msg = f"{msg}\n{self._region_mismatch_hint()}"
                    raise ApiError(code, msg, path)

            if attempt < self._max_retries:
                await asyncio.sleep(self._backoff_base * (2**attempt))

        assert last_error is not None
        raise last_error

    def _region_mismatch_hint(self) -> str:
        """Explain OKX 50119, which is almost always a *region* mistake.

        An OKX API key is issued by one regional entity and is simply unknown
        to the others, so a Global/UAE demo key asked against the EEA host
        returns "API key doesn't exist" rather than anything that names the
        real cause.
        """
        others = ", ".join(
            f"{name} ({p.rest_host})"
            for name, p in sorted(DEMO_PROFILES.items())
            if name != self.profile.region
        )
        return (
            f"OKX says this API key does not exist on {self.profile.describe()}. "
            "An OKX key belongs to ONE regional entity, so this usually means the key was "
            "created on a different one. Check which OKX site you created the Demo Trading "
            f"key on and set exchange.region in config accordingly — other demo regions: "
            f"{others}. Also confirm the key was created inside Demo Trading, not on the "
            "live account."
        )

    @staticmethod
    def _data(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return payload.get("data", []) or []

    # --- per-item (order operation) result handling -----------------------

    def _raise_item_error(self, item: dict[str, Any], path: str) -> NoReturn:
        """Reject an order operation using its own ``sCode``, not the envelope."""
        self._consecutive_errors += 1
        raise OrderRejectedError(
            _item_code(item),
            str(item.get("sMsg") or "").strip() or "no reason supplied by the exchange",
            path,
            sub_code=str(item.get("subCode") or ""),
            client_order_id=str(item.get("clOrdId") or ""),
            order_id=str(item.get("ordId") or ""),
        )

    def _check_items(
        self, payload: dict[str, Any], path: str, *, expected: int | None = None
    ) -> list[dict[str, Any]]:
        """Validate every item of an order-operation response.

        Returns the items only when *all* of them carry ``sCode == 0``. The
        first failure raises with that item's real code and reason. A missing,
        empty, or non-object data array is itself a failure: without items
        there is nothing to prove the operation was accepted.
        """
        # A clean envelope with nothing in it is still a failure, so the error
        # never reports code 0 — that would read as success.
        envelope_code = int(payload.get("code", -1)) or -1
        items = self._data(payload)
        if not items or not all(isinstance(item, dict) for item in items):
            self._consecutive_errors += 1
            raise ApiError(
                envelope_code,
                f"{payload.get('msg', '')} (no usable data array in the response)".strip(),
                path,
            )
        if expected is not None and len(items) != expected:
            self._consecutive_errors += 1
            raise ApiError(
                envelope_code,
                f"expected {expected} result(s) but the response carried {len(items)}",
                path,
            )
        for item in items:
            if _item_code(item) != 0:
                self._raise_item_error(item, path)
        return items

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

        OKX's trade endpoint is batch-shaped: the envelope ``code`` describes
        the batch (0 all succeeded, 1 all failed, 2 partial) and the actual
        rejection is per item. An envelope of 1 with ``msg="All operations
        failed"`` says nothing useful, so 1 and 2 are passed through to this
        method (see :data:`ORDER_OPERATION_PATHS`) and the order is accepted
        **only** when its item carries ``sCode == 0``.
        """
        payload = await self._request(
            "POST",
            Paths.ORDER,
            body=request.to_payload(),
            authenticated=True,
            item_level_errors=True,
        )
        # Raises on the first non-zero sCode, and on a response that carries no
        # item to check. One order in, exactly one result expected out.
        item = self._check_items(payload, Paths.ORDER, expected=1)[0]
        return OrderResult(
            client_order_id=item.get("clOrdId") or request.client_order_id,
            exchange_order_id=item.get("ordId", ""),
            accepted=True,
            s_code=0,
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
            item_level_errors=True,
        )
        return self._check_items(payload, Paths.CANCEL_ORDER, expected=1)[0]

    async def cancel_all(self, inst_id: str) -> dict[str, Any]:
        """Cancel every pending order on ``inst_id``.

        OKX has no single cancel-all call; pending orders are listed and then
        cancelled in documented batches of up to 20.

        A batch legitimately reports mixed results (envelope code 2), so this
        method counts each item individually and returns both tallies rather
        than raising. It never reports a cancellation it cannot see an
        ``sCode == 0`` for — a batch that comes back without a usable data
        array is counted as entirely failed, not silently as success.
        """
        pending = await self.get_open_orders(inst_id)
        cancelled = 0
        failures: list[str] = []
        for start in range(0, len(pending), 20):
            batch = pending[start : start + 20]
            try:
                payload = await self._request(
                    "POST",
                    Paths.CANCEL_BATCH_ORDERS,
                    body=[{"instId": inst_id, "ordId": order.order_id} for order in batch],
                    authenticated=True,
                    item_level_errors=True,
                )
            except ApiError as exc:
                if exc.ret_code not in ITEM_LEVEL_ENVELOPE_CODES:
                    # Authentication, environment, rate-limit and malformed
                    # responses are not this method's to absorb.
                    raise
                # The batch failed and OKX sent no per-item detail. Record every
                # order in it as failed rather than losing the batch silently.
                failures.extend(f"{order.order_id}: {exc.ret_msg}" for order in batch)
                continue
            items = self._data(payload)
            if not _has_usable_items(payload):
                self._consecutive_errors += 1
                failures.extend(
                    f"{order.order_id}: no result returned "
                    f"(envelope code={payload.get('code')} {payload.get('msg', '')})"
                    for order in batch
                )
                continue
            for item in items:
                if _item_code(item) == 0:
                    cancelled += 1
                else:
                    failures.append(f"{item.get('ordId', '?')}: {_item_diagnostic(item)}")
        if failures:
            self._consecutive_errors += 1
            log.warning("OKX", f"cancel_all: {len(failures)} cancellations failed: {failures[:3]}")
        return {"cancelled": cancelled, "failed": failures}

    async def get_order(
        self,
        inst_id: str,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> OrderDetails | None:
        """Read one order's authoritative state from ``GET /api/v5/trade/order``.

        ``order_id`` (OKX's ``ordId``) is preferred: it is assigned by the
        exchange and is the identifier every other endpoint keys on.
        ``client_order_id`` is a fallback for orders whose ``ordId`` we never
        received — a transport failure mid-submission, for instance.

        Returns ``None`` when OKX says the order does not exist. That is a
        legitimate answer during reconciliation (the order may never have been
        accepted), not an error, so the caller decides what it means.
        """
        if not order_id and not client_order_id:
            raise ApiError(-1, "order lookup requires order_id or client_order_id", Paths.ORDER)
        params: dict[str, Any] = {"instId": inst_id}
        if order_id:
            params["ordId"] = order_id
        else:
            params["clOrdId"] = client_order_id
        try:
            payload = await self._request("GET", Paths.ORDER, params=params, authenticated=True)
        except ApiError as exc:
            if exc.ret_code in ORDER_NOT_FOUND_CODES:
                return None
            raise
        data = self._data(payload)
        return OrderDetails.from_response(data[0]) if data else None

    # --- authenticated: exchange-side protection (algo orders) -----------

    async def place_algo_order(
        self,
        inst_id: str,
        *,
        side: Side,
        size: str,
        td_mode: str = "isolated",
        pos_side: str | None = None,
        sl_trigger_price: str | None = None,
        tp_trigger_price: str | None = None,
        reduce_only: bool = True,
        client_algo_id: str | None = None,
    ) -> AlgoOrder:
        """Place a conditional / OCO order that protects an open position.

        This is the only way to obtain protection that survives this process.
        Both triggers together produce an OCO — OKX cancels the remaining leg
        when one fires, which is what stops a filled take-profit from leaving
        an orphaned stop behind.

        Orders are ``reduceOnly`` by default: protection may only ever close a
        position, never open or increase one.
        """
        if not sl_trigger_price and not tp_trigger_price:
            raise ApiError(
                -1, "an algo order needs a stop-loss or take-profit trigger", Paths.ORDER_ALGO
            )
        # Both legs -> OCO (one cancels the other). One leg -> conditional.
        ord_type = "oco" if (sl_trigger_price and tp_trigger_price) else "conditional"
        body: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side.value,
            "ordType": ord_type,
            "sz": size,
            "reduceOnly": reduce_only,
        }
        if pos_side:
            body["posSide"] = pos_side
        if client_algo_id:
            body["algoClOrdId"] = client_algo_id
        if sl_trigger_price:
            body["slTriggerPx"] = sl_trigger_price
            body["slOrdPx"] = "-1"        # -1 = close at market when triggered
        if tp_trigger_price:
            body["tpTriggerPx"] = tp_trigger_price
            body["tpOrdPx"] = "-1"
        payload = await self._request(
            "POST", Paths.ORDER_ALGO, body=body, authenticated=True, item_level_errors=True
        )
        item = self._check_items(payload, Paths.ORDER_ALGO, expected=1)[0]
        return AlgoOrder.from_response({**body, **item})

    async def get_algo_orders(
        self, inst_id: str, *, order_type: str = "oco"
    ) -> list[AlgoOrder]:
        """Pending algo orders of one type — the verification read.

        OKX requires ``ordType`` on this endpoint and does not accept a
        wildcard, so callers that want the full picture ask for each type.
        """
        payload = await self._request(
            "GET",
            Paths.ORDERS_ALGO_PENDING,
            params={"instId": inst_id, "ordType": order_type},
            authenticated=True,
        )
        return [AlgoOrder.from_response(item) for item in self._data(payload)]

    async def get_protective_orders(self, inst_id: str) -> list[AlgoOrder]:
        """Every pending order that could protect ``inst_id``, all types.

        A failure on one type is not allowed to look like "no protection" —
        that would send the caller down the emergency-close path on a transient
        read error — so read errors propagate.
        """
        found: dict[str, AlgoOrder] = {}
        for order_type in ("oco", "conditional", "trigger", "move_order_stop"):
            for order in await self.get_algo_orders(inst_id, order_type=order_type):
                if order.algo_id:
                    found[order.algo_id] = order
        return list(found.values())

    async def cancel_algo_orders(
        self, inst_id: str, algo_ids: list[str], *, order_type: str = "oco"
    ) -> list[dict[str, Any]]:
        """Cancel protective orders by algoId (used when replacing them)."""
        if not algo_ids:
            return []
        body = [{"instId": inst_id, "algoId": algo_id} for algo_id in algo_ids]
        payload = await self._request(
            "POST", Paths.CANCEL_ALGOS, body=body, authenticated=True, item_level_errors=True
        )
        return self._data(payload)

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
    header, to a single read-only endpoint on the active region's host. A
    demo-scoped key
    must be rejected with OKX error 50101 ("APIKey does not match current
    environment"). If the call *succeeds*, the key can act on the live
    environment and the demo guard hard-refuses to trade with it.

    This class has no order method, no transfer method, and no way to send
    anything other than the one GET below. It is a safety assertion, not a
    trading path — and it is the only place in the codebase that deliberately
    builds a request without the demo header.
    """

    __slots__ = ("_api_key", "_api_secret", "_passphrase", "_profile", "_timeout")

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        *,
        profile: DemoProfile = DEFAULT_PROFILE,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._profile = profile
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
        url = f"{self._profile.rest_host}{NEGATIVE_CONTROL_PATH}"
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
        if code == KEY_NOT_FOUND_CODE:
            # "API key doesn't exist" — the key genuinely does not exist in the
            # live environment, which equally proves it cannot act there.
            return (True, f"live environment does not know this key (code={code})")
        return (
            False,
            f"negative control inconclusive: unexpected code={code} ({payload.get('msg')})",
        )


def _transport_hint_403(path: str, host: str = DEFAULT_PROFILE.rest_host) -> str:
    host = host.removeprefix("https://")
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
