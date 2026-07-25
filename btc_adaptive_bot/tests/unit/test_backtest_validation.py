"""Deterministic backtest validation.

The brief asks for a fixture whose expected trades and PnL are known **in
advance**, proving the backtester produces the right answer rather than merely
running.

A synthetic strategy with a fixed entry bar, a fixed stop, and a fixed target is
replayed over hand-built candles. The expected outcome is computed here by hand
from the documented execution model, then compared with the engine's output.
"""

from __future__ import annotations

import math

from btcbot.backtesting.engine import Backtester
from btcbot.backtesting.execution_model import (
    ExecutionCosts,
    ExecutionModel,
    ExitReason,
    PositionManager,
    SimulatedPosition,
)
from btcbot.config.schema import RegimeConfig
from btcbot.exchange.models import Candle
from btcbot.strategies.base import (
    Direction,
    ExitMechanism,
    ExitPolicy,
    SetupProposal,
    Strategy,
    StrategyCategory,
    StrategyContext,
)
from tests.conftest import make_candles

ZERO_COSTS = ExecutionCosts(
    fee_rate_taker=0.0,
    fee_rate_maker=0.0,
    slippage_bps=0.0,
    spread_bps=0.0,
    latency_ms=0,
    partial_fill_probability=0.0,   # deterministic: no partial fills
)


class TestExecutionModelArithmetic:
    """Hand-checked cost arithmetic — the foundation everything else builds on."""

    def test_entry_price_includes_half_spread_and_slippage(self):
        costs = ExecutionCosts(spread_bps=2.0, slippage_bps=3.0)
        model = ExecutionModel(costs)
        # Long pays up: 1.0 bps (half of 2) + 3.0 bps = 4 bps = 0.04%.
        price = model.entry_price(Direction.LONG, 50_000.0)
        assert math.isclose(price, 50_000.0 * 1.0004, rel_tol=1e-12)

        # Short receives less: same 4 bps adverse, opposite sign.
        price = model.entry_price(Direction.SHORT, 50_000.0)
        assert math.isclose(price, 50_000.0 * 0.9996, rel_tol=1e-12)

    def test_exit_price_is_adverse_in_the_opposite_sense(self):
        model = ExecutionModel(ExecutionCosts(spread_bps=2.0, slippage_bps=3.0))
        assert math.isclose(
            model.exit_price(Direction.LONG, 50_000.0), 50_000.0 * 0.9996, rel_tol=1e-12
        )
        assert math.isclose(
            model.exit_price(Direction.SHORT, 50_000.0), 50_000.0 * 1.0004, rel_tol=1e-12
        )

    def test_taker_fee_is_exact(self):
        model = ExecutionModel(ExecutionCosts(fee_rate_taker=0.00055))
        # 0.055% of a $10,000 notional = $5.50.
        assert math.isclose(model.fee(10_000.0), 5.50, rel_tol=1e-12)

    def test_maker_fee_is_used_when_requested(self):
        model = ExecutionModel(ExecutionCosts(fee_rate_maker=0.0002))
        assert math.isclose(model.fee(10_000.0, is_maker=True), 2.00, rel_tol=1e-12)

    def test_stress_multiplier_scales_frictions_not_latency(self):
        costs = ExecutionCosts(
            fee_rate_taker=0.001, slippage_bps=2.0, spread_bps=1.0, latency_ms=250
        )
        stressed = costs.stressed(4.0)
        assert stressed.fee_rate_taker == 0.004
        assert stressed.slippage_bps == 8.0
        assert stressed.spread_bps == 4.0
        assert stressed.latency_ms == 250

    def test_partial_fill_never_exceeds_request(self):
        model = ExecutionModel(ExecutionCosts(partial_fill_probability=1.0))
        for _ in range(200):
            assert model.fill_quantity(1.0) <= 1.0


