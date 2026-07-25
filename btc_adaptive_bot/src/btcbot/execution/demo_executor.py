"""Actual Bybit Demo order execution (Layer 3).

Every real order passes through this class, and every real order therefore
passes the same gates in the same order:

    demo verified? → safe mode? → capability supported? → duplicate/stale?
    → sized and bounded? → circuit breakers? → DB row reserved? → disclosed?
    → submitted

If any gate fails, no HTTP request is made. The database row is written
*before* submission so that a crash between "sent" and "recorded" still leaves
an authoritative trace that blocks a duplicate on restart.
"""

from __future__ import annotations

from typing import Any

from ..config.schema import RiskConfig
from ..database.repositories import DemoOrderRepository, SystemRepository
from ..exchange.demo_guard import DemoGuard
from ..exchange.instruments import ExchangeCapabilities
from ..exchange.models import (
    Capability,
    Category,
    Execution,
    OrderRequest,
    OrderType,
    Side,
    TimeInForce,
)
from ..exchange.rest import BybitDemoClient
from ..risk.position_sizing import PositionSizer, SizingInputs, SizingResult
from ..safety.circuit_breakers import CircuitBreakers
from ..strategies.base import Direction, StrategySignal
from ..utils.errors import ApiError, TransportError, UnsupportedOperationError
from ..utils.ids import client_order_id
from ..utils.logging import get_logger
from ..utils.numeric import format_qty
from ..utils.timeutil import iso, now_utc
from .order_safety import OrderSafetyGuard, disclose_order
from .position_ledger import LedgerPosition, PositionLedger

log = get_logger(__name__)


class DemoExecutionResult:
    """Outcome of an attempted real demo order."""

    __slots__ = ("success", "reason", "client_order_id", "exchange_order_id", "position", "sizing")

    def __init__(
        self,
        success: bool,
        *,
        reason: str = "",
        client_order_id_value: str | None = None,
        exchange_order_id: str | None = None,
        position: LedgerPosition | None = None,
        sizing: SizingResult | None = None,
    ) -> None:
        self.success = success
        self.reason = reason
        self.client_order_id = client_order_id_value
        self.exchange_order_id = exchange_order_id
        self.position = position
        self.sizing = sizing


