"""End-to-end integration with a mocked exchange.

Exercises the whole order path — allocator → sizing → safety guards → executor →
ledger → trade management → allocator feedback — without touching the network.

The mock deliberately records every call so the tests can assert that **no order
request is made** when a gate fails. "Rejected before the request" is a much
stronger guarantee than "request made and rejected".
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from btcbot.config.schema import (
    AllocatorConfig,
    RiskConfig,
    SafetyConfig,
    ShadowConfig,
)
from btcbot.config.schema import LeverageConfig
from btcbot.exchange.demo_guard import DemoGuard, DemoVerification, SignalResult
from btcbot.exchange.instruments import ExchangeCapabilities
from btcbot.exchange.models import (
    Execution,
    LeverageInfo,
    OrderResult,
    PositionMode,
    Side,
    TdMode,
)
from btcbot.exchange.rest import OkxDemoClient
from btcbot.execution.allocator import DemoAllocator
from btcbot.execution.demo_executor import DemoExecutor
from btcbot.execution.order_safety import OrderSafetyGuard
from btcbot.execution.position_ledger import PositionLedger
from btcbot.execution.trade_manager import TradeManager
from btcbot.regime.classifier import Regime
from btcbot.risk.leverage_engine import LeverageEngine
from btcbot.risk.position_sizing import PositionSizer
from btcbot.safety.circuit_breakers import CircuitBreakers
from btcbot.strategies.base import Direction, ExitMechanism, ExitPolicy, StrategySignal
from btcbot.utils.errors import ApiError
from btcbot.utils.timeutil import now_utc

pytestmark = pytest.mark.integration


class MockOkxClient:
    """Records calls; never touches the network.

    Mirrors the OKX client surface the executor uses, including the
    set-and-confirm leverage round trip.
    """

    def __init__(
        self,
        *,
        fail_with: Exception | None = None,
        leverage_confirm_mismatch: bool = False,
    ) -> None:
        self.base_url = "https://eea.okx.com"
        self.orders: list[Any] = []
        self.cancels: list[Any] = []
        self.leverage_sets: list[tuple[str, str, str | None]] = []
        self._fail_with = fail_with
        self._leverage_confirm_mismatch = leverage_confirm_mismatch
        self._current_leverage = "0"
        self.consecutive_errors = 0

    @property
    def has_credentials(self) -> bool:
        return True

    async def place_order(self, request) -> OrderResult:
        if self._fail_with is not None:
            raise self._fail_with
        self.orders.append(request)
        return OrderResult(
            client_order_id=request.client_order_id,
            exchange_order_id=f"mock-{len(self.orders)}",
            accepted=True,
            raw={"code": "0"},
        )

    async def set_leverage(self, inst_id, leverage, *, mgn_mode, pos_side=None):
        self.leverage_sets.append((inst_id, leverage, pos_side))
        self._current_leverage = leverage
        return {"instId": inst_id, "lever": leverage, "mgnMode": mgn_mode}

    async def get_leverage_info(self, inst_id, *, mgn_mode):
        lever = "1" if self._leverage_confirm_mismatch else self._current_leverage
        return [
            LeverageInfo(inst_id=inst_id, margin_mode=mgn_mode, pos_side="net",
                         leverage=__import__("decimal").Decimal(lever))
        ]

    async def get_positions(self, inst_id=None):
        return []

    async def cancel_all(self, inst_id: str) -> dict[str, Any]:
        self.cancels.append(inst_id)
        return {"cancelled": 0, "failed": []}


# Backwards-friendly alias used throughout this module.
MockBybitClient = MockOkxClient


def _verified_guard() -> DemoGuard:
    guard = DemoGuard(OkxDemoClient(), run_mainnet_negative_control=False)
    signals = [
        SignalResult("host pin", True, "ok"),
        SignalResult("demo header enforcement", True, "ok"),
        SignalResult("authenticated reachability", True, "ok"),
        SignalResult("live-environment negative control", True, "ok"),
    ]
    guard._verified = True                       # noqa: SLF001 - test seam
    guard._last_verification = DemoVerification(  # noqa: SLF001
        verified=True, checked_at=now_utc(), signals=tuple(signals)
    )
    return guard


def _unverified_guard() -> DemoGuard:
    return DemoGuard(OkxDemoClient(), run_mainnet_negative_control=False)


def _signal(direction: Direction = Direction.LONG, **overrides) -> StrategySignal:
    base = dict(
        strategy_id="ema_trend_cross_15m",
        strategy_version="1.0",
        direction=direction,
        symbol="BTC-USDT-SWAP",
        timeframe="15",
        bar_open_ms=1_700_000_000_000,
        entry_reference=50_000.0,
        stop_price=49_000.0 if direction is Direction.LONG else 51_000.0,
        target_price=52_000.0 if direction is Direction.LONG else 48_000.0,
        confidence=0.75,
        setup_key="setup_key_1",
        rationale="integration test signal",
        exit_policy=ExitPolicy(
            mechanisms=frozenset({ExitMechanism.FIXED_RR, ExitMechanism.BREAK_EVEN}),
            break_even_at_r=1.0,
        ),
        regime=Regime.TREND_UP,
        regime_confidence=0.8,
    )
    base.update(overrides)
    return StrategySignal(**base)


@pytest.fixture
def capabilities(perp_instrument) -> ExchangeCapabilities:
    return ExchangeCapabilities(base_ccy="BTC", instrument=perp_instrument)


@pytest.fixture
def long_only_capabilities() -> ExchangeCapabilities:
    """A hypothetical long-only product — proves capability is discovered."""
    from dataclasses import replace

    from btcbot.exchange.models import Capability

    from conftest import make_perp_instrument

    crippled = replace(
        make_perp_instrument(),
        capabilities=frozenset({Capability.LONG, Capability.MARKET_ORDER}),
    )
    return ExchangeCapabilities(base_ccy="BTC", instrument=crippled)


# The full perp capabilities double as the "shorts allowed" fixture.
@pytest.fixture
def linear_capabilities(capabilities) -> ExchangeCapabilities:
    return capabilities


def _executor(
    repos,
    client,
    guard,
    *,
    dry_run: bool = False,
    position_mode: PositionMode = PositionMode.NET,
) -> tuple[DemoExecutor, PositionLedger]:
    ledger = PositionLedger(repos.positions, experiment_id="exp_test")
    executor = DemoExecutor(
        client,
        guard=guard,
        breakers=CircuitBreakers(SafetyConfig()),
        sizer=PositionSizer(RiskConfig()),
        leverage_engine=LeverageEngine(LeverageConfig()),
        ledger=ledger,
        safety=OrderSafetyGuard(repos.demo_orders),
        orders=repos.demo_orders,
        leverage_decisions=repos.leverage,
        rejected_signals=repos.rejected,
        system=repos.system,
        risk_config=RiskConfig(),
        experiment_id="exp_test",
        position_mode=position_mode,
        dry_run=dry_run,
    )
    return executor, ledger


async def _submit(executor, signal, capabilities, **overrides):
    kwargs = dict(
        setup_id="set_1",
        signal_id="sig_1",
        capabilities=capabilities,
        equity=10_000.0,
        available=10_000.0,
        atr=500.0,
        expectancy_r=0.3,
        observations=25,
        drawdown_pct=0.0,
        spread_bps=1.0,
        news_size_factor=1.0,
        news_state="calm",
        volatility_pct=0.01,
    )
    kwargs.update(overrides)
    return await executor.submit_entry(signal, **kwargs)


class TestHappyPath:
    async def test_full_entry_records_everything(self, repos, capabilities):
        client = MockBybitClient()
        executor, ledger = _executor(repos, client, _verified_guard())

        result = await _submit(executor, _signal(), capabilities)

        assert result.success, result.reason
        assert len(client.orders) == 1

        request = client.orders[0]
        assert request.side is Side.BUY
        assert request.td_mode is TdMode.ISOLATED, "orders must use isolated margin"
        assert request.pos_side is None, "net mode omits posSide"
        assert "e" not in request.sz.lower(), "quantity used scientific notation"

        # Leverage was SET and CONFIRMED before the order existed.
        assert client.leverage_sets, "no set-leverage call before the entry"
        assert result.leverage_decision is not None
        assert 1.0 <= result.leverage_decision.leverage <= 10.0
        lev_rows = repos.leverage.recent(5)
        assert lev_rows and lev_rows[0]["approved"] == 1
        assert lev_rows[0]["confirmed_by_exchange"] == 1

        # Attribution is fully recorded.
        stored = repos.demo_orders.get(result.client_order_id)
        assert stored["strategy_id"] == "ema_trend_cross_15m"
        assert stored["strategy_version"] == "1.0"
        assert stored["setup_id"] == "set_1"
        assert stored["signal_id"] == "sig_1"
        assert stored["experiment_id"] == "exp_test"
        assert stored["status"] == "accepted"
        assert stored["signal_ts_utc"]
        assert stored["sizing_reasoning"]
        assert stored["td_mode"] == "isolated"
        assert stored["leverage"] and 1.0 <= stored["leverage"] <= 10.0
        assert stored["contracts"] and stored["contracts"] > 0

        # The ledger knows why, who, and when.
        position = ledger.current()
        assert position is not None
        assert position.strategy_id == "ema_trend_cross_15m"
        assert position.reason == "integration test signal"
        assert position.planned_exit
        assert result.sizing.risk_pct_of_equity <= RiskConfig().max_risk_pct

    async def test_exit_closes_the_position_and_books_pnl(self, repos, capabilities):
        client = MockBybitClient()
        executor, ledger = _executor(repos, client, _verified_guard())
        await _submit(executor, _signal(), capabilities)
        position = ledger.current()

        exit_result = await executor.submit_exit(
            position, exit_reason="take_profit", capabilities=capabilities
        )
        assert exit_result.success
        assert len(client.orders) == 2
        assert client.orders[1].side is Side.SELL
        # In net mode a close carries reduceOnly so it can never flip direction.
        assert client.orders[1].reduce_only is True

        trade = ledger.close(
            position, exit_price=52_000.0, exit_reason="take_profit",
            exit_order_id=exit_result.client_order_id, fees=0.5,
        )
        assert trade["pnl"] > 0
        assert math.isclose(trade["r_multiple"], trade["pnl"] / position.initial_risk, rel_tol=1e-9)
        assert not ledger.has_open_position

    async def test_fill_is_recorded_and_idempotent(self, repos, capabilities):
        client = MockBybitClient()
        executor, _ = _executor(repos, client, _verified_guard())
        result = await _submit(executor, _signal(), capabilities)

        execution = Execution(
            exec_id="exec_1", order_id="mock-1", client_order_id=result.client_order_id,
            inst_id="BTC-USDT-SWAP", side=Side.BUY, pos_side="net", price=50_010.0,
            qty=0.1, fee=0.0275, fee_currency="USDT", is_maker=False,
            exec_ts_ms=1_700_000_000_000,
        )
        executor.record_fill(execution)
        executor.record_fill(execution)

        fills = repos.demo_orders.fills_for(result.client_order_id)
        assert len(fills) == 1, "the same execId was stored twice"
        assert repos.demo_orders.get(result.client_order_id)["status"] == "filled"


class TestGatesBlockBeforeAnyRequest:
    async def test_unverified_demo_blocks_without_a_request(self, repos, capabilities):
        client = MockBybitClient()
        executor, ledger = _executor(repos, client, _unverified_guard())

        result = await _submit(executor, _signal(), capabilities)

        assert not result.success
        assert "not verified" in result.reason
        assert client.orders == [], "an order was sent despite failed verification"
        assert not ledger.has_open_position

    async def test_short_on_long_only_product_is_skipped_and_journaled(
        self, repos, long_only_capabilities
    ):
        """Capability is discovered, not assumed — a long-only product sends
        no short order, and the refusal lands in rejected_signals."""
        client = MockOkxClient()
        executor, _ = _executor(repos, client, _verified_guard())

        result = await _submit(executor, _signal(Direction.SHORT), long_only_capabilities)

        assert not result.success
        assert "not supported" in result.reason
        assert client.orders == []
        categories = {e["category"] for e in repos.system.recent_events(10)}
        assert "order_not_sent" in categories
        rejected = repos.rejected.recent(5)
        assert rejected and rejected[0]["layer_name"] == "instrument_capability"

    async def test_short_is_allowed_on_the_perp(self, repos, linear_capabilities):
        """The X-Perp shorts natively — a SHORT entry is a sell order."""
        client = MockOkxClient()
        executor, _ = _executor(repos, client, _verified_guard())

        result = await _submit(executor, _signal(Direction.SHORT), linear_capabilities)

        assert result.success, result.reason
        assert client.orders[0].side is Side.SELL
        assert client.orders[0].reduce_only is None  # entry, not a reduce

    async def test_long_short_mode_carries_pos_side(self, repos, capabilities):
        """In long/short accounts the order pair (side, posSide) is explicit."""
        from btcbot.exchange.models import PosSide

        client = MockOkxClient()
        executor, ledger = _executor(
            repos, client, _verified_guard(), position_mode=PositionMode.LONG_SHORT
        )
        result = await _submit(executor, _signal(Direction.SHORT), capabilities)
        assert result.success, result.reason
        assert client.orders[0].side is Side.SELL
        assert client.orders[0].pos_side is PosSide.SHORT

        position = ledger.current()
        exit_result = await executor.submit_exit(
            position, exit_reason="stop_loss", capabilities=capabilities
        )
        assert exit_result.success
        assert client.orders[1].side is Side.BUY
        assert client.orders[1].pos_side is PosSide.SHORT
        assert client.orders[1].reduce_only is None  # unambiguous from the pair

    async def test_leverage_confirmation_mismatch_blocks_the_order(
        self, repos, capabilities
    ):
        """Set-without-confirm is never trusted: a mismatch aborts the entry."""
        client = MockOkxClient(leverage_confirm_mismatch=True)
        executor, ledger = _executor(repos, client, _verified_guard())

        result = await _submit(executor, _signal(), capabilities)

        assert not result.success
        assert "expected" in result.reason or "leverage" in result.reason.lower()
        assert client.orders == [], "order sent despite unconfirmed leverage"
        assert not ledger.has_open_position
        rejected = repos.rejected.recent(5)
        assert any(r["layer_name"] == "leverage_confirmation" for r in rejected)

    async def test_duplicate_setup_sends_only_one_order(self, repos, capabilities):
        client = MockBybitClient()
        executor, ledger = _executor(repos, client, _verified_guard())

        first = await _submit(executor, _signal(), capabilities)
        assert first.success
        ledger._open.clear()          # noqa: SLF001 - simulate a stale in-memory view

        second = await _submit(executor, _signal(), capabilities)
        assert not second.success
        assert len(client.orders) == 1, "a duplicate order reached the exchange"

    async def test_safe_mode_blocks_without_a_request(self, repos, capabilities):
        client = MockBybitClient()
        executor, _ = _executor(repos, client, _verified_guard())
        executor.breakers.check_price(0.0)   # trip a breaker

        result = await _submit(executor, _signal(), capabilities)
        assert not result.success
        assert "SAFE_MODE" in result.reason
        assert client.orders == []

    async def test_impossible_sizing_blocks_without_a_request(self, repos, capabilities):
        client = MockBybitClient()
        executor, _ = _executor(repos, client, _verified_guard())

        # A $10 stop on a $50,000 price is 0.02% — below both the percentage
        # floor and 0.15x ATR, and would demand an enormous position.
        result = await _submit(
            executor, _signal(stop_price=49_990.0), capabilities
        )
        assert not result.success
        assert "sizing rejected" in result.reason
        assert client.orders == []

    async def test_dry_run_validates_but_never_submits(self, repos, capabilities):
        client = MockBybitClient()
        executor, ledger = _executor(repos, client, _verified_guard(), dry_run=True)

        result = await _submit(executor, _signal(), capabilities)

        assert not result.success
        assert result.reason == "dry_run"
        assert client.orders == [], "dry run submitted a real order"
        assert not ledger.has_open_position
        # It still validated far enough to produce a size.
        assert result.sizing is not None and result.sizing.approved

    async def test_api_rejection_is_recorded_not_retried_blindly(self, repos, capabilities):
        client = MockOkxClient(fail_with=ApiError(51008, "Insufficient balance", "/api/v5/trade/order"))
        executor, ledger = _executor(repos, client, _verified_guard())

        result = await _submit(executor, _signal(), capabilities)

        assert not result.success
        assert "Insufficient balance" in result.reason
        assert not ledger.has_open_position
        stored = repos.demo_orders.get(result.client_order_id)
        assert stored["status"] == "rejected"
        assert "51008" in stored["reject_reason"]

    async def test_transport_failure_leaves_the_order_for_reconciliation(
        self, repos, capabilities
    ):
        """An ambiguous send must not be blindly retried into a second position."""
        from btcbot.utils.errors import TransportError

        client = MockOkxClient(fail_with=TransportError("connection reset"))
        executor, ledger = _executor(repos, client, _verified_guard())

        result = await _submit(executor, _signal(), capabilities)

        assert not result.success
        assert not ledger.has_open_position
        stored = repos.demo_orders.get(result.client_order_id)
        assert stored["status"] == "submitted", "ambiguous order must await reconciliation"
        assert stored["client_order_id"] in {o["client_order_id"] for o in repos.demo_orders.in_flight()}


class TestAllocatorIntegration:
    def _allocator(self, repos, **overrides) -> DemoAllocator:
        config = AllocatorConfig(
            cooldown_seconds_per_strategy=0, global_cooldown_seconds=0, **overrides
        )
        allocator = DemoAllocator(
            config, repos.allocator, repos.demo_orders, experiment_id="exp_test"
        )
        allocator.initialise(["s1", "s2", "s3"])
        return allocator

    def test_no_allocation_when_a_position_is_open(self, repos):
        allocator = self._allocator(repos)
        decision = allocator.allocate(
            [("s1", _signal())], regime="TREND_UP", position_open=True
        )
        assert not decision.granted
        assert "already open" in decision.reason

    def test_no_allocation_without_candidates(self, repos):
        allocator = self._allocator(repos)
        decision = allocator.allocate([], regime="TREND_UP", position_open=False)
        assert not decision.granted

    def test_low_confidence_candidates_are_filtered(self, repos):
        allocator = self._allocator(repos, min_signal_confidence=0.9)
        decision = allocator.allocate(
            [("s1", _signal(confidence=0.2))], regime="TREND_UP", position_open=False
        )
        assert not decision.granted
        assert "confidence threshold" in decision.reason

    def test_exactly_one_strategy_is_chosen(self, repos):
        allocator = self._allocator(repos)
        candidates = [("s1", _signal()), ("s2", _signal()), ("s3", _signal())]
        decision = allocator.allocate(candidates, regime="TREND_UP", position_open=False)
        assert decision.granted
        assert decision.strategy_id in {"s1", "s2", "s3"}
        assert decision.signal is not None

    def test_early_winner_cannot_monopolise(self, repos):
        """The brief's requirement: no lucky strategy takes over the account."""
        allocator = self._allocator(repos, forced_exploration_ratio=0.3)
        # s1 looks fantastic; s2 and s3 have no record at all.
        for _ in range(30):
            allocator.record_result("s1", 3.0)

        chosen: dict[str, int] = {}
        for _ in range(120):
            decision = allocator.allocate(
                [("s1", _signal()), ("s2", _signal()), ("s3", _signal())],
                regime="TREND_UP",
                position_open=False,
            )
            if decision.granted and decision.strategy_id:
                chosen[decision.strategy_id] = chosen.get(decision.strategy_id, 0) + 1
                allocator.confirm_allocation(decision.strategy_id)

        assert set(chosen) >= {"s2", "s3"}, f"under-sampled strategies were starved: {chosen}"
        assert chosen.get("s1", 0) < sum(chosen.values()), "one strategy took every slot"

    def test_evidence_from_other_layers_forms_the_prior(self, repos):
        allocator = self._allocator(repos)
        allocator.update_evidence(
            shadow={"s1": (1.2, 40), "s2": (-0.8, 40)},
            historical={"s1": (0.9, 100), "s2": (-0.5, 100)},
        )
        assert allocator.arms["s1"].prior_mean > 0
        assert allocator.arms["s2"].prior_mean < 0
        assert allocator.arms["s1"].prior_strength > 1.0

    def test_regime_fitness_influences_selection(self, repos):
        allocator = self._allocator(repos, forced_exploration_ratio=0.0)
        for arm in allocator.arms.values():
            for _ in range(20):
                allocator.record_result(arm.strategy_id, 0.0)
        allocator.update_evidence(
            regime_fitness={"s1": {"RANGING": 1.0}, "s2": {"TREND_UP": 1.0}}
        )
        picks = {"s1": 0, "s2": 0, "s3": 0}
        for _ in range(60):
            decision = allocator.allocate(
                [("s1", _signal()), ("s2", _signal()), ("s3", _signal())],
                regime="RANGING",
                position_open=False,
            )
            if decision.granted and decision.strategy_id:
                picks[decision.strategy_id] += 1
        assert picks["s1"] > picks["s2"], f"regime fitness was ignored: {picks}"

    def test_hourly_rate_limit_is_enforced(self, repos):
        allocator = self._allocator(repos, max_demo_orders_per_hour=1)
        order = {
            "client_order_id": "b_rate_1", "experiment_id": "exp_test", "signal_id": None,
            "setup_id": "set_rate", "strategy_id": "s1", "strategy_version": "1.0",
            "signal_ts_utc": "2026-07-24T12:00:00Z",
            "submitted_ts_utc": now_utc().isoformat().replace("+00:00", "Z"),
            "symbol": "BTC-USDT-SWAP", "category": "SWAP", "side": "buy", "order_type": "market",
            "intent": "entry", "quantity": 0.001, "quantity_str": "0.001", "price": None,
            "estimated_notional": 50.0, "stop_price": 49_000.0, "target_price": 52_000.0,
            "estimated_risk_pct": 0.0075, "regime": "TREND_UP", "confidence": 0.7,
            "sizing_reasoning": "x",
        }
        repos.demo_orders.reserve(order)
        decision = allocator.allocate(
            [("s2", _signal())], regime="TREND_UP", position_open=False
        )
        assert not decision.granted
        assert "limit reached" in decision.reason

    def test_cooldown_blocks_a_repeat_allocation(self, repos):
        allocator = DemoAllocator(
            AllocatorConfig(cooldown_seconds_per_strategy=900, global_cooldown_seconds=0),
            repos.allocator, repos.demo_orders, experiment_id="exp_test",
        )
        allocator.initialise(["s1"])
        first = allocator.allocate([("s1", _signal())], regime="TREND_UP", position_open=False)
        assert first.granted
        allocator.confirm_allocation("s1")

        second = allocator.allocate([("s1", _signal())], regime="TREND_UP", position_open=False)
        assert not second.granted
        assert "cooldown" in second.reason

    def test_fairness_metric_detects_monopoly(self, repos):
        allocator = self._allocator(repos)
        for _ in range(50):
            allocator.confirm_allocation("s1")
        assert allocator.allocation_fairness() < 0.5

        balanced = self._allocator(repos)
        for strategy_id in ("s1", "s2", "s3"):
            for _ in range(10):
                balanced.confirm_allocation(strategy_id)
        assert balanced.allocation_fairness() > 0.9