class TestKnownPnl:
    """Positions with hand-computed outcomes."""

    def _position(self, **overrides) -> SimulatedPosition:
        base = dict(
            strategy_id="fixture",
            strategy_version="1.0",
            direction=Direction.LONG,
            symbol="BTCUSDT",
            timeframe="5",
            entry_price=50_000.0,
            initial_stop=49_000.0,
            stop_price=49_000.0,
            target_price=52_000.0,
            quantity=0.1,
            remaining_quantity=0.1,
            entry_bar_ms=0,
            entry_index=0,
            exit_policy=ExitPolicy(mechanisms=frozenset({ExitMechanism.FIXED_RR})),
            atr_at_entry=500.0,
            confidence=0.6,
            entry_regime="TREND_UP",
        )
        base.update(overrides)
        return SimulatedPosition(**base)

    def test_target_hit_produces_exact_expected_profit(self):
        """0.1 BTC from 50,000 to 52,000 with zero costs = +$200.00, exactly +2R."""
        manager = PositionManager(ExecutionModel(ZERO_COSTS))
        position = self._position()
        bar = Candle(
            open_ms=1000, open=51_000.0, high=52_500.0, low=50_900.0, close=52_100.0,
            volume=10.0, turnover=0.0, timeframe="5", confirmed=True,
        )

        events = manager.process_bar(position, bar)
        assert len(events) == 1
        assert events[0].reason is ExitReason.TAKE_PROFIT

        pnl = manager.apply_exit(position, events[0])
        assert math.isclose(pnl, 200.0, rel_tol=1e-9), f"expected +$200.00, got {pnl}"
        # Risk was 1,000 × 0.1 = $100 ⇒ +2.00R.
        assert math.isclose(position.r_multiple(pnl), 2.0, rel_tol=1e-9)

    def test_stop_hit_produces_exact_expected_loss(self):
        """0.1 BTC from 50,000 to 49,000 = -$100.00, exactly -1R."""
        manager = PositionManager(ExecutionModel(ZERO_COSTS))
        position = self._position()
        bar = Candle(
            open_ms=1000, open=49_800.0, high=49_900.0, low=48_500.0, close=48_700.0,
            volume=10.0, turnover=0.0, timeframe="5", confirmed=True,
        )

        events = manager.process_bar(position, bar)
        assert events[0].reason is ExitReason.STOP_LOSS
        pnl = manager.apply_exit(position, events[0])
        assert math.isclose(pnl, -100.0, rel_tol=1e-9), f"expected -$100.00, got {pnl}"
        assert math.isclose(position.r_multiple(pnl), -1.0, rel_tol=1e-9)

    def test_stop_wins_when_a_bar_contains_both_levels(self):
        """Ambiguous bars must resolve pessimistically — the stop, not the target."""
        manager = PositionManager(ExecutionModel(ZERO_COSTS))
        position = self._position()
        bar = Candle(
            open_ms=1000, open=50_100.0, high=52_500.0, low=48_900.0, close=51_000.0,
            volume=10.0, turnover=0.0, timeframe="5", confirmed=True,
        )
        events = manager.process_bar(position, bar)
        assert events[0].reason is ExitReason.STOP_LOSS

    def test_gap_through_stop_fills_at_the_open(self):
        """A bar opening below the stop fills there, not at the stop price."""
        manager = PositionManager(ExecutionModel(ZERO_COSTS))
        position = self._position()
        bar = Candle(
            open_ms=1000, open=48_000.0, high=48_200.0, low=47_500.0, close=47_800.0,
            volume=10.0, turnover=0.0, timeframe="5", confirmed=True,
        )
        events = manager.process_bar(position, bar)
        assert math.isclose(events[0].price, 48_000.0)
        pnl = manager.apply_exit(position, events[0])
        # 0.1 × (48,000 - 50,000) = -$200 — worse than 1R, which is correct.
        assert math.isclose(pnl, -200.0, rel_tol=1e-9)

    def test_short_target_produces_exact_expected_profit(self):
        manager = PositionManager(ExecutionModel(ZERO_COSTS))
        position = self._position(
            direction=Direction.SHORT,
            initial_stop=51_000.0,
            stop_price=51_000.0,
            target_price=48_000.0,
        )
        bar = Candle(
            open_ms=1000, open=49_500.0, high=49_600.0, low=47_900.0, close=48_100.0,
            volume=10.0, turnover=0.0, timeframe="5", confirmed=True,
        )
        events = manager.process_bar(position, bar)
        assert events[0].reason is ExitReason.TAKE_PROFIT
        pnl = manager.apply_exit(position, events[0])
        # 0.1 × (50,000 - 48,000) = +$200.
        assert math.isclose(pnl, 200.0, rel_tol=1e-9)

    def test_fees_reduce_pnl_by_the_exact_amount(self):
        """Same winning trade, but with a known taker fee on the exit leg."""
        costs = ExecutionCosts(
            fee_rate_taker=0.001, slippage_bps=0.0, spread_bps=0.0,
            partial_fill_probability=0.0,
        )
        manager = PositionManager(ExecutionModel(costs))
        position = self._position()
        bar = Candle(
            open_ms=1000, open=51_000.0, high=52_500.0, low=50_900.0, close=52_100.0,
            volume=10.0, turnover=0.0, timeframe="5", confirmed=True,
        )
        events = manager.process_bar(position, bar)
        pnl = manager.apply_exit(position, events[0])
        # Gross +$200; exit fee = 0.1% of (52,000 × 0.1) = $5.20 ⇒ +$194.80.
        assert math.isclose(pnl, 194.80, rel_tol=1e-9), f"expected +$194.80, got {pnl}"

    def test_time_stop_exits_at_the_close(self):
        manager = PositionManager(ExecutionModel(ZERO_COSTS))
        position = self._position(
            exit_policy=ExitPolicy(
                mechanisms=frozenset({ExitMechanism.TIME_STOP}), time_stop_bars=2
            ),
            target_price=None,
        )
        quiet = Candle(
            open_ms=1000, open=50_050.0, high=50_100.0, low=49_950.0, close=50_050.0,
            volume=10.0, turnover=0.0, timeframe="5", confirmed=True,
        )
        assert manager.process_bar(position, quiet) == []
        events = manager.process_bar(position, quiet)
        assert events and events[0].reason is ExitReason.TIME_STOP
        pnl = manager.apply_exit(position, events[0])
        # 0.1 × (50,050 - 50,000) = +$5.00.
        assert math.isclose(pnl, 5.0, rel_tol=1e-9)

    def test_break_even_moves_stop_to_entry(self):
        manager = PositionManager(ExecutionModel(ZERO_COSTS))
        position = self._position(
            exit_policy=ExitPolicy(
                mechanisms=frozenset({ExitMechanism.BREAK_EVEN}),
                break_even_at_r=1.0,
            ),
            target_price=None,
        )
        # Close at 51,000 = +1R (risk is 1,000/unit).
        bar = Candle(
            open_ms=1000, open=50_100.0, high=51_100.0, low=50_050.0, close=51_000.0,
            volume=10.0, turnover=0.0, timeframe="5", confirmed=True,
        )
        manager.process_bar(position, bar)
        assert position.break_even_applied
        assert math.isclose(position.stop_price, 50_000.0)

    def test_partial_exit_realises_half_and_keeps_the_rest(self):
        manager = PositionManager(ExecutionModel(ZERO_COSTS))
        position = self._position(
            exit_policy=ExitPolicy(
                mechanisms=frozenset({ExitMechanism.PARTIAL_EXIT}),
                partial_at_r=1.0,
                partial_fraction=0.5,
            ),
            target_price=None,
        )
        bar = Candle(
            open_ms=1000, open=50_100.0, high=51_100.0, low=50_050.0, close=51_000.0,
            volume=10.0, turnover=0.0, timeframe="5", confirmed=True,
        )
        events = manager.process_bar(position, bar)
        assert len(events) == 1 and events[0].is_partial
        pnl = manager.apply_exit(position, events[0])
        # 0.05 BTC × 1,000 = +$50, with 0.05 BTC still open.
        assert math.isclose(pnl, 50.0, rel_tol=1e-9)
        assert math.isclose(position.remaining_quantity, 0.05, rel_tol=1e-9)


