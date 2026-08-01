"""Exploration may only ever draw from actual-eligible candidates.

The allocator's job is to balance exploitation against exploration, and
exploration deliberately favours strategies with few observations. That is
correct — but it was drawing from a pool that included setups the executor
would certainly refuse, so the exploration budget was being spent on trades
that could never happen, and each one was announced as "allocated actual Demo
trade" first.

The fix is structural rather than a new rule inside the allocator: ineligible
candidates are removed from the pool *before* `allocate` is called, so there is
nothing ineligible left to draw. These tests assert that from both directions —
the filter removes them, and the allocator cannot return one even when
exploration is forced to fire on every call.
"""

from __future__ import annotations

import pytest

from btcbot.config.schema import ActualEligibilityConfig, AllocatorConfig, RiskConfig
from btcbot.execution.allocator import DemoAllocator
from btcbot.execution.eligibility import ActualTradeEligibility, TradeCosts
from btcbot.regime.classifier import Regime
from btcbot.strategies.base import Direction, ExitMechanism, ExitPolicy, StrategySignal

pytestmark = pytest.mark.integration

ENTRY = 100_000.0
DEMO_TAKER = 0.0025          # OKX Demo: 0.25% per side

MICRO = "trade_flow_imbalance_1m"
BOOK = "orderbook_imbalance_1m"
WIDE = "ema_trend_cross_15m"
HOUR = "donchian_breakout_1h"


def costs(spread_bps: float = 2.0) -> TradeCosts:
    return TradeCosts(
        taker_fee_rate=DEMO_TAKER, spread_bps=spread_bps,
        slippage_bps=2.0, source="exchange",
    )


def signal(strategy_id: str, *, stop_pct: float, target_pct: float,
           timeframe: str = "15") -> StrategySignal:
    return StrategySignal(
        strategy_id=strategy_id,
        strategy_version="1.0",
        direction=Direction.LONG,
        symbol="BTC-USDT-SWAP",
        timeframe=timeframe,
        bar_open_ms=1_700_000_000_000,
        entry_reference=ENTRY,
        stop_price=ENTRY * (1 - stop_pct),
        target_price=ENTRY * (1 + target_pct),
        confidence=0.75,
        setup_key=f"k_{strategy_id}",
        rationale="test",
        exit_policy=ExitPolicy(mechanisms=frozenset({ExitMechanism.FIXED_RR})),
        regime=Regime.TREND_UP,
        regime_confidence=0.8,
    )


def micro_candidate(strategy_id: str = MICRO):
    """The shape from the live logs: a 0.04% stop and a 0.06% target."""
    return (strategy_id, signal(strategy_id, stop_pct=0.0004, target_pct=0.0006,
                                timeframe="1"))


def wide_candidate(strategy_id: str = WIDE):
    """A setup whose target genuinely clears the cost of trading it."""
    return (strategy_id, signal(strategy_id, stop_pct=0.005, target_pct=0.025))


class StubOrderRepo:
    """The allocator only asks this for rate-limit counts."""

    def count_since(self, experiment_id, since):
        return 0


class StubAllocatorRepo:
    def load(self, experiment_id):
        return {}

    def upsert(self, row):
        pass


def allocator(strategy_ids, *, always_explore: bool = False) -> DemoAllocator:
    config = AllocatorConfig(
        # No cooldowns: this is about *which* candidate is chosen, not when.
        cooldown_seconds_per_strategy=0,
        global_cooldown_seconds=0,
        # Force exploration on every call, so the exploration path is the one
        # under test rather than an occasional branch.
        forced_exploration_ratio=1.0 if always_explore else 0.25,
    )
    alloc = DemoAllocator(
        config, StubAllocatorRepo(), StubOrderRepo(), experiment_id="exp_test", seed=7
    )
    alloc.initialise(list(strategy_ids))
    return alloc


def eligibility(**overrides) -> ActualTradeEligibility:
    return ActualTradeEligibility(
        ActualEligibilityConfig(**overrides), RiskConfig()
    )


def allocate(alloc, candidates):
    return alloc.allocate(candidates, regime="TREND_UP", position_open=False)


class TestExplorationCannotSelectAnIneligibleSetup:
    def test_the_pool_offered_to_the_allocator_holds_only_eligible_setups(self):
        candidates = [micro_candidate(), wide_candidate(), micro_candidate(BOOK)]

        eligible, _ = eligibility().filter(candidates, costs=costs())

        assert [sid for sid, _ in eligible] == [WIDE]

    def test_forced_exploration_still_cannot_reach_a_micro_setup(self):
        """Exploration fires on every call here, and still cannot pick one."""
        candidates = [micro_candidate(), micro_candidate(BOOK), wide_candidate()]
        alloc = allocator([MICRO, BOOK, WIDE], always_explore=True)

        eligible, _ = eligibility().filter(candidates, costs=costs())
        decisions = [allocate(alloc, eligible) for _ in range(25)]

        chosen = {d.strategy_id for d in decisions if d.granted}
        assert chosen == {WIDE}, chosen
        assert any(d.exploration for d in decisions), "exploration never fired"

    def test_an_all_ineligible_bar_allocates_nothing_at_all(self):
        """No eligible candidate means no allocation — not a fallback pick."""
        candidates = [micro_candidate(), micro_candidate(BOOK)]
        alloc = allocator([MICRO, BOOK], always_explore=True)

        eligible, verdicts = eligibility().filter(candidates, costs=costs())
        assert eligible == []

        decision = allocate(alloc, eligible)
        assert not decision.granted
        assert decision.strategy_id is None
        assert all(not v.eligible for v in verdicts)

    def test_the_allocator_is_never_told_about_the_rejected_candidates(self):
        """It cannot score what it cannot see — that is the whole mechanism."""
        candidates = [micro_candidate(), wide_candidate()]
        alloc = allocator([MICRO, WIDE], always_explore=True)

        eligible, _ = eligibility().filter(candidates, costs=costs())
        decision = allocate(alloc, eligible)

        assert decision.granted
        assert MICRO not in decision.sampled_scores