class TestTradeManagement:
    def _open_position(self, repos, capabilities):
        ledger = PositionLedger(repos.positions, experiment_id="exp_test")
        signal = _signal()
        position = ledger.open(
            signal=signal, setup_id="set_1", signal_id="sig_1", category="SWAP",
            entry_price=50_000.0, quantity=0.01, entry_order_id="o1",
            news_state="calm", atr=500.0, leverage=2.0, contracts=1.0,
            liq_price_at_entry=42_000.0,
        )
        return ledger, position

    def test_stop_triggers_an_exit(self, repos, capabilities):
        ledger, position = self._open_position(repos, capabilities)
        decision = TradeManager(ledger).evaluate(position, price=48_900.0)
        assert decision.should_exit
        assert decision.reason == "stop_loss"

    def test_target_triggers_an_exit(self, repos, capabilities):
        ledger, position = self._open_position(repos, capabilities)
        decision = TradeManager(ledger).evaluate(position, price=52_100.0)
        assert decision.should_exit
        assert decision.reason == "take_profit"

    def test_no_exit_inside_the_range(self, repos, capabilities):
        ledger, position = self._open_position(repos, capabilities)
        assert not TradeManager(ledger).evaluate(position, price=50_500.0).should_exit

    def test_break_even_moves_the_stop(self, repos, capabilities):
        ledger, position = self._open_position(repos, capabilities)
        manager = TradeManager(ledger)
        decision = manager.evaluate(position, price=51_000.0)   # +1R
        assert not decision.should_exit
        assert decision.stop_moved_to == position.entry_price
        assert position.break_even_applied
        # Now a fall back to entry exits at break-even, not a loss.
        assert manager.evaluate(position, price=49_999.0).reason == "break_even"

    def test_stale_data_holds_rather_than_exiting_on_a_bad_price(self, repos, capabilities):
        ledger, position = self._open_position(repos, capabilities)
        decision = TradeManager(ledger).stale_data_action(position)
        assert not decision.should_exit
        assert decision.reason == "stale_data_hold"

    def test_finalisation_policy_is_respected(self, repos, capabilities):
        ledger, position = self._open_position(repos, capabilities)
        manager = TradeManager(ledger)
        assert manager.finalization_exit(position, "close_at_end").should_exit
        assert not manager.finalization_exit(position, "manage_to_exit").should_exit

    def test_excursions_are_tracked(self, repos, capabilities):
        ledger, position = self._open_position(repos, capabilities)
        manager = TradeManager(ledger)
        manager.evaluate(position, price=51_500.0)
        manager.evaluate(position, price=49_500.0)
        assert position.mfe > 0
        assert position.mae < 0