class DemoExecutor:
    """Submits and manages real orders on the Bybit demo account."""

    def __init__(
        self,
        client: BybitDemoClient,
        *,
        guard: DemoGuard,
        breakers: CircuitBreakers,
        sizer: PositionSizer,
        ledger: PositionLedger,
        safety: OrderSafetyGuard,
        orders: DemoOrderRepository,
        system: SystemRepository,
        risk_config: RiskConfig,
        experiment_id: str,
        dry_run: bool = False,
    ) -> None:
        self.client = client
        self.guard = guard
        self.breakers = breakers
        self.sizer = sizer
        self.ledger = ledger
        self.safety = safety
        self.orders = orders
        self.system = system
        self.risk_config = risk_config
        self.experiment_id = experiment_id
        # Dry run exercises every gate and stops at submission. It is a
        # debugging aid, not a trading mode — the 14-day timer never starts here.
        self.dry_run = dry_run
        self.submitted_orders = 0
        self.rejected_orders = 0

    # --- entry ------------------------------------------------------------

    async def submit_entry(
        self,
        signal: StrategySignal,
        *,
        setup_id: str,
        signal_id: str | None,
        capabilities: ExchangeCapabilities,
        equity: float,
        available: float,
        atr: float,
        expectancy_r: float,
        observations: int,
        drawdown_pct: float,
        spread_bps: float,
        news_size_factor: float,
        news_state: str | None,
        volatility_pct: float | None,
    ) -> DemoExecutionResult:
        """Attempt a real demo entry for ``signal``."""
        # --- gate 1: demo verification (never bypassable) ----------------
        if not self.guard.orders_permitted():
            return self._reject(
                setup_id, "demo environment is not verified — order submission disabled"
            )

        # --- gate 2: safe mode -------------------------------------------
        allowed, reason = self.breakers.orders_allowed()
        if not allowed:
            return self._reject(setup_id, reason)

        # --- gate 3: exchange capability ---------------------------------
        instrument = capabilities.primary
        category = capabilities.primary_category or Category.SPOT
        if signal.direction is Direction.SHORT and not instrument.supports(Capability.SHORT):
            # Expected and journaled, not an error: the strategy keeps running in
            # the shadow layer so its hypothesis is still researched.
            return self._reject(
                setup_id,
                f"short_not_supported_on_{category.value} — researched in shadow only",
                level="INFO",
            )

        # --- gate 4: duplicate / staleness -------------------------------
        verdict = self.safety.check(setup_id=setup_id, intent="entry")
        if not verdict.allowed:
            trip = self.breakers.record_duplicate_attempt(setup_id)
            return self._reject(setup_id, verdict.reason + (f" [{trip.reason}]" if trip else ""))

        # --- gate 5: sizing ----------------------------------------------
        sizing = self.sizer.calculate(
            SizingInputs(
                equity=equity,
                available_balance=available,
                entry_price=signal.entry_reference,
                stop_price=signal.stop_price,
                direction=signal.direction,
                confidence=signal.confidence,
                atr=atr,
                expectancy_r=expectancy_r,
                observations=observations,
                drawdown_pct=drawdown_pct,
                regime=signal.regime.value,
                regime_confidence=signal.regime_confidence,
                spread_bps=spread_bps,
                news_size_factor=news_size_factor,
                volatility_pct=volatility_pct,
            ),
            instrument,
        )
        if not sizing.approved:
            return self._reject(setup_id, f"position sizing rejected: {sizing.reason}", sizing=sizing)

        # --- gate 6: circuit breakers on the concrete numbers -------------
        trip = self.breakers.check_quantity(
            float(sizing.quantity), equity=equity, price=signal.entry_reference
        )
        if trip is not None:
            return self._reject(setup_id, trip.reason, sizing=sizing)
        trip = self.breakers.check_order_rate()
        if trip is not None:
            return self._reject(setup_id, trip.reason, sizing=sizing)

        # --- gate 7: reserve the database row before any network call -----
        order_id = client_order_id(
            self.experiment_id,
            signal.strategy_id,
            signal.strategy_version,
            setup_id,
            signal.bar_open_ms,
        )
        if not self.safety.begin(setup_id, "entry"):
            return self._reject(setup_id, "another submission for this setup started first")

        try:
            side = Side.BUY if signal.direction is Direction.LONG else Side.SELL
            reserved = self.orders.reserve(
                {
                    "client_order_id": order_id,
                    "experiment_id": self.experiment_id,
                    "signal_id": signal_id,
                    "setup_id": setup_id,
                    "strategy_id": signal.strategy_id,
                    "strategy_version": signal.strategy_version,
                    "signal_ts_utc": iso(now_utc()),
                    "submitted_ts_utc": iso(now_utc()),
                    "symbol": signal.symbol,
                    "category": category.value,
                    "side": side.value,
                    "order_type": OrderType.MARKET.value,
                    "intent": "entry",
                    "quantity": float(sizing.quantity),
                    "quantity_str": sizing.quantity_str,
                    "price": None,
                    "estimated_notional": sizing.notional,
                    "stop_price": signal.stop_price,
                    "target_price": signal.target_price,
                    "estimated_risk_pct": sizing.risk_pct_of_equity,
                    "regime": signal.regime.value,
                    "confidence": signal.confidence,
                    "sizing_reasoning": sizing.explain(),
                }
            )
            if not reserved:
                self.breakers.record_duplicate_attempt(setup_id)
                return self._reject(setup_id, "database rejected a duplicate order for this setup")

            # --- gate 8: disclosure ---------------------------------------
            disclose_order(
                strategy_id=signal.strategy_id,
                strategy_version=signal.strategy_version,
                direction=signal.direction.value,
                symbol=signal.symbol,
                category=category.value,
                order_type=OrderType.MARKET.value,
                quantity=sizing.quantity_str,
                estimated_notional=sizing.notional,
                entry_reference=signal.entry_reference,
                stop_price=signal.stop_price,
                target_price=signal.target_price,
                estimated_risk_pct=sizing.risk_pct_of_equity,
                regime=signal.regime.value,
                confidence=signal.confidence,
                client_order_id=order_id,
            )

            if self.dry_run:
                self.orders.mark_result(
                    order_id, status="rejected", reject_reason="dry_run: not submitted"
                )
                log.info("DEMO", "DRY RUN — order validated but NOT submitted to Bybit")
                return DemoExecutionResult(
                    False, reason="dry_run", client_order_id_value=order_id, sizing=sizing
                )

            # --- submit ---------------------------------------------------
            request = OrderRequest(
                symbol=signal.symbol,
                category=category,
                side=side,
                order_type=OrderType.MARKET,
                qty=sizing.quantity_str,
                client_order_id=order_id,
                time_in_force=TimeInForce.IOC,
                # Spot market orders default to quote-denominated quantity; we
                # always size in the base coin, so state it explicitly.
                market_unit="baseCoin" if category is Category.SPOT else None,
            )

            try:
                result = await self.client.place_order(request)
            except ApiError as exc:
                self.orders.mark_result(
                    order_id, status="rejected", reject_reason=f"retCode={exc.ret_code} {exc.ret_msg}"
                )
                self.rejected_orders += 1
                log.error("ORDER", f"Bybit rejected the order: {exc.ret_msg} (retCode {exc.ret_code})")
                self.system.event(
                    "order_rejected",
                    f"{signal.strategy_id} entry rejected: {exc.ret_msg}",
                    level="ERROR",
                    experiment_id=self.experiment_id,
                    payload={"retCode": exc.ret_code, "client_order_id": order_id},
                )
                return DemoExecutionResult(False, reason=exc.ret_msg, client_order_id_value=order_id)
            except TransportError as exc:
                # Ambiguous: the order may or may not exist at the exchange.
                # Left as 'submitted' so reconciliation resolves it rather than
                # a blind retry creating a second position.
                self.orders.mark_result(
                    order_id, status="submitted", reject_reason=f"transport: {exc}"
                )
                log.error("ORDER", f"Transport failure during submission: {exc} — will reconcile")
                return DemoExecutionResult(False, reason=str(exc), client_order_id_value=order_id)

            self.orders.mark_result(
                order_id,
                status="accepted",
                exchange_order_id=result.exchange_order_id,
                raw_response=result.raw,
            )
            self.breakers.record_order_submitted()
            self.submitted_orders += 1

            log.info(
                "ORDER",
                f"Demo {side.value} accepted — exchange id {result.exchange_order_id}",
                strategy=signal.strategy_id,
            )

            position = self.ledger.open(
                signal=signal,
                setup_id=setup_id,
                signal_id=signal_id,
                category=category.value,
                entry_price=signal.entry_reference,
                quantity=float(sizing.quantity),
                entry_order_id=order_id,
                news_state=news_state,
                atr=atr,
            )
            self.system.event(
                "demo_entry",
                f"{signal.strategy_id} opened {signal.direction.value} {sizing.quantity_str} {signal.symbol}",
                experiment_id=self.experiment_id,
                payload={
                    "client_order_id": order_id,
                    "position_id": position.position_id,
                    "risk_pct": sizing.risk_pct_of_equity,
                },
            )
            return DemoExecutionResult(
                True,
                client_order_id_value=order_id,
                exchange_order_id=result.exchange_order_id,
                position=position,
                sizing=sizing,
            )
        finally:
            self.safety.finish(setup_id, "entry")

    # --- exit -------------------------------------------------------------

    async def submit_exit(
        self,
        position: LedgerPosition,
        *,
        exit_reason: str,
        capabilities: ExchangeCapabilities,
        quantity: float | None = None,
    ) -> DemoExecutionResult:
        """Close (or partially reduce) a real demo position at market."""
        if not self.guard.orders_permitted():
            return self._reject(position.setup_id, "demo not verified — cannot submit exit")

        instrument = capabilities.primary
        category = capabilities.primary_category or Category.SPOT
        qty = quantity if quantity is not None else position.remaining_qty
        rounded = instrument.round_qty(qty)
        valid, message = instrument.qty_within_bounds(rounded)
        if not valid:
            # Below the exchange minimum: the residue cannot be sold. Close the
            # ledger entry anyway so accounting stays truthful.
            log.warning("ORDER", f"Cannot submit exit ({message}); closing ledger entry")
            return DemoExecutionResult(False, reason=message)

        intent = "exit" if quantity is None else f"exit_partial_{int(qty * 1e8)}"
        order_id = client_order_id(
            self.experiment_id,
            position.strategy_id,
            position.strategy_version,
            f"{position.setup_id}_{intent}",
            int(position.opened_at.timestamp() * 1000),
        )
        side = Side.SELL if position.direction is Direction.LONG else Side.BUY

        if not self.safety.begin(position.setup_id, intent):
            return self._reject(position.setup_id, "exit already in flight")

        try:
            reserved = self.orders.reserve(
                {
                    "client_order_id": order_id,
                    "experiment_id": self.experiment_id,
                    "signal_id": position.signal_id,
                    "setup_id": position.setup_id,
                    "strategy_id": position.strategy_id,
                    "strategy_version": position.strategy_version,
                    "signal_ts_utc": iso(now_utc()),
                    "submitted_ts_utc": iso(now_utc()),
                    "symbol": position.symbol,
                    "category": category.value,
                    "side": side.value,
                    "order_type": OrderType.MARKET.value,
                    "intent": intent,
                    "quantity": float(rounded),
                    "quantity_str": format_qty(rounded, instrument.qty_step),
                    "price": None,
                    "estimated_notional": float(rounded) * position.entry_price,
                    "stop_price": position.stop_price,
                    "target_price": position.target_price,
                    "estimated_risk_pct": 0.0,
                    "regime": position.entry_regime,
                    "confidence": position.confidence,
                    "sizing_reasoning": f"exit: {exit_reason}",
                }
            )
            if not reserved:
                return self._reject(position.setup_id, f"duplicate exit order for {intent}")

            log.info(
                "ORDER",
                f"Demo {side.value} EXIT {format_qty(rounded, instrument.qty_step)} "
                f"{position.symbol} — reason: {exit_reason}",
                strategy=position.strategy_id,
            )

            if self.dry_run:
                self.orders.mark_result(order_id, status="rejected", reject_reason="dry_run")
                return DemoExecutionResult(False, reason="dry_run", client_order_id_value=order_id)

            request = OrderRequest(
                symbol=position.symbol,
                category=category,
                side=side,
                order_type=OrderType.MARKET,
                qty=format_qty(rounded, instrument.qty_step),
                client_order_id=order_id,
                time_in_force=TimeInForce.IOC,
                market_unit="baseCoin" if category is Category.SPOT else None,
                reduce_only=True if category is not Category.SPOT else None,
            )

            try:
                result = await self.client.place_order(request)
            except (ApiError, TransportError) as exc:
                self.orders.mark_result(order_id, status="rejected", reject_reason=str(exc))
                log.error("ORDER", f"Exit order failed: {exc}")
                return DemoExecutionResult(False, reason=str(exc), client_order_id_value=order_id)

            self.orders.mark_result(
                order_id,
                status="accepted",
                exchange_order_id=result.exchange_order_id,
                raw_response=result.raw,
            )
            self.breakers.record_order_submitted()
            self.submitted_orders += 1
            return DemoExecutionResult(
                True, client_order_id_value=order_id, exchange_order_id=result.exchange_order_id
            )
        finally:
            self.safety.finish(position.setup_id, intent)

    # --- fills ------------------------------------------------------------

    def record_fill(self, execution: Execution) -> None:
        """Persist a fill from the private stream or a REST reconciliation."""
        order = self.orders.get(execution.client_order_id) if execution.client_order_id else None
        inserted = self.orders.record_fill(
            {
                "fill_id": execution.exec_id,
                "client_order_id": execution.client_order_id or None,
                "exchange_order_id": execution.order_id,
                "experiment_id": self.experiment_id,
                "strategy_id": order.get("strategy_id") if order else None,
                "symbol": execution.symbol,
                "side": execution.side.value,
                "price": execution.price,
                "quantity": execution.qty,
                "fee": execution.fee,
                "fee_currency": execution.fee_currency,
                "is_maker": int(execution.is_maker),
                "exec_ts_utc": iso(now_utc()),
                "raw": execution.raw,
            }
        )
        if inserted:
            log.info(
                "FILL",
                f"{execution.qty:.6f} {execution.symbol} @ {execution.price:,.2f} "
                f"(fee {execution.fee:.4f} {execution.fee_currency})",
            )
            if order:
                self.orders.mark_result(execution.client_order_id, status="filled")

    # --- helpers ----------------------------------------------------------

    def _reject(
        self, setup_id: str, reason: str, *, level: str = "WARNING", sizing: SizingResult | None = None
    ) -> DemoExecutionResult:
        self.rejected_orders += 1
        if level == "INFO":
            log.info("DEMO", f"Order not sent ({setup_id}): {reason}")
        else:
            log.warning("DEMO", f"Order not sent ({setup_id}): {reason}")
        self.system.event(
            "order_not_sent",
            reason,
            level=level,
            experiment_id=self.experiment_id,
            payload={"setup_id": setup_id},
        )
        return DemoExecutionResult(False, reason=reason, sizing=sizing)

    def stats(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted_orders,
            "rejected": self.rejected_orders,
            "safety": self.safety.snapshot(),
        }


def unsupported_direction_error(direction: Direction, category: Category) -> UnsupportedOperationError:
    return UnsupportedOperationError(
        f"{direction.value} orders are not supported on {category.value}; "
        "this strategy continues to be researched in the shadow engine"
    )