class TestEligibleSetupsStillReachTheAllocator:
    def test_a_wider_profitable_setup_is_allocated_normally(self):
        alloc = allocator([WIDE])

        eligible, _ = eligibility().filter([wide_candidate()], costs=costs())
        decision = allocate(alloc, eligible)

        assert decision.granted
        assert decision.strategy_id == WIDE

    def test_diversity_is_preserved_among_eligible_candidates(self):
        """Requirement 5: the filter narrows the pool, it does not pick winners."""
        candidates = [
            wide_candidate(WIDE),
            (HOUR, signal(HOUR, stop_pct=0.006, target_pct=0.03, timeframe="60")),
        ]
        alloc = allocator([WIDE, HOUR], always_explore=True)

        eligible, _ = eligibility().filter(candidates, costs=costs())
        assert len(eligible) == 2

        chosen = {allocate(alloc, eligible).strategy_id for _ in range(40)}
        assert chosen == {WIDE, HOUR}, "exploration collapsed onto one strategy"

    def test_a_micro_strategy_with_a_real_target_is_allowed_through(self):
        """Requirement 3: allowed in when the individual setup genuinely passes."""
        candidate = (MICRO, signal(MICRO, stop_pct=0.004, target_pct=0.03,
                                   timeframe="1"))
        alloc = allocator([MICRO])

        eligible, _ = eligibility().filter([candidate], costs=costs())
        decision = allocate(alloc, eligible)

        assert decision.granted
        assert decision.strategy_id == MICRO


class TestTheFinalGatesAreUnchanged:
    def test_eligibility_is_a_first_opinion_not_the_last_word(self):
        """The executor's own layers must still run on what gets through.

        Asserted structurally: the executor's submit path still contains its
        leverage, sizing and safety gates, and none of them consult the
        eligibility filter.
        """
        import inspect

        from btcbot.execution import demo_executor

        source = inspect.getsource(demo_executor)
        for gate in ("leverage_engine", "position_sizing", "order_safety"):
            assert gate in source, gate
        assert "eligibility" not in source, (
            "the executor now depends on the pre-filter — it must stay independent"
        )

    def test_the_pre_filter_shares_the_sizers_stop_floor(self):
        """Two independent minimums would drift apart and reopen the bug."""
        risk = RiskConfig()
        filt = ActualTradeEligibility(ActualEligibilityConfig(), risk)

        assert filt.risk is risk
        just_under = risk.min_stop_distance_pct * 0.99
        verdict = filt.assess(
            signal(WIDE, stop_pct=just_under, target_pct=0.05), costs=costs()
        )
        assert not verdict.eligible, "the pre-filter passed what the sizer refuses"


class TestIneligibleSetupsKeepTradingInShadow:
    def test_shadow_entry_happens_before_and_independently_of_allocation(self):
        """Requirement 2: a shadow-only verdict costs the strategy nothing.

        Structural, because it is a property of the call order rather than of
        any single function: shadow routing happens inside `_evaluate_strategy`,
        which runs for every signal before `_allocate_demo` is ever called. The
        eligibility filter lives inside `_allocate_demo`, so it cannot reach
        back and suppress a shadow trade.
        """
        import inspect

        from btcbot.app.orchestrator import Orchestrator

        evaluate = inspect.getsource(Orchestrator._evaluate_strategy)
        assert "self.shadow.on_signal" in evaluate
        assert "eligibility" not in evaluate, (
            "shadow routing now consults the actual-trade filter — it must not"
        )

        on_bar = inspect.getsource(Orchestrator._on_bar_closed)
        assert on_bar.index("_evaluate_strategy") < on_bar.index("_allocate_demo")

        allocate_demo = inspect.getsource(Orchestrator._allocate_demo)
        assert "self.eligibility.filter" in allocate_demo

    def test_a_shadow_only_verdict_is_journaled_as_layer_zero(self):
        """Not layers 8-10: it never entered the order pipeline at all."""
        import inspect

        from btcbot.app.orchestrator import Orchestrator

        source = inspect.getsource(Orchestrator._journal_ineligible)
        assert '"layer_index": 0' in source
        assert '"layer_name": "actual_eligibility"' in source


class TestCostsDriveTheVerdictNotTheTimeframe:
    def test_a_higher_fee_rate_makes_previously_eligible_setups_shadow_only(self):
        candidate = wide_candidate()

        # Both marked verified: this is about the rate's size, not whether it
        # was confirmed. The unverified case has its own test.
        cheap = TradeCosts(taker_fee_rate=0.0005, spread_bps=2.0, slippage_bps=2.0,
                           source="exchange")
        expensive = TradeCosts(taker_fee_rate=0.01, spread_bps=2.0, slippage_bps=2.0,
                               source="exchange")

        assert eligibility().assess(candidate[1], costs=cheap).eligible
        assert not eligibility().assess(candidate[1], costs=expensive).eligible

    def test_a_widening_spread_can_close_the_window(self):
        candidate = wide_candidate()

        assert eligibility().assess(candidate[1], costs=costs(2.0)).eligible
        verdict = eligibility().assess(candidate[1], costs=costs(50.0))
        assert not verdict.eligible
        assert "liquidity" in verdict.reason