class _FixedEntryStrategy(Strategy):
    """Enters LONG once, on a specific bar index, with fixed levels."""

    id = "fixture_fixed_entry"
    name = "Fixture: fixed entry"
    version = "1.0"
    category = StrategyCategory.TREND
    primary_timeframe = "5"
    min_bars = 210
    exit_mechanisms = frozenset({ExitMechanism.FIXED_RR})

    def __init__(self, *, entry_bar_ms: int, stop: float, target: float) -> None:
        super().__init__()
        self._entry_bar_ms = entry_bar_ms
        self._stop = stop
        self._target = target
        self.fired = 0

    @classmethod
    def default_params(cls):
        return {}

    def allowed_regimes(self):
        return frozenset()

    def detect(self, ctx: StrategyContext, features) -> SetupProposal | None:
        if features.bar_open_ms != self._entry_bar_ms:
            return None
        self.fired += 1
        return SetupProposal(
            direction=Direction.LONG,
            entry_reference=features.close,
            setup_key="fixture",
            rationale="fixture entry",
            raw_confidence=0.9,
            stop_hint=self._stop,
            target_hint=self._target,
        )

    def calculate_confidence(self, ctx, features, proposal) -> float:
        return 0.9   # constant, so the test is unaffected by context scoring


class TestBacktesterEndToEnd:
    """Full replay with a known expected trade."""

    def test_single_known_trade_matches_hand_calculation(self):
        # 260 flat-ish bars, then a rise to the target after the entry bar.
        closes = [50_000.0] * 260
        for i in range(260, 300):
            closes.append(50_000.0 + (i - 259) * 60.0)   # reaches 52,400
        candles = make_candles(closes, timeframe="5", high_pad=30.0, low_pad=30.0)

        entry_bar = candles[250]
        strategy = _FixedEntryStrategy(
            entry_bar_ms=entry_bar.open_ms, stop=49_000.0, target=52_000.0
        )

        backtester = Backtester(
            costs=ZERO_COSTS,
            regime_config=RegimeConfig(),
            initial_equity=10_000.0,
            risk_pct=0.01,
        )
        result = backtester.run(strategy, {"5": candles}, symbol="BTCUSDT")

        assert strategy.fired == 1, "the fixture strategy must fire exactly once"
        assert result.trade_count == 1, f"expected exactly 1 trade, got {result.trade_count}"

        trade = result.trades[0]
        # Entry fills at the NEXT bar's open (no look-ahead), which is 50,000.
        assert math.isclose(trade.entry_price, 50_000.0, rel_tol=1e-9)
        # Risk 1% of $10,000 = $100; stop distance $1,000 ⇒ 0.1 BTC.
        assert math.isclose(trade.quantity, 0.1, rel_tol=1e-6)
        assert trade.exit_reason == ExitReason.TAKE_PROFIT.value
        assert math.isclose(trade.exit_price, 52_000.0, rel_tol=1e-9)
        # 0.1 × (52,000 - 50,000) = +$200.00 exactly.
        assert math.isclose(trade.pnl, 200.0, rel_tol=1e-6), f"expected +$200.00, got {trade.pnl}"
        assert math.isclose(trade.r_multiple, 2.0, rel_tol=1e-6)
        assert math.isclose(result.final_equity, 10_200.0, rel_tol=1e-6)

    def test_known_losing_trade_matches_hand_calculation(self):
        closes = [50_000.0] * 260
        for i in range(260, 300):
            closes.append(50_000.0 - (i - 259) * 60.0)   # falls to 47,600
        candles = make_candles(closes, timeframe="5", high_pad=30.0, low_pad=30.0)

        strategy = _FixedEntryStrategy(
            entry_bar_ms=candles[250].open_ms, stop=49_000.0, target=52_000.0
        )
        backtester = Backtester(
            costs=ZERO_COSTS, regime_config=RegimeConfig(),
            initial_equity=10_000.0, risk_pct=0.01,
        )
        result = backtester.run(strategy, {"5": candles}, symbol="BTCUSDT")

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.STOP_LOSS.value
        assert math.isclose(trade.pnl, -100.0, rel_tol=1e-6), f"expected -$100.00, got {trade.pnl}"
        assert math.isclose(trade.r_multiple, -1.0, rel_tol=1e-6)
        assert math.isclose(result.final_equity, 9_900.0, rel_tol=1e-6)

    def test_costs_reduce_the_known_result_predictably(self):
        closes = [50_000.0] * 260 + [50_000.0 + (i - 259) * 60.0 for i in range(260, 300)]
        candles = make_candles(closes, timeframe="5", high_pad=30.0, low_pad=30.0)
        strategy = _FixedEntryStrategy(
            entry_bar_ms=candles[250].open_ms, stop=49_000.0, target=52_000.0
        )

        with_costs = Backtester(
            costs=ExecutionCosts(
                fee_rate_taker=0.00055, slippage_bps=2.0, spread_bps=1.0,
                partial_fill_probability=0.0,
            ),
            regime_config=RegimeConfig(),
            initial_equity=10_000.0,
            risk_pct=0.01,
        ).run(strategy, {"5": candles}, symbol="BTCUSDT")

        assert with_costs.trade_count == 1
        trade = with_costs.trades[0]
        # Still a winner, but strictly less than the frictionless $200.
        assert 0 < trade.pnl < 200.0
        assert trade.fees > 0
        assert trade.slippage_cost > 0

    def test_no_trades_when_the_strategy_never_fires(self):
        candles = make_candles([50_000.0] * 300, timeframe="5")
        strategy = _FixedEntryStrategy(entry_bar_ms=-1, stop=49_000.0, target=52_000.0)
        result = Backtester(
            costs=ZERO_COSTS, regime_config=RegimeConfig()
        ).run(strategy, {"5": candles}, symbol="BTCUSDT")
        assert result.trade_count == 0
        assert math.isclose(result.final_equity, 10_000.0)

    def test_open_position_is_closed_at_end_of_data(self):
        """Leaving a trade open would silently exclude it from the record."""
        closes = [50_000.0] * 260 + [50_000.0 + (i - 259) * 5.0 for i in range(260, 275)]
        candles = make_candles(closes, timeframe="5", high_pad=5.0, low_pad=5.0)
        strategy = _FixedEntryStrategy(
            entry_bar_ms=candles[262].open_ms, stop=49_000.0, target=99_000.0
        )
        result = Backtester(
            costs=ZERO_COSTS, regime_config=RegimeConfig()
        ).run(strategy, {"5": candles}, symbol="BTCUSDT")
        assert result.trade_count == 1
        assert result.trades[0].exit_reason == ExitReason.END_OF_DATA.value
