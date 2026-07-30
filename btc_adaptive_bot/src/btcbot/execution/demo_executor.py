"""Actual OKX Demo order execution (Layer 3) — BTC X-Perp, long and short.

Every real order passes through this class, and every real order therefore
passes the same gates in the same order:

    demo verified? → safe mode? → capability supported? → duplicate/stale?
    → leverage decided (journaled)? → sized and margin-bounded?
    → circuit breakers? → leverage SET AND CONFIRMED at the exchange?
    → DB row reserved? → disclosed? → submitted

If any gate fails, no order request is made, and the refusal is journaled to
``rejected_signals`` with the gate that refused it. The database row is written
*before* submission so that a crash between "sent" and "recorded" still leaves
an authoritative trace that blocks a duplicate on restart.

Perpetual-swap specifics handled here:

* **Position mode adaptation** — the account's ``posMode`` decides order
  shape: ``long_short_mode`` orders carry ``posSide``; ``net_mode`` closing
  orders carry ``reduceOnly`` so a close can never flip into an opposite
  position. The mode is read from the account, never forced.
* **Isolated margin only** — ``tdMode`` is always ``isolated``; a rejection
  is a journaled failure, never a silent retry with ``cross``.
* **Set-and-confirm leverage** — the chosen leverage is written with
  ``set-leverage`` and then read back with ``leverage-info`` before the entry
  order exists. A mismatch aborts the entry.
* **Contracts vs base units** — orders are sized in contracts (via the
  discovered ``ctVal``/``ctMult``/``lotSz``); the ledger keeps base-currency
  quantities so PnL stays exact.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from typing import Any

from ..config.schema import RiskConfig
from ..database.repositories import (
    DemoOrderRepository,
    LeverageDecisionRepository,
    RejectedSignalRepository,
    SystemRepository,
)
from ..exchange.demo_guard import DemoGuard
from ..exchange.instruments import ExchangeCapabilities
from ..exchange.models import (
    Capability,
    Execution,
    OrderRequest,
    OrderType,
    PositionMode,
    PosSide,
    Side,
    TdMode,
)
from ..exchange.rest import OkxDemoClient
from ..risk.leverage_engine import LeverageDecision, LeverageEngine, LeverageInputs
from ..risk.position_sizing import PositionSizer, SizingInputs, SizingResult
from ..safety.circuit_breakers import CircuitBreakers
from ..strategies.base import Direction, StrategySignal
from ..utils.errors import ApiError, TransportError
from ..utils.ids import client_order_id
from ..utils.logging import get_logger
from ..utils.numeric import format_qty
from ..utils.timeutil import iso, now_utc
from .order_safety import OrderSafetyGuard, disclose_order
from .position_ledger import LedgerPosition, PositionLedger
from .protection import PositionProtector, ProtectionRegistry, ProtectionState
from .reconciliation import FillReconciler, ReconciliationOutcome

log = get_logger(__name__)


class DemoExecutionResult:
    """Outcome of an attempted real demo order."""

    __slots__ = (
        "success",
        "reason",
        "client_order_id",
        "exchange_order_id",
        "position",
        "sizing",
        "leverage_decision",
    )

    def __init__(
        self,
        success: bool,
        *,
        reason: str = "",
        client_order_id_value: str | None = None,
        exchange_order_id: str | None = None,
        position: LedgerPosition | None = None,
        sizing: SizingResult | None = None,
        leverage_decision: LeverageDecision | None = None,
    ) -> None:
        self.success = success
        self.reason = reason
        self.client_order_id = client_order_id_value
        self.exchange_order_id = exchange_order_id
        self.position = position
        self.sizing = sizing
        self.leverage_decision = leverage_decision


class DemoExecutor:
    """Submits and manages real orders on the OKX demo account."""

    def __init__(
        self,
        client: OkxDemoClient,
        *,
        guard: DemoGuard,
        breakers: CircuitBreakers,
        sizer: PositionSizer,
        leverage_engine: LeverageEngine,
        ledger: PositionLedger,
        safety: OrderSafetyGuard,
        orders: DemoOrderRepository,
        leverage_decisions: LeverageDecisionRepository,
        rejected_signals: RejectedSignalRepository,
        system: SystemRepository,
        risk_config: RiskConfig,
        experiment_id: str,
        position_mode: PositionMode = PositionMode.NET,
        dry_run: bool = False,
        reconciler: FillReconciler | None = None,
        protector: PositionProtector | None = None,
    ) -> None:
        self.client = client
        # One reconciliation behaviour, shared with the smoke test. Injectable
        # so tests can shorten the backoff without changing what is tested.
        self.reconciler = reconciler or FillReconciler(client)
        # Exchange-side protection. Every real entry must end with a verified
        # stop registered at OKX — see execution/protection.py.
        self.protector = protector or PositionProtector(client)
        self.protection = ProtectionRegistry()
        self.guard = guard
        self.breakers = breakers
        self.sizer = sizer
        self.leverage_engine = leverage_engine
        self.ledger = ledger
        self.safety = safety
        self.orders = orders
        self.leverage_decisions = leverage_decisions
        self.rejected_signals = rejected_signals
        self.system = system
        self.risk_config = risk_config
        self.experiment_id = experiment_id
        self.position_mode = position_mode
        # Dry run exercises every gate and stops at submission. It is a
        # debugging aid, not a trading mode — the 14-day timer never starts here.
        self.dry_run = dry_run
        self.submitted_orders = 0
        self.rejected_orders = 0
        # Leverage confirmed at the exchange per (posSide) — re-confirmed
        # before every entry regardless; this is only for reporting.
        self.last_confirmed_leverage: float | None = None

    # --- order shaping per position mode ---------------------------------

    def _entry_sides(self, direction: Direction) -> tuple[Side, PosSide | None]:
        if direction is Direction.LONG:
            side = Side.BUY
            pos_side = PosSide.LONG
        else:
            side = Side.SELL
            pos_side = PosSide.SHORT
        if self.position_mode is PositionMode.NET:
            return side, None
        return side, pos_side

    def _exit_sides(self, direction: Direction) -> tuple[Side, PosSide | None, bool | None]:
        """(side, posSide, reduceOnly) for closing a position in ``direction``."""
        if direction is Direction.LONG:
            side = Side.SELL
            pos_side = PosSide.LONG
        else:
            side = Side.BUY
            pos_side = PosSide.SHORT
        if self.position_mode is PositionMode.NET:
            # In net mode a close is just an opposite-side order; reduceOnly
            # guarantees it can only reduce, never flip.
            return side, None, True
        return side, pos_side, None

    def _leverage_pos_side(self, direction: Direction) -> str | None:
        """posSide argument for set-leverage in long/short isolated mode."""
        if self.position_mode is PositionMode.NET:
            return None
        return "long" if direction is Direction.LONG else "short"

    # --- rejection journaling ---------------------------------------------

    def _journal_rejection(
        self,
        *,
        layer_index: int,
        layer_name: str,
        reason: str,
        signal: StrategySignal | None = None,
        setup_id: str | None = None,
        signal_id: str | None = None,
        strategy_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.rejected_signals.record(
                {
                    "experiment_id": self.experiment_id,
                    "ts_utc": iso(now_utc()),
                    "signal_id": signal_id,
                    "setup_id": setup_id,
                    "strategy_id": strategy_id
                    or (signal.strategy_id if signal else "unknown"),
                    "strategy_version": signal.strategy_version if signal else None,
                    "inst_id": signal.symbol if signal else None,
                    "direction": signal.direction.value if signal else None,
                    "layer_index": layer_index,
                    "layer_name": layer_name,
                    "reason": reason,
                    "detail": detail,
                    "regime": signal.regime.value if signal else None,
                    "confidence": signal.confidence if signal else None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - journaling must not break execution
            log.warning("DEMO", f"Could not journal rejection: {exc}")

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
        risk_state: str = "NORMAL",
    ) -> DemoExecutionResult:
        """Attempt a real demo entry for ``signal``."""

        def reject(
            layer_index: int, layer_name: str, reason: str, *, level: str = "WARNING", **kw: Any
        ) -> DemoExecutionResult:
            self._journal_rejection(
                layer_index=layer_index,
                layer_name=layer_name,
                reason=reason,
                signal=signal,
                setup_id=setup_id,
                signal_id=signal_id,
            )
            return self._reject(setup_id, reason, level=level, **kw)

        # --- gate 1: demo verification (never bypassable) ----------------
        if not self.guard.orders_permitted():
            return reject(
                1, "demo_verification", "demo environment is not verified — order submission disabled"
            )

        # --- gate 2: safe mode / circuit breakers ------------------------
        allowed, reason = self.breakers.orders_allowed()
        if not allowed:
            return reject(2, "circuit_breakers", reason)

        # --- gate 3: exchange capability ---------------------------------
        instrument = capabilities.primary
        needed = Capability.LONG if signal.direction is Direction.LONG else Capability.SHORT
        if not instrument.supports(needed):
            return reject(
                3,
                "instrument_capability",
                f"{signal.direction.value} not supported on {instrument.inst_id} — "
                "researched in shadow only",
                level="INFO",
            )

        # --- gate 4: duplicate / staleness -------------------------------
        verdict = self.safety.check(setup_id=setup_id, intent="entry")
        if not verdict.allowed:
            trip = self.breakers.record_duplicate_attempt(setup_id)
            return reject(
                4, "duplicate_guard", verdict.reason + (f" [{trip.reason}]" if trip else "")
            )

        # --- gate 5: leverage decision (journaled either way) ------------
        leverage_decision = self.leverage_engine.decide(
            LeverageInputs(
                entry_price=signal.entry_reference,
                stop_price=signal.stop_price,
                direction=signal.direction.value,
                confidence=signal.confidence,
                volatility_pct=volatility_pct,
                regime=signal.regime.value,
                regime_confidence=signal.regime_confidence,
                drawdown_pct=drawdown_pct,
                risk_state=risk_state,
                max_exchange_leverage=float(instrument.max_leverage),
            )
        )
        try:
            self.leverage_decisions.record(
                {
                    "experiment_id": self.experiment_id,
                    "ts_utc": iso(now_utc()),
                    "setup_id": setup_id,
                    "strategy_id": signal.strategy_id,
                    "inst_id": instrument.inst_id,
                    "direction": signal.direction.value,
                    "approved": int(leverage_decision.approved),
                    "leverage": leverage_decision.leverage,
                    "confidence": signal.confidence,
                    "volatility_pct": volatility_pct,
                    "regime": signal.regime.value,
                    "regime_confidence": signal.regime_confidence,
                    "drawdown_pct": drawdown_pct,
                    "risk_state": risk_state,
                    "stop_distance_pct": leverage_decision.stop_distance_pct,
                    "est_liq_distance_pct": leverage_decision.estimated_liq_distance_pct,
                    "liq_buffer_ratio": leverage_decision.liq_buffer_ratio,
                    "confirmed_by_exchange": 0,
                    "reason": leverage_decision.reason,
                    "reasoning": leverage_decision.reasoning,
                    "adjustments": leverage_decision.adjustments,
                }
            )
        except Exception as exc:  # noqa: BLE001 - journaling must not break execution
            log.warning("DEMO", f"Could not journal leverage decision: {exc}")

        if not leverage_decision.approved:
            return reject(
                5,
                "leverage_engine",
                f"leverage engine rejected the entry: {leverage_decision.reason}",
            )

        # --- gate 6: sizing (contracts + margin) -------------------------
        sizing = self.sizer.calculate(
            SizingInputs(
                equity=equity,
                available_balance=available,
                entry_price=signal.entry_reference,
                stop_price=signal.stop_price,
                direction=signal.direction,
                leverage=leverage_decision.leverage,
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
            return reject(
                6,
                "position_sizing",
                f"position sizing rejected: {sizing.reason}",
                sizing=sizing,
            )

        # --- gate 7: circuit breakers on the concrete numbers -------------
        trip = self.breakers.check_quantity(
            float(sizing.quantity), equity=equity, price=signal.entry_reference
        )
        if trip is not None:
            return reject(7, "circuit_breakers_quantity", trip.reason, sizing=sizing)
        trip = self.breakers.check_order_rate()
        if trip is not None:
            return reject(7, "circuit_breakers_rate", trip.reason, sizing=sizing)

        # --- gate 8: SET AND CONFIRM leverage at the exchange -------------
        if not self.dry_run:
            confirmed, detail = await self._set_and_confirm_leverage(
                instrument.inst_id,
                leverage_decision.leverage,
                pos_side=self._leverage_pos_side(signal.direction),
            )
            if not confirmed:
                return reject(8, "leverage_confirmation", detail, sizing=sizing)
            with contextlib.suppress(Exception):
                self.leverage_decisions.mark_confirmed(setup_id)
            self.last_confirmed_leverage = leverage_decision.leverage

        # --- gate 9: reserve the database row before any network call -----
        order_id = client_order_id(
            self.experiment_id,
            signal.strategy_id,
            signal.strategy_version,
            setup_id,
            signal.bar_open_ms,
        )
        if not self.safety.begin(setup_id, "entry"):
            return reject(9, "in_flight_guard", "another submission for this setup started first")

        try:
            side, pos_side = self._entry_sides(signal.direction)
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
                    "symbol": instrument.inst_id,
                    "category": instrument.inst_type.value,
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
                    "pos_side": pos_side.value if pos_side else None,
                    "td_mode": TdMode.ISOLATED.value,
                    "leverage": leverage_decision.leverage,
                    "contracts": float(sizing.contracts),
                }
            )
            if not reserved:
                self.breakers.record_duplicate_attempt(setup_id)
                return reject(
                    9, "database_uniqueness", "database rejected a duplicate order for this setup"
                )

            # --- gate 10: disclosure --------------------------------------
            disclose_order(
                strategy_id=signal.strategy_id,
                strategy_version=signal.strategy_version,
                direction=signal.direction.value,
                symbol=instrument.inst_id,
                category=(
                    f"{instrument.inst_type.value} isolated {leverage_decision.leverage:.1f}x"
                ),
                order_type=OrderType.MARKET.value,
                quantity=f"{sizing.quantity_str} contracts ({sizing.quantity} {instrument.base_ccy})",
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
                log.info("DEMO", "DRY RUN — order validated but NOT submitted to OKX")
                return DemoExecutionResult(
                    False,
                    reason="dry_run",
                    client_order_id_value=order_id,
                    sizing=sizing,
                    leverage_decision=leverage_decision,
                )

            # --- submit ---------------------------------------------------
            # Attach the stop and target to the entry itself, so OKX creates
            # protection with the fill rather than in a later round trip. This
            # is belt; the braces are the verified OCO placed after the fill.
            # Both are needed: OKX can accept an order and reject its algo.
            request = OrderRequest(
                inst_id=instrument.inst_id,
                td_mode=TdMode.ISOLATED,
                side=side,
                order_type=OrderType.MARKET,
                sz=sizing.quantity_str,
                client_order_id=order_id,
                pos_side=pos_side,
                sl_trigger_price=str(instrument.round_price(signal.stop_price)),
                tp_trigger_price=(
                    str(instrument.round_price(signal.target_price))
                    if signal.target_price
                    else None
                ),
            )

            try:
                result = await self.client.place_order(request)
            except ApiError as exc:
                self.orders.mark_result(
                    order_id, status="rejected", reject_reason=f"code={exc.ret_code} {exc.ret_msg}"
                )
                self.rejected_orders += 1
                log.error("ORDER", f"OKX rejected the order: {exc.ret_msg} (code {exc.ret_code})")
                self._journal_rejection(
                    layer_index=10,
                    layer_name="exchange_rejection",
                    reason=f"code={exc.ret_code} {exc.ret_msg}",
                    signal=signal,
                    setup_id=setup_id,
                    signal_id=signal_id,
                )
                self.system.event(
                    "order_rejected",
                    f"{signal.strategy_id} entry rejected: {exc.ret_msg}",
                    level="ERROR",
                    experiment_id=self.experiment_id,
                    payload={"code": exc.ret_code, "client_order_id": order_id},
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

            # Accepted is not filled. OKX settles its read endpoints on
            # different schedules, so the outcome is established from order
            # details (the authority) before a position is booked.
            outcome = await self._confirm_fill(
                instrument.inst_id,
                order_id=result.exchange_order_id,
                client_order_id=order_id,
            )
            if not outcome.confirmed:
                return self._handle_unconfirmed(
                    outcome,
                    inst_id=instrument.inst_id,
                    order_id=result.exchange_order_id,
                    client_order_id=order_id,
                    signal=signal,
                    setup_id=setup_id,
                    signal_id=signal_id,
                )

            # The position is real. It must not remain open for longer than it
            # takes to prove a stop exists at the exchange.
            protection = await self._ensure_protection(
                instrument,
                signal=signal,
                direction=signal.direction,
                filled_size=outcome.filled_size or float(sizing.contracts),
                fill_price=outcome.avg_price or signal.entry_reference,
                client_order_id=order_id,
            )
            if not protection.protected:
                return await self._emergency_close_unprotected(
                    instrument,
                    direction=signal.direction,
                    filled_size=outcome.filled_size or float(sizing.contracts),
                    protection=protection,
                    signal=signal,
                    setup_id=setup_id,
                    signal_id=signal_id,
                    client_order_id=order_id,
                )

            # Best-effort: read back the live position for its actual
            # liquidation price so the protection layer monitors real numbers.
            liq_price = await self._read_liq_price(instrument.inst_id, signal.direction)

            position = self.ledger.open(
                signal=signal,
                setup_id=setup_id,
                signal_id=signal_id,
                category=instrument.inst_type.value,
                entry_price=signal.entry_reference,
                quantity=float(sizing.quantity),
                entry_order_id=order_id,
                news_state=news_state,
                atr=atr,
                leverage=leverage_decision.leverage,
                contracts=float(sizing.contracts),
                liq_price_at_entry=liq_price,
            )
            self.system.event(
                "demo_entry",
                f"{signal.strategy_id} opened {signal.direction.value} "
                f"{sizing.quantity_str} contracts {instrument.inst_id} "
                f"at {leverage_decision.leverage:.1f}x isolated",
                experiment_id=self.experiment_id,
                payload={
                    "client_order_id": order_id,
                    "position_id": position.position_id,
                    "risk_pct": sizing.risk_pct_of_equity,
                    "leverage": leverage_decision.leverage,
                    "required_margin": sizing.required_margin,
                    "liq_price": liq_price,
                },
            )
            return DemoExecutionResult(
                True,
                client_order_id_value=order_id,
                exchange_order_id=result.exchange_order_id,
                position=position,
                sizing=sizing,
                leverage_decision=leverage_decision,
            )
        finally:
            self.safety.finish(setup_id, "entry")

    async def _set_and_confirm_leverage(
        self, inst_id: str, leverage: float, *, pos_side: str | None
    ) -> tuple[bool, str]:
        """Write the leverage setting, then read it back. Set-without-confirm
        is never trusted before an entry order."""
        leverage_str = f"{leverage:g}"
        try:
            await self.client.set_leverage(
                inst_id, leverage_str, mgn_mode=TdMode.ISOLATED.value, pos_side=pos_side
            )
        except ApiError as exc:
            return (False, f"set-leverage rejected: code={exc.ret_code} {exc.ret_msg}")
        except TransportError as exc:
            return (False, f"set-leverage transport failure: {exc}")

        try:
            infos = await self.client.get_leverage_info(inst_id, mgn_mode=TdMode.ISOLATED.value)
        except (ApiError, TransportError) as exc:
            return (False, f"leverage confirmation failed: {exc}")

        relevant = [i for i in infos if pos_side is None or i.pos_side in ("", "net", pos_side)]
        for info in relevant:
            if abs(float(info.leverage) - leverage) < 1e-6:
                return (True, f"confirmed {leverage_str}x isolated on {inst_id}")
        observed = [(i.pos_side, str(i.leverage)) for i in infos]
        return (
            False,
            f"exchange reports leverage {observed}, expected {leverage_str}x — entry aborted",
        )

    async def _read_liq_price(self, inst_id: str, direction: Direction) -> float | None:
        """Best-effort read of the live position's liquidation price."""
        try:
            positions = await self.client.get_positions(inst_id)
        except (ApiError, TransportError) as exc:
            log.debug("DEMO", f"Could not read liquidation price: {exc}")
            return None
        wanted = "LONG" if direction is Direction.LONG else "SHORT"
        for position in positions:
            if position.direction == wanted and position.liq_price:
                return position.liq_price
        return None

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
        qty = quantity if quantity is not None else position.remaining_qty
        contracts = (
            instrument.contracts_from_base(qty)
            if instrument.is_derivative
            else instrument.round_qty(qty)
        )
        valid, message = instrument.qty_within_bounds(contracts)
        if not valid:
            # Below the exchange minimum: the residue cannot be closed at the
            # exchange. Close the ledger entry anyway so accounting stays truthful.
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
        side, pos_side, reduce_only = self._exit_sides(position.direction)

        if not self.safety.begin(position.setup_id, intent):
            return self._reject(position.setup_id, "exit already in flight")

        try:
            contracts_str = format_qty(contracts, instrument.lot_size)
            base_equivalent = (
                instrument.base_from_contracts(contracts)
                if instrument.is_derivative
                else contracts
            )
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
                    "category": position.category,
                    "side": side.value,
                    "order_type": OrderType.MARKET.value,
                    "intent": intent,
                    "quantity": float(base_equivalent),
                    "quantity_str": contracts_str,
                    "price": None,
                    "estimated_notional": float(base_equivalent) * position.entry_price,
                    "stop_price": position.stop_price,
                    "target_price": position.target_price,
                    "estimated_risk_pct": 0.0,
                    "regime": position.entry_regime,
                    "confidence": position.confidence,
                    "sizing_reasoning": f"exit: {exit_reason}",
                    "pos_side": pos_side.value if pos_side else None,
                    "td_mode": TdMode.ISOLATED.value,
                    "leverage": position.leverage,
                    "contracts": float(contracts),
                }
            )
            if not reserved:
                return self._reject(position.setup_id, f"duplicate exit order for {intent}")

            log.info(
                "ORDER",
                f"Demo {side.value} EXIT {contracts_str} contracts "
                f"{position.symbol} — reason: {exit_reason}",
                strategy=position.strategy_id,
            )

            if self.dry_run:
                self.orders.mark_result(order_id, status="rejected", reject_reason="dry_run")
                return DemoExecutionResult(False, reason="dry_run", client_order_id_value=order_id)

            request = OrderRequest(
                inst_id=position.symbol,
                td_mode=TdMode.ISOLATED,
                side=side,
                order_type=OrderType.MARKET,
                sz=contracts_str,
                client_order_id=order_id,
                pos_side=pos_side,
                reduce_only=reduce_only,
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

            # Same reconciliation as the entry path: confirm the exit really
            # filled and persist its fills (and its fee) rather than assuming.
            # An exit that cannot be confirmed is reported but does not enter
            # SAFE_MODE here — the trade manager re-reads the position, and a
            # stale "still open" reading is the safe direction to be wrong in.
            outcome = await self._confirm_fill(
                position.symbol,
                order_id=result.exchange_order_id,
                client_order_id=order_id,
            )
            if not outcome.confirmed:
                log.warning(
                    "RECONCILE",
                    f"Exit order {result.exchange_order_id} not confirmed within the "
                    f"reconciliation budget ({outcome.detail}) — the position will be "
                    "re-read from the exchange rather than assumed closed",
                )
            return DemoExecutionResult(
                True, client_order_id_value=order_id, exchange_order_id=result.exchange_order_id
            )
        finally:
            self.safety.finish(position.setup_id, intent)

    # --- exchange-side protection ----------------------------------------

    async def _ensure_protection(
        self,
        instrument,
        *,
        signal: StrategySignal,
        direction: Direction,
        filled_size: float,
        fill_price: float,
        client_order_id: str,
    ) -> ProtectionState:
        """Guarantee a verified exchange-side stop on a just-filled position.

        Protection is computed from the **actual fill price**, not the signal's
        reference: a stop placed around a price the exchange never traded sits
        at the wrong distance and risks the wrong amount.
        """
        stop_price = signal.stop_price
        target_price = signal.target_price
        if fill_price > 0 and signal.entry_reference > 0:
            # Preserve the strategy's intended risk *distance* around the price
            # actually paid, rather than its absolute level.
            drift = fill_price - signal.entry_reference
            stop_price = signal.stop_price + drift
            target_price = signal.target_price + drift if signal.target_price else None
            if abs(drift) > 1e-9:
                log.info(
                    "PROTECTION",
                    f"Filled at {fill_price:,.2f} vs reference {signal.entry_reference:,.2f} — "
                    f"stop shifted to {stop_price:,.2f}",
                )
        try:
            state = await self.protector.protect(
                instrument,
                direction=direction,
                filled_size=filled_size,
                entry_price=fill_price or signal.entry_reference,
                stop_price=stop_price,
                target_price=target_price,
                position_mode=self.position_mode,
                client_algo_id=f"p{client_order_id}"[:32],
            )
        except (ApiError, TransportError) as exc:
            # A read/placement failure is NOT evidence of protection. Treated
            # exactly like an absent stop.
            state = ProtectionState.unprotected(
                instrument.inst_id, f"protection could not be established: {exc}"
            )
        self.protection.record(state)
        return state

    async def _emergency_close_unprotected(
        self,
        instrument,
        *,
        direction: Direction,
        filled_size: float,
        protection: ProtectionState,
        signal: StrategySignal,
        setup_id: str,
        signal_id: str | None,
        client_order_id: str,
    ) -> DemoExecutionResult:
        """Close a position that has no verified stop, then stop trading.

        The position is real and naked. Closing it at market is a known small
        cost; leaving it open is an unbounded one. The ledger is deliberately
        NOT updated with an open position — there must be nothing for the
        software-side trade manager to "manage", because software-side
        management is exactly what failed to protect it.
        """
        log.critical(
            "PROTECTION",
            f"{instrument.inst_id} position has NO verified exchange stop "
            f"({protection.detail}) — closing it reduce-only now",
        )
        side, pos_side, reduce_only = self._exit_sides(direction)
        close_id = client_order_id_new = f"x{client_order_id}"[:32]
        closed = False
        close_detail = ""
        try:
            close_request = OrderRequest(
                inst_id=instrument.inst_id,
                td_mode=TdMode.ISOLATED,
                side=side,
                order_type=OrderType.MARKET,
                sz=format_qty(instrument.round_qty(filled_size), instrument.lot_size),
                client_order_id=client_order_id_new,
                pos_side=pos_side,
                reduce_only=reduce_only,
            )
            close_result = await self.client.place_order(close_request)
            closed = True
            close_detail = f"closed with {close_result.exchange_order_id}"
            log.critical("PROTECTION", f"Unprotected position CLOSED — {close_detail}")
        except (ApiError, TransportError) as exc:
            close_detail = f"CLOSE FAILED: {exc}"
            log.critical(
                "PROTECTION",
                f"Could not close the unprotected {instrument.inst_id} position: {exc}. "
                "CHECK YOUR OKX DEMO ACCOUNT IMMEDIATELY — a position may be open with "
                "no stop.",
            )

        # Trading stops either way: an entry that cannot be protected is a
        # defect, and the next one would be no safer.
        self.breakers.record_unprotected_position(
            inst_id=instrument.inst_id,
            detail=f"{protection.detail}; {close_detail}",
            closed=closed,
        )
        self.protection.forget(instrument.inst_id)
        self.orders.mark_result(
            client_order_id,
            status="filled",
            reject_reason=f"unprotected, emergency close: {protection.detail}",
        )
        self._journal_rejection(
            layer_index=10,
            layer_name="protection_unverified",
            reason=protection.detail,
            signal=signal,
            setup_id=setup_id,
            signal_id=signal_id,
        )
        self.system.event(
            "position_unprotected",
            f"{signal.strategy_id} entry had no verifiable exchange stop — "
            f"{'closed' if closed else 'CLOSE FAILED'}; SAFE_MODE entered",
            level="ERROR",
            experiment_id=self.experiment_id,
            payload={
                "inst_id": instrument.inst_id,
                "client_order_id": client_order_id,
                "close_client_order_id": close_id,
                "closed": closed,
                "detail": protection.detail,
            },
        )
        return DemoExecutionResult(
            False,
            reason=f"no verified exchange stop: {protection.detail}",
            client_order_id_value=client_order_id,
        )

    # --- fills and reconciliation -----------------------------------------

    async def _confirm_fill(
        self, inst_id: str, *, order_id: str, client_order_id: str
    ) -> ReconciliationOutcome:
        """Establish whether an accepted order actually filled, and persist it.

        Order details are the authority (see ``execution/reconciliation.py``);
        the fills endpoint lags them and supplies the per-fill detail. Any
        fill records that do arrive are persisted here, and any that arrive
        later are picked up by :meth:`record_fill` from the private stream or
        by startup reconciliation — both idempotent on ``fill_id``.
        """
        try:
            outcome = await self.reconciler.reconcile(
                inst_id, order_id=order_id, client_order_id=client_order_id
            )
        except (ApiError, TransportError) as exc:
            # Reads failed outright. Unknown outcome — treated exactly like a
            # timeout: never assumed filled, never assumed not filled.
            log.error("RECONCILE", f"Could not read back order {order_id}: {exc}")
            return ReconciliationOutcome(
                order=None, detail=f"reconciliation reads failed: {exc}"
            )

        for execution in outcome.fills:
            self.record_fill(execution)

        if outcome.confirmed:
            self.orders.mark_result(
                client_order_id,
                status="partially_filled" if outcome.partially_filled else "filled",
                exchange_order_id=outcome.order.order_id if outcome.order else order_id,
                raw_response=outcome.order.raw if outcome.order else None,
            )
            log.info("RECONCILE", f"Order {order_id} confirmed — {outcome.detail}")
            if outcome.fills_delayed:
                # Bookkeeping only: the fill is proven, its per-fill record is
                # simply not published yet. The stream or the next startup
                # reconciliation will persist it.
                log.warning(
                    "RECONCILE",
                    f"Order {order_id} filled but its fill record has not appeared yet — "
                    "it will be persisted when it does",
                )
        return outcome

    def _handle_unconfirmed(
        self,
        outcome: ReconciliationOutcome,
        *,
        inst_id: str,
        order_id: str,
        client_order_id: str,
        signal: StrategySignal,
        setup_id: str,
        signal_id: str | None,
    ) -> DemoExecutionResult:
        """An accepted order whose outcome could not be established.

        Two distinct situations, handled differently:

        * **Canceled** — a known outcome. No position exists, nothing is
          ambiguous, so trading continues.
        * **Unconfirmed** — the exchange never settled the order within the
          budget. We do not know whether we hold a position, so SAFE_MODE is
          entered and the position ledger is left untouched.

        In neither case is a replacement order sent. The order row keeps its
        ``UNIQUE(setup_id, intent)`` reservation, so a second submission for
        this setup is structurally impossible.
        """
        if outcome.canceled:
            self.orders.mark_result(
                client_order_id,
                status="cancelled",
                exchange_order_id=order_id,
                reject_reason=f"canceled at the exchange: {outcome.detail}",
            )
            log.warning("ORDER", f"Order {order_id} was canceled at the exchange — no position")
            self._journal_rejection(
                layer_index=10,
                layer_name="order_canceled",
                reason=outcome.detail,
                signal=signal,
                setup_id=setup_id,
                signal_id=signal_id,
            )
            self.system.event(
                "order_canceled",
                f"{signal.strategy_id} entry canceled at the exchange before filling",
                level="WARNING",
                experiment_id=self.experiment_id,
                payload={"order_id": order_id, "client_order_id": client_order_id},
            )
            return DemoExecutionResult(
                False, reason=f"order canceled: {outcome.detail}",
                client_order_id_value=client_order_id, exchange_order_id=order_id,
            )

        # Unknown. Stop trading and reconcile — never guess, never resubmit.
        self.orders.mark_result(
            client_order_id,
            status="accepted",
            exchange_order_id=order_id,
            reject_reason=f"unconfirmed: {outcome.detail}",
        )
        self.breakers.record_unconfirmed_order(
            order_id=order_id, client_order_id=client_order_id, detail=outcome.detail
        )
        log.critical(
            "RECONCILE",
            f"Order {order_id} ({inst_id}) was accepted but never confirmed. SAFE_MODE is "
            "active and no replacement order will be sent. Reconcile positions against the "
            "exchange before resuming.",
        )
        self._journal_rejection(
            layer_index=10,
            layer_name="unconfirmed_fill",
            reason=outcome.detail,
            signal=signal,
            setup_id=setup_id,
            signal_id=signal_id,
        )
        self.system.event(
            "order_unconfirmed",
            f"{signal.strategy_id} entry accepted but not confirmed — SAFE_MODE entered, "
            "position ledger left untouched pending reconciliation",
            level="ERROR",
            experiment_id=self.experiment_id,
            payload={
                "order_id": order_id,
                "client_order_id": client_order_id,
                "state": outcome.state,
                "waited_seconds": round(outcome.waited_seconds, 2),
                "attempts": outcome.order_attempts,
            },
        )
        return DemoExecutionResult(
            False, reason=f"unconfirmed fill: {outcome.detail}",
            client_order_id_value=client_order_id, exchange_order_id=order_id,
        )

    def record_fill(self, execution: Execution) -> None:
        """Persist a fill from the private stream or a REST reconciliation.

        OKX fill quantities are in contracts; the base-unit conversion for
        PnL accounting happens in the ledger, which knows the instrument.

        Attribution resolves by ``ordId`` first. OKX does not always echo
        ``clOrdId`` on the fills endpoint, and a fill whose ``clOrdId`` is
        blank would otherwise be persisted orphaned — recorded, but attached
        to no order, no strategy and no experiment.
        """
        order = self.orders.get_by_exchange_order_id(execution.order_id)
        if order is None and execution.client_order_id:
            order = self.orders.get(execution.client_order_id)
        # The order row is the authority on which client order this belongs to.
        client_order_id = (
            order.get("client_order_id") if order else execution.client_order_id
        ) or None
        inserted = self.orders.record_fill(
            {
                "fill_id": execution.exec_id,
                "client_order_id": client_order_id,
                "exchange_order_id": execution.order_id,
                "experiment_id": self.experiment_id,
                "strategy_id": order.get("strategy_id") if order else None,
                "symbol": execution.inst_id,
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
                f"{execution.qty:g} contract(s) {execution.inst_id} @ {execution.price:,.2f} "
                f"(fee {execution.fee:.6f} {execution.fee_currency})",
            )
            # Only promote to "filled" once the order is known and not already
            # in a terminal state — a late fill must never reopen a cancelled
            # order or downgrade one already reconciled.
            if order and client_order_id and order.get("status") in ("submitted", "accepted"):
                self.orders.mark_result(client_order_id, status="filled")

    # --- helpers ----------------------------------------------------------

    def _reject(
        self,
        setup_id: str,
        reason: str,
        *,
        level: str = "WARNING",
        sizing: SizingResult | None = None,
        leverage_decision: LeverageDecision | None = None,
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
        return DemoExecutionResult(
            False, reason=reason, sizing=sizing, leverage_decision=leverage_decision
        )

    def contracts_for(self, capabilities: ExchangeCapabilities, base_qty: float) -> Decimal:
        """Convenience conversion used by reconciliation paths."""
        instrument = capabilities.primary
        if instrument.is_derivative:
            return instrument.contracts_from_base(base_qty)
        return instrument.round_qty(base_qty)

    def stats(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted_orders,
            "rejected": self.rejected_orders,
            "position_mode": self.position_mode.value,
            "last_confirmed_leverage": self.last_confirmed_leverage,
            "safety": self.safety.snapshot(),
        }
