"""WebSocket clients: public market data, business (candles), private account.

All three connect to the demo WS hosts of the configured region, pinned in
``endpoints.py`` — unlike REST, OKX separates demo and live by hostname
(``wspap`` vs ``ws``, ``wseeapap`` vs ``wseea``, ``wsuspap`` vs ``wsus``), and
the allow-list check is exact-string, never substring.

OKX WS protocol specifics implemented here:

* Subscriptions are argument objects: ``{"op": "subscribe", "args":
  [{"channel": "tickers", "instId": …}]}``.
* Candlestick channels (``candle1m`` …) live on the **business** endpoint.
* Keepalive is application-level: the client sends the literal text ``ping``
  and the server answers ``pong`` (raw text, not JSON). Protocol-level pings
  are disabled.
* The private stream authenticates with an ``op: login`` frame whose
  signature uses epoch-seconds (see ``signing.sign_ws_login``), then
  subscribes to ``orders`` / ``account`` / ``positions``.

Shared machinery in :class:`_ReconnectingSocket` handles the operational
realities of a 14-day unattended run: heartbeats, exponential backoff with
jitter, resubscription after reconnect, and per-channel freshness tracking.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from ..utils.errors import MainnetRejectedError
from ..utils.logging import get_logger
from ..utils.timeutil import now_ms
from .endpoints import (
    DEFAULT_PROFILE,
    DemoProfile,
    candle_channel,
    from_okx_bar,
    is_allowed_ws_url,
)
from .models import Candle, Ticker
from .signing import sign_ws_login

log = get_logger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]

# Send an application-level ping after this much idle time; OKX disconnects
# clients that stay silent for 30 s.
PING_IDLE_SECONDS = 20.0


@dataclass(slots=True)
class StreamHealth:
    """Freshness and integrity bookkeeping for one channel."""

    topic: str
    last_updated_ms: int = 0
    message_count: int = 0
    duplicate_count: int = 0
    sequence_gaps: int = 0
    last_sequence: int | None = None

    def age_seconds(self, *, now: int | None = None) -> float:
        if self.last_updated_ms == 0:
            return float("inf")
        return ((now or now_ms()) - self.last_updated_ms) / 1000.0

    def is_stale(self, budget_seconds: float, *, now: int | None = None) -> bool:
        return self.age_seconds(now=now) > budget_seconds


class _ReconnectingSocket:
    """A websocket connection that maintains itself (OKX text-ping protocol)."""

    def __init__(
        self,
        url: str,
        *,
        name: str,
        ping_interval: float = PING_IDLE_SECONDS,
        max_backoff: float = 60.0,
        on_connect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not is_allowed_ws_url(url):
            # Same structural guarantee as the REST client: a socket to a
            # non-demo host cannot even be constructed.
            raise MainnetRejectedError(
                f"refusing to open a websocket to {url!r} — not a recognised OKX "
                "demo endpoint. Live streams are rejected by construction."
            )
        self.url = url
        self.name = name
        self._ping_interval = ping_interval
        self._max_backoff = max_backoff
        self._on_connect = on_connect
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._running = False
        self._connected = asyncio.Event()
        self._handlers: list[MessageHandler] = []
        self._last_rx_ms = 0
        self.reconnects = 0
        self.last_error: str | None = None
        self.connected_since_ms: int | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def add_handler(self, handler: MessageHandler) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name=f"ws-{self.name}")

    async def stop(self) -> None:
        self._running = False
        for task in (self._ping_task, self._task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._ping_task = None
        self._task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        self._connected.clear()

    async def send(self, payload: dict[str, Any]) -> None:
        if self._ws is None or not self.is_connected:
            raise ConnectionError(f"{self.name} websocket is not connected")
        await self._ws.send(json.dumps(payload))

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def _ping_loop(self) -> None:
        """OKX keepalive: literal text ``ping`` when the line has been idle."""
        while True:
            await asyncio.sleep(self._ping_interval / 2)
            if self._ws is None or not self.is_connected:
                continue
            idle = (now_ms() - self._last_rx_ms) / 1000.0
            if idle >= self._ping_interval / 2:
                with contextlib.suppress(Exception):
                    await self._ws.send("ping")

    async def _run(self) -> None:
        attempt = 0
        while self._running:
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=None,  # OKX uses text ping/pong, not protocol pings
                    close_timeout=5,
                    max_queue=512,
                ) as socket:
                    self._ws = socket
                    self._connected.set()
                    self.connected_since_ms = now_ms()
                    self._last_rx_ms = now_ms()
                    attempt = 0
                    self.last_error = None
                    log.info("DATA", f"{self.name} websocket connected")

                    self._ping_task = asyncio.create_task(
                        self._ping_loop(), name=f"ws-ping-{self.name}"
                    )
                    try:
                        if self._on_connect is not None:
                            await self._on_connect()

                        async for raw in socket:
                            self._last_rx_ms = now_ms()
                            if raw == "pong":
                                continue
                            try:
                                message = json.loads(raw)
                            except json.JSONDecodeError:
                                log.debug("DATA", f"{self.name}: dropped non-JSON frame")
                                continue
                            for handler in self._handlers:
                                try:
                                    await handler(message)
                                except Exception as exc:  # noqa: BLE001 - one bad handler must not kill the feed
                                    log.error(
                                        "DATA",
                                        f"{self.name} handler error: {type(exc).__name__}: {exc}",
                                        exc_info=True,
                                    )
                    finally:
                        if self._ping_task is not None:
                            self._ping_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await self._ping_task
                            self._ping_task = None

            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, WebSocketException, OSError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - keep the reconnect loop alive
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.error("DATA", f"{self.name} unexpected websocket error: {exc}", exc_info=True)
            finally:
                self._connected.clear()
                self._ws = None
                self.connected_since_ms = None

            if not self._running:
                break

            self.reconnects += 1
            # Exponential backoff with jitter — jitter matters because several
            # sockets reconnect together after a network drop.
            delay = min(self._max_backoff, (2**attempt)) * (0.5 + random.random() / 2)
            attempt = min(attempt + 1, 8)
            log.warning(
                "DATA",
                f"{self.name} websocket disconnected ({self.last_error}); "
                f"reconnecting in {delay:.1f}s",
                reconnects=self.reconnects,
            )
            await asyncio.sleep(delay)


def _event_of(message: dict[str, Any]) -> str | None:
    event = message.get("event")
    return event if isinstance(event, str) else None


def _channel_of(message: dict[str, Any]) -> str | None:
    arg = message.get("arg")
    if isinstance(arg, dict):
        channel = arg.get("channel")
        return channel if isinstance(channel, str) else None
    return None


class PublicMarketStream:
    """Public market data for one instrument.

    Runs **two** demo sockets: the public endpoint (tickers, books, trades,
    funding rate, mark price, open interest) and the business endpoint, which
    is where OKX serves candlestick channels.
    """

    def __init__(
        self,
        *,
        inst_id: str,
        timeframes: list[str],
        orderbook_depth: int = 50,
        ping_interval: float = PING_IDLE_SECONDS,
        max_backoff: float = 60.0,
        profile: DemoProfile = DEFAULT_PROFILE,
    ) -> None:
        self.inst_id = inst_id
        self.timeframes = timeframes
        self.orderbook_depth = orderbook_depth
        self.profile = profile
        self.health: dict[str, StreamHealth] = {}
        self._public = _ReconnectingSocket(
            profile.ws_public,
            name="public",
            ping_interval=ping_interval,
            max_backoff=max_backoff,
            on_connect=self._subscribe_public,
        )
        self._business = _ReconnectingSocket(
            profile.ws_business,
            name="business",
            ping_interval=ping_interval,
            max_backoff=max_backoff,
            on_connect=self._subscribe_business,
        )
        self._public.add_handler(self._dispatch)
        self._business.add_handler(self._dispatch)
        self._candle_handlers: list[Callable[[str, Candle], Awaitable[None]]] = []
        self._ticker_handlers: list[Callable[[Ticker], Awaitable[None]]] = []
        self._orderbook_handlers: list[Callable[[dict[str, Any], str], Awaitable[None]]] = []
        self._trade_handlers: list[Callable[[list[dict[str, Any]]], Awaitable[None]]] = []
        self._funding_handlers: list[Callable[[dict[str, Any]], Awaitable[None]]] = []
        self._seen_candle_bars: dict[str, int] = {}

    @property
    def is_connected(self) -> bool:
        return self._public.is_connected and self._business.is_connected

    @property
    def reconnects(self) -> int:
        return self._public.reconnects + self._business.reconnects

    def on_candle(self, handler: Callable[[str, Candle], Awaitable[None]]) -> None:
        self._candle_handlers.append(handler)

    def on_ticker(self, handler: Callable[[Ticker], Awaitable[None]]) -> None:
        self._ticker_handlers.append(handler)

    def on_orderbook(self, handler: Callable[[dict[str, Any], str], Awaitable[None]]) -> None:
        self._orderbook_handlers.append(handler)

    def on_trades(self, handler: Callable[[list[dict[str, Any]]], Awaitable[None]]) -> None:
        self._trade_handlers.append(handler)

    def on_funding(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._funding_handlers.append(handler)

    async def start(self) -> None:
        await self._public.start()
        await self._business.start()

    async def stop(self) -> None:
        await self._public.stop()
        await self._business.stop()

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        ok_public = await self._public.wait_connected(timeout)
        ok_business = await self._business.wait_connected(timeout)
        return ok_public and ok_business

    def public_channels(self) -> list[dict[str, str]]:
        return [
            {"channel": "tickers", "instId": self.inst_id},
            {"channel": "books", "instId": self.inst_id},
            {"channel": "trades", "instId": self.inst_id},
            {"channel": "funding-rate", "instId": self.inst_id},
            {"channel": "mark-price", "instId": self.inst_id},
            # Perp-only: open interest distinguishes new positioning from
            # position closing. Strategies stand down when it is absent.
            {"channel": "open-interest", "instId": self.inst_id},
        ]

    def business_channels(self) -> list[dict[str, str]]:
        return [
            {"channel": candle_channel(tf), "instId": self.inst_id} for tf in self.timeframes
        ]

    async def _subscribe_public(self) -> None:
        """(Re)subscribe on every connect — including after a reconnect."""
        args = self.public_channels()
        await self._public.send({"op": "subscribe", "args": args})
        log.info("DATA", f"Subscribed to {len(args)} public channels for {self.inst_id}")

    async def _subscribe_business(self) -> None:
        args = self.business_channels()
        await self._business.send({"op": "subscribe", "args": args})
        log.info("DATA", f"Subscribed to {len(args)} candle channels for {self.inst_id}")

    async def _dispatch(self, message: dict[str, Any]) -> None:
        event = _event_of(message)
        if event is not None:
            if event == "error":
                log.warning(
                    "DATA",
                    f"Public subscription error: code={message.get('code')} {message.get('msg')}",
                )
            return

        channel = _channel_of(message)
        if channel is None:
            return

        health = self.health.setdefault(channel, StreamHealth(topic=channel))
        health.last_updated_ms = now_ms()
        health.message_count += 1

        data = message.get("data", []) or []
        if channel.startswith("candle"):
            await self._handle_candles(channel, data, health)
        elif channel == "tickers":
            for item in data:
                ticker = Ticker.from_response(item, ts_ms=int(item.get("ts") or now_ms()))
                for handler in self._ticker_handlers:
                    await handler(ticker)
        elif channel == "books":
            await self._handle_orderbook(message, data, health)
        elif channel == "trades":
            for handler in self._trade_handlers:
                await handler(data)
        elif channel in {"funding-rate", "mark-price", "open-interest"}:
            for item in data:
                for handler in self._funding_handlers:
                    await handler({"channel": channel, **item})

    async def _handle_candles(
        self, channel: str, data: list[list[str]], health: StreamHealth
    ) -> None:
        interval = from_okx_bar(channel.removeprefix("candle"))
        for row in data:
            candle = Candle.from_okx_row(row, interval)
            # Duplicate detection: the same closed bar can arrive twice across a
            # reconnect. Only the first confirmation of a bar advances the cursor.
            if candle.confirmed:
                previous = self._seen_candle_bars.get(interval)
                if previous is not None and candle.open_ms <= previous:
                    health.duplicate_count += 1
                    continue
                self._seen_candle_bars[interval] = candle.open_ms
                if previous is not None:
                    from ..utils.timeutil import interval_ms

                    expected = previous + interval_ms(interval)
                    if candle.open_ms > expected:
                        health.sequence_gaps += 1
                        log.warning(
                            "DATA",
                            f"Missing {interval} candle(s) between {previous} and "
                            f"{candle.open_ms} — will be repaired from REST",
                            channel=channel,
                        )
            for handler in self._candle_handlers:
                await handler(interval, candle)

    async def _handle_orderbook(
        self, message: dict[str, Any], data: list[dict[str, Any]], health: StreamHealth
    ) -> None:
        action = message.get("action", "snapshot")
        for book in data:
            # OKX books carry seqId/prevSeqId; a mismatch means our local book
            # may be wrong, so we flag it and wait for the next snapshot.
            seq = book.get("seqId")
            prev_seq = book.get("prevSeqId")
            if isinstance(seq, int):
                if (
                    health.last_sequence is not None
                    and action == "update"
                    and isinstance(prev_seq, int)
                    and prev_seq != health.last_sequence
                ):
                    health.sequence_gaps += 1
                health.last_sequence = seq
            for handler in self._orderbook_handlers:
                await handler(book, action)

    def worst_staleness_seconds(self) -> float:
        """Age of the least-recently-updated subscribed channel."""
        if not self.health:
            return float("inf")
        return max(h.age_seconds() for h in self.health.values())

    def health_report(self) -> dict[str, dict[str, Any]]:
        return {
            topic: {
                "age_seconds": round(h.age_seconds(), 1),
                "messages": h.message_count,
                "duplicates": h.duplicate_count,
                "gaps": h.sequence_gaps,
            }
            for topic, h in self.health.items()
        }


class PrivateAccountStream:
    """Private orders / account / positions stream on the region's demo host."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        passphrase: str,
        ping_interval: float = PING_IDLE_SECONDS,
        max_backoff: float = 60.0,
        profile: DemoProfile = DEFAULT_PROFILE,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self.profile = profile
        self._logged_in = asyncio.Event()
        self.health: dict[str, StreamHealth] = {}
        self._socket = _ReconnectingSocket(
            profile.ws_private,
            name="private",
            ping_interval=ping_interval,
            max_backoff=max_backoff,
            on_connect=self._login,
        )
        self._socket.add_handler(self._dispatch)
        self._order_handlers: list[Callable[[list[dict[str, Any]]], Awaitable[None]]] = []
        self._execution_handlers: list[Callable[[list[dict[str, Any]]], Awaitable[None]]] = []
        self._wallet_handlers: list[Callable[[list[dict[str, Any]]], Awaitable[None]]] = []
        self._position_handlers: list[Callable[[list[dict[str, Any]]], Awaitable[None]]] = []

    @property
    def is_connected(self) -> bool:
        return self._socket.is_connected

    @property
    def reconnects(self) -> int:
        return self._socket.reconnects

    def on_order(self, handler: Callable[[list[dict[str, Any]]], Awaitable[None]]) -> None:
        self._order_handlers.append(handler)

    def on_execution(self, handler: Callable[[list[dict[str, Any]]], Awaitable[None]]) -> None:
        self._execution_handlers.append(handler)

    def on_wallet(self, handler: Callable[[list[dict[str, Any]]], Awaitable[None]]) -> None:
        self._wallet_handlers.append(handler)

    def on_position(self, handler: Callable[[list[dict[str, Any]]], Awaitable[None]]) -> None:
        self._position_handlers.append(handler)

    async def start(self) -> None:
        await self._socket.start()

    async def stop(self) -> None:
        await self._socket.stop()

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        return await self._socket.wait_connected(timeout)

    async def _login(self) -> None:
        """Authenticate, then subscribe once the login event confirms."""
        self._logged_in.clear()
        login_args = sign_ws_login(
            self._api_key,
            self._api_secret,
            self._passphrase,
            epoch_seconds=now_ms() // 1000,
        )
        await self._socket.send({"op": "login", "args": [login_args]})
        # Subscription happens in _dispatch when the login event arrives —
        # OKX rejects subscriptions sent before login completes.

    async def _subscribe(self) -> None:
        channels = [
            {"channel": "orders", "instType": "SWAP"},
            {"channel": "account"},
            {"channel": "positions", "instType": "SWAP"},
        ]
        await self._socket.send({"op": "subscribe", "args": channels})
        log.info("DATA", "Private stream subscribed: orders, account, positions")

    async def _dispatch(self, message: dict[str, Any]) -> None:
        event = _event_of(message)
        if event == "login":
            if str(message.get("code", "")) == "0":
                log.info("DATA", "Private stream authenticated")
                self._logged_in.set()
                await self._subscribe()
            else:
                log.error("DATA", f"Private stream auth failed: {message.get('msg')}")
            return
        if event == "error":
            log.error(
                "DATA", f"Private stream error: code={message.get('code')} {message.get('msg')}"
            )
            return
        if event is not None:
            return

        channel = _channel_of(message)
        if channel is None:
            return

        health = self.health.setdefault(channel, StreamHealth(topic=channel))
        health.last_updated_ms = now_ms()
        health.message_count += 1

        data = message.get("data", []) or []
        if channel == "orders":
            for handler in self._order_handlers:
                await handler(data)
            # OKX delivers fills as fields on the order update; forward the
            # updates that actually contain a fill as execution events.
            fills = [item for item in data if float(item.get("fillSz") or 0.0) > 0.0]
            if fills:
                for handler in self._execution_handlers:
                    await handler(fills)
        elif channel == "account":
            for handler in self._wallet_handlers:
                await handler(data)
        elif channel == "positions":
            for handler in self._position_handlers:
                await handler(data)
