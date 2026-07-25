"""WebSocket clients: public market data and private account streams.

Both share :class:`_ReconnectingSocket`, which handles the operational realities
of a 14-day unattended run: heartbeats, exponential backoff with jitter,
resubscription after reconnect, and per-topic freshness tracking.

Public data comes from the mainnet public stream (Bybit documents that the demo
module has no public stream and that mainnet public data is identical). That
connection is unauthenticated and read-only. Private order/execution/wallet
updates come from the demo private stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from ..utils.logging import get_logger
from ..utils.timeutil import now_ms
from .endpoints import DEMO_WS_PRIVATE, public_ws_url
from .signing import sign_ws_auth

log = get_logger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class StreamHealth:
    """Freshness and integrity bookkeeping for one topic."""

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
    """A websocket connection that maintains itself."""

    def __init__(
        self,
        url: str,
        *,
        name: str,
        ping_interval: float = 20.0,
        max_backoff: float = 60.0,
        on_connect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.url = url
        self.name = name
        self._ping_interval = ping_interval
        self._max_backoff = max_backoff
        self._on_connect = on_connect
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._connected = asyncio.Event()
        self._handlers: list[MessageHandler] = []
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
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
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

    async def _run(self) -> None:
        attempt = 0
        while self._running:
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_interval * 2,
                    close_timeout=5,
                    max_queue=512,
                ) as socket:
                    self._ws = socket
                    self._connected.set()
                    self.connected_since_ms = now_ms()
                    attempt = 0
                    self.last_error = None
                    log.info("DATA", f"{self.name} websocket connected")

                    if self._on_connect is not None:
                        await self._on_connect()

                    async for raw in socket:
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


class PublicMarketStream:
    """Public kline / ticker / orderbook / trade streams (unauthenticated)."""

    def __init__(
        self,
        *,
        category: str,
        symbol: str,
        timeframes: list[str],
        orderbook_depth: int = 50,
        ping_interval: float = 20.0,
        max_backoff: float = 60.0,
    ) -> None:
        self.symbol = symbol
        self.category = category
        self.timeframes = timeframes
        self.orderbook_depth = orderbook_depth
        self.health: dict[str, StreamHealth] = defaultdict(lambda: StreamHealth(topic="unknown"))
        self._socket = _ReconnectingSocket(
            public_ws_url(category),
            name="public",
            ping_interval=ping_interval,
            max_backoff=max_backoff,
            on_connect=self._subscribe,
        )
        self._socket.add_handler(self._dispatch)
        self._kline_handlers: list[Callable[[str, dict[str, Any]], Awaitable[None]]] = []
        self._ticker_handlers: list[Callable[[dict[str, Any]], Awaitable[None]]] = []
        self._orderbook_handlers: list[Callable[[dict[str, Any], str], Awaitable[None]]] = []
        self._trade_handlers: list[Callable[[list[dict[str, Any]]], Awaitable[None]]] = []
        self._seen_kline_bars: dict[str, int] = {}

    @property
    def is_connected(self) -> bool:
        return self._socket.is_connected

    @property
    def reconnects(self) -> int:
        return self._socket.reconnects

    def on_kline(self, handler: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        self._kline_handlers.append(handler)

    def on_ticker(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._ticker_handlers.append(handler)

    def on_orderbook(self, handler: Callable[[dict[str, Any], str], Awaitable[None]]) -> None:
        self._orderbook_handlers.append(handler)

    def on_trades(self, handler: Callable[[list[dict[str, Any]]], Awaitable[None]]) -> None:
        self._trade_handlers.append(handler)

    async def start(self) -> None:
        await self._socket.start()

    async def stop(self) -> None:
        await self._socket.stop()

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        return await self._socket.wait_connected(timeout)

    def topics(self) -> list[str]:
        topics = [f"kline.{tf}.{self.symbol}" for tf in self.timeframes]
        topics.append(f"tickers.{self.symbol}")
        topics.append(f"orderbook.{self.orderbook_depth}.{self.symbol}")
        topics.append(f"publicTrade.{self.symbol}")
        return topics

    async def _subscribe(self) -> None:
        """(Re)subscribe on every connect — including after a reconnect."""
        topics = self.topics()
        await self._socket.send({"op": "subscribe", "args": topics})
        log.info("DATA", f"Subscribed to {len(topics)} public topics for {self.symbol}")

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if message.get("op") in {"subscribe", "pong"} or "success" in message:
            if message.get("success") is False:
                log.warning("DATA", f"Public subscription rejected: {message.get('ret_msg')}")
            return

        topic = message.get("topic")
        if not isinstance(topic, str):
            return

        health = self.health.get(topic)
        if health is None or health.topic == "unknown":
            health = StreamHealth(topic=topic)
            self.health[topic] = health
        health.last_updated_ms = int(message.get("ts") or now_ms())
        health.message_count += 1

        if topic.startswith("kline."):
            await self._handle_kline(topic, message, health)
        elif topic.startswith("tickers."):
            for handler in self._ticker_handlers:
                await handler(message.get("data", {}) or {})
        elif topic.startswith("orderbook."):
            await self._handle_orderbook(message, health)
        elif topic.startswith("publicTrade."):
            for handler in self._trade_handlers:
                await handler(message.get("data", []) or [])

    async def _handle_kline(
        self, topic: str, message: dict[str, Any], health: StreamHealth
    ) -> None:
        interval = topic.split(".")[1]
        for item in message.get("data", []) or []:
            # Duplicate detection: the same closed bar can arrive twice across a
            # reconnect. Only the first confirmation of a bar is forwarded.
            if item.get("confirm"):
                key = f"{interval}:{item.get('start')}"
                previous = self._seen_kline_bars.get(interval)
                bar_start = int(item.get("start", 0))
                if previous is not None and bar_start <= previous:
                    health.duplicate_count += 1
                    continue
                self._seen_kline_bars[interval] = bar_start
                if previous is not None:
                    from ..utils.timeutil import interval_ms

                    expected = previous + interval_ms(interval)
                    if bar_start > expected:
                        health.sequence_gaps += 1
                        log.warning(
                            "DATA",
                            f"Missing {interval}m candle(s) between {previous} and {bar_start} "
                            "— will be repaired from REST",
                            topic=key,
                        )
            for handler in self._kline_handlers:
                await handler(interval, item)

    async def _handle_orderbook(self, message: dict[str, Any], health: StreamHealth) -> None:
        data = message.get("data", {}) or {}
        msg_type = message.get("type", "delta")
        # Bybit orderbook messages carry `u` (update id) and `seq`; a gap means
        # our local book may be wrong, so we flag it and wait for a snapshot.
        sequence = data.get("u")
        if isinstance(sequence, int):
            if (
                health.last_sequence is not None
                and msg_type == "delta"
                and sequence != health.last_sequence + 1
            ):
                health.sequence_gaps += 1
            health.last_sequence = sequence
        for handler in self._orderbook_handlers:
            await handler(data, msg_type)

    def worst_staleness_seconds(self) -> float:
        """Age of the least-recently-updated subscribed topic."""
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
    """Private order / execution / wallet / position stream on the demo host."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        ping_interval: float = 20.0,
        max_backoff: float = 60.0,
        include_position: bool = False,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._include_position = include_position
        self.health: dict[str, StreamHealth] = {}
        self._socket = _ReconnectingSocket(
            DEMO_WS_PRIVATE,
            name="private",
            ping_interval=ping_interval,
            max_backoff=max_backoff,
            on_connect=self._authenticate_and_subscribe,
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

    async def _authenticate_and_subscribe(self) -> None:
        expires = now_ms() + 10_000
        await self._socket.send(
            {"op": "auth", "args": sign_ws_auth(self._api_key, self._api_secret, expires)}
        )
        topics = ["order", "execution", "wallet"]
        if self._include_position:
            topics.append("position")
        await self._socket.send({"op": "subscribe", "args": topics})
        log.info("DATA", f"Private stream subscribed: {', '.join(topics)}")

    async def _dispatch(self, message: dict[str, Any]) -> None:
        op = message.get("op")
        if op == "auth":
            if message.get("success"):
                log.info("DATA", "Private stream authenticated")
            else:
                log.error("DATA", f"Private stream auth failed: {message.get('ret_msg')}")
            return
        if op in {"subscribe", "pong"}:
            return

        topic = message.get("topic")
        if not isinstance(topic, str):
            return

        health = self.health.setdefault(topic, StreamHealth(topic=topic))
        health.last_updated_ms = int(message.get("creationTime") or now_ms())
        health.message_count += 1

        data = message.get("data", []) or []
        if topic == "order":
            for handler in self._order_handlers:
                await handler(data)
        elif topic == "execution":
            for handler in self._execution_handlers:
                await handler(data)
        elif topic == "wallet":
            for handler in self._wallet_handlers:
                await handler(data)
        elif topic == "position":
            for handler in self._position_handlers:
                await handler(data)