class TestShadowEngineIntegration:
    async def test_signal_opens_an_isolated_shadow_position(self, repos):
        from btcbot.shadow.engine import ShadowEngine
        from btcbot.strategies.trend import EmaTrendCross

        engine = ShadowEngine(
            ShadowConfig(), repos.shadow, experiment_id="exp_test", symbol="BTC-USDT-SWAP"
        )
        strategy = EmaTrendCross()
        engine.initialise([strategy.id, "other"])

        assert engine.on_signal(strategy, _signal(), atr=500.0, news_state="calm") is True
        assert engine.accounts[strategy.id].has_position
        assert not engine.accounts["other"].has_position

        # One position per strategy at a time.
        assert engine.on_signal(strategy, _signal(), atr=500.0) is False

    async def test_shadow_trade_closes_and_persists(self, repos):
        from btcbot.exchange.models import Candle
        from btcbot.shadow.engine import ShadowEngine
        from btcbot.strategies.trend import EmaTrendCross

        engine = ShadowEngine(
            ShadowConfig(), repos.shadow, experiment_id="exp_test", symbol="BTC-USDT-SWAP"
        )
        strategy = EmaTrendCross()
        engine.initialise([strategy.id])
        engine.on_signal(strategy, _signal(), atr=500.0, news_state="calm")

        winner = Candle(
            open_ms=1_700_000_900_000, open=51_000.0, high=52_500.0, low=50_900.0,
            close=52_200.0, volume=10.0, turnover=0.0, timeframe="15", confirmed=True,
        )
        closed = engine.on_bar(winner, atr_by_timeframe={"15": 500.0})

        assert len(closed) == 1
        trade = closed[0]
        assert trade["strategy_id"] == strategy.id
        assert trade["pnl"] > 0
        assert trade["exit_reason"] == "take_profit"

        stored = repos.shadow.closed_trades(experiment_id="exp_test")
        assert len(stored) == 1
        assert stored[0]["is_open"] == 0
        assert engine.accounts[strategy.id].equity > 10_000.0

    async def test_force_close_flattens_everything(self, repos):
        from btcbot.shadow.engine import ShadowEngine
        from btcbot.strategies.trend import EmaTrendCross

        engine = ShadowEngine(
            ShadowConfig(), repos.shadow, experiment_id="exp_test", symbol="BTC-USDT-SWAP"
        )
        strategy = EmaTrendCross()
        engine.initialise([strategy.id])
        engine.on_signal(strategy, _signal(), atr=500.0)
        assert engine.open_position_count() == 1

        closed = engine.force_close_all(50_500.0)
        assert len(closed) == 1
        assert engine.open_position_count() == 0
        assert closed[0]["exit_reason"] == "experiment_end"
