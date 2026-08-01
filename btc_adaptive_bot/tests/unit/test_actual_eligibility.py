"""A setup may only become an actual trade if it can pay for itself.

The behaviour this pins down: the allocator was repeatedly selecting setups
that the executor then refused. Live logs showed candidates with stop distances
of 0.034%–0.043% against a 0.080% minimum, and others whose round-trip costs
came to 190%–310% of the target profit — each one logged as "allocated actual
Demo trade" moments before being blocked.

The filter here runs *before* allocation and answers one question per setup:
ELIGIBLE, or SHADOW_ONLY. It is not a safety gate — the executor's leverage,
sizing and order-safety layers still run on everything that passes — it exists
so the allocator stops choosing candidates that cannot work.

Two properties matter most, and both are asserted from several angles:

* the arithmetic is *net*: costs are charged to both legs, not ignored
* nothing is judged by its timeframe — a 1m setup with a real target passes,
  and a 1h setup with a thin one does not
"""

from __future__ import annotations

import logging

import pytest

from btcbot.config.schema import ActualEligibilityConfig, RiskConfig
from btcbot.execution.eligibility import (
    ELIGIBLE,
    SHADOW_ONLY,
    ActualTradeEligibility,
    EligibilityStats,
    TradeCosts,
)
from btcbot.regime.classifier import Regime
from btcbot.strategies.base import Direction, ExitMechanism, ExitPolicy, StrategySignal

ENTRY = 100_000.0

#: The rate that motivated this work: OKX Demo charges 0.25% per side, so a
#: round trip costs 0.5% before spread and slippage ever enter the picture.
DEMO_TAKER = 0.0025


def costs(*, taker: float = DEMO_TAKER, spread_bps: float = 2.0,
          slippage_bps: float = 2.0, source: str = "exchange") -> TradeCosts:
    return TradeCosts(
        taker_fee_rate=taker, spread_bps=spread_bps,
        slippage_bps=slippage_bps, source=source,
    )


def signal(*, stop_pct: float, target_pct: float | None, timeframe: str = "15",
           strategy_id: str = "ema_trend_cross_15m") -> StrategySignal:
    """A long whose stop and target are expressed as fractions of entry."""
    return StrategySignal(
        strategy_id=strategy_id,
        strategy_version="1.0",
        direction=Direction.LONG,
        symbol="BTC-USDT-SWAP",
        timeframe=timeframe,
        bar_open_ms=1_700_000_000_000,
        entry_reference=ENTRY,
        stop_price=ENTRY * (1 - stop_pct),
        target_price=ENTRY * (1 + target_pct) if target_pct is not None else None,
        confidence=0.75,
        setup_key="k",
        rationale="test",
        exit_policy=ExitPolicy(mechanisms=frozenset({ExitMechanism.FIXED_RR})),
        regime=Regime.TREND_UP,
        regime_confidence=0.8,
    )


def filter_for(**config_overrides) -> ActualTradeEligibility:
    return ActualTradeEligibility(
        ActualEligibilityConfig(**config_overrides), RiskConfig()
    )


class TestTheCostModel:
    def test_a_round_trip_charges_two_taker_legs(self):
        c = costs(taker=DEMO_TAKER, spread_bps=0.0, slippage_bps=0.0)
        assert c.fee_cost == pytest.approx(0.005), "0.25% per side is 0.5% round trip"
        assert c.round_trip == pytest.approx(0.005)

    def test_spread_and_slippage_are_charged_on_both_legs(self):
        c = costs(taker=0.0, spread_bps=4.0, slippage_bps=3.0)
        # One full spread (half each way) plus slippage on entry and exit.
        assert c.spread_cost == pytest.approx(0.0004)
        assert c.slippage_cost == pytest.approx(0.0006)
        assert c.round_trip == pytest.approx(0.0010)

    def test_the_fee_source_is_recorded_not_assumed(self):
        assert "exchange" in costs(source="exchange").describe()
        assert "config fallback" in costs(source="config fallback").describe()


class TestMicrostructureSetupsBecomeShadowOnly:
    def test_a_1m_setup_with_a_004_percent_stop_is_shadow_only(self):
        """The exact shape from the live logs: 0.04% stop, 0.08% minimum."""
        verdict = filter_for().assess(
            signal(stop_pct=0.0004, target_pct=0.0006, timeframe="1",
                   strategy_id="trade_flow_imbalance_1m"),
            costs=costs(),
        )

        assert not verdict.eligible
        assert verdict.label == SHADOW_ONLY
        assert "stop distance" in verdict.reason
        assert "0.0400%" in verdict.reason
        assert "0.0800%" in verdict.reason

    def test_the_rejection_happens_before_any_cost_arithmetic(self):
        """A stop the sizer will refuse is refused for that reason, plainly."""
        verdict = filter_for().assess(
            signal(stop_pct=0.00034, target_pct=0.10), costs=costs()
        )
        assert not verdict.eligible
        assert "position sizing would refuse" in verdict.reason

    def test_micro_strategies_are_not_banned_by_name_or_timeframe(self):
        """A 1m setup with a genuinely wide target is eligible."""
        verdict = filter_for().assess(
            signal(stop_pct=0.004, target_pct=0.03, timeframe="1",
                   strategy_id="orderbook_imbalance_1m"),
            costs=costs(),
        )

        assert verdict.eligible, verdict.reason
        assert verdict.label == ELIGIBLE

    def test_a_wide_timeframe_does_not_excuse_a_thin_target(self):
        """Requirement: do not approve trades purely because of timeframe."""
        verdict = filter_for().assess(
            signal(stop_pct=0.002, target_pct=0.003, timeframe="60",
                   strategy_id="donchian_breakout_1h"),
            costs=costs(),
        )

        assert not verdict.eligible, "a 1h setup passed on timeframe alone"


class TestNetProfitArithmetic:
    def test_a_negative_expected_net_profit_is_shadow_only(self):
        """Target 0.50% against 0.56% of round-trip costs — a loss by construction."""
        verdict = filter_for().assess(
            signal(stop_pct=0.002, target_pct=0.005), costs=costs()
        )

        assert not verdict.eligible
        assert verdict.expected_net_profit_pct < 0
        assert "not positive" in verdict.reason
        assert "smaller than round-trip costs" in verdict.reason

    def test_costs_far_exceeding_the_target_are_reported_as_a_ratio(self):
        """The "costs are 190%-310% of target profit" case."""
        verdict = filter_for(min_target_distance_pct=0.001).assess(
            signal(stop_pct=0.002, target_pct=0.002), costs=costs()
        )

        assert not verdict.eligible
        assert verdict.cost_pct > verdict.target_distance_pct

    def test_a_thin_but_positive_edge_still_fails_the_cost_multiple(self):
        """Positive is not the same as worth taking."""
        # Target 0.70% against 0.54% costs: net +0.16%, but only 1.3x costs.
        verdict = filter_for(min_target_to_cost_multiple=2.0).assess(
            signal(stop_pct=0.002, target_pct=0.007), costs=costs()
        )

        assert not verdict.eligible
        assert verdict.expected_net_profit_pct > 0, "the gross edge was positive"
        assert "round-trip costs" in verdict.reason
        assert "% of target profit" in verdict.reason

    def test_net_reward_risk_charges_costs_to_both_legs(self):
        """Costs shrink the reward AND deepen the loss — that is the point."""
        verdict = filter_for(min_net_reward_risk=5.0).assess(
            signal(stop_pct=0.005, target_pct=0.02), costs=costs()
        )

        cost = verdict.cost_pct
        expected = (0.02 - cost) / (0.005 + cost)
        assert verdict.net_reward_risk == pytest.approx(expected)
        assert not verdict.eligible, "an impossible R:R threshold was somehow met"
        assert "both" in verdict.reason

    def test_a_genuinely_profitable_wider_setup_is_eligible(self):
        """Requirement 4: wider-target setups remain available for actual use."""
        verdict = filter_for().assess(
            signal(stop_pct=0.005, target_pct=0.025, timeframe="60"), costs=costs()
        )

        assert verdict.eligible, verdict.reason
        assert verdict.expected_net_profit_pct > 0
        assert verdict.net_reward_risk >= 1.0
        assert verdict.target_to_cost >= 2.0


class TestTargetsAndLiquidity:
    def test_a_setup_without_a_target_cannot_be_priced(self):
        verdict = filter_for().assess(
            signal(stop_pct=0.005, target_pct=None), costs=costs()
        )

        assert not verdict.eligible
        assert "no take-profit target" in verdict.reason

    def test_a_target_below_the_minimum_distance_is_shadow_only(self):
        verdict = filter_for(min_target_distance_pct=0.01).assess(
            signal(stop_pct=0.002, target_pct=0.008), costs=costs()
        )

        assert not verdict.eligible
        assert "target distance" in verdict.reason

    def test_a_wide_spread_is_insufficient_liquidity(self):
        verdict = filter_for(max_spread_bps=5.0).assess(
            signal(stop_pct=0.005, target_pct=0.03), costs=costs(spread_bps=25.0)
        )

        assert not verdict.eligible
        assert "insufficient liquidity" in verdict.reason

    def test_an_invalid_order_book_blocks_actual_trades_not_shadow_ones(self):
        verdict = filter_for().assess(
            signal(stop_pct=0.005, target_pct=0.03), costs=costs(),
            orderbook_valid=False,
        )

        assert not verdict.eligible
        assert "order book" in verdict.reason

    def test_liquidity_can_be_waived_by_configuration_but_not_by_default(self):
        assert ActualEligibilityConfig().require_orderbook is True
        verdict = filter_for(require_orderbook=False).assess(
            signal(stop_pct=0.005, target_pct=0.03), costs=costs(),
            orderbook_valid=False,
        )
        assert verdict.eligible, verdict.reason


class TestTheFilterSplitsCandidates:
    def test_only_eligible_candidates_are_returned_for_allocation(self):
        candidates = [
            ("trade_flow_imbalance_1m", signal(stop_pct=0.0004, target_pct=0.0006, timeframe="1")),
            ("ema_trend_cross_15m", signal(stop_pct=0.005, target_pct=0.025)),
            ("orderbook_imbalance_1m", signal(stop_pct=0.0003, target_pct=0.0005, timeframe="1")),
        ]

        eligible, verdicts = filter_for().filter(candidates, costs=costs())

        assert len(verdicts) == 3, "every candidate must get a verdict"
        assert [sid for sid, _ in eligible] == ["ema_trend_cross_15m"]

    def test_verdicts_are_returned_in_candidate_order(self):
        """The orchestrator zips them back together to journal each rejection."""
        candidates = [
            ("a_1m", signal(stop_pct=0.0004, target_pct=0.0006, strategy_id="a_1m")),
            ("b_15m", signal(stop_pct=0.005, target_pct=0.025, strategy_id="b_15m")),
        ]

        _, verdicts = filter_for().filter(candidates, costs=costs())

        assert [v.strategy_id for v in verdicts] == ["a_1m", "b_15m"]

    def test_an_empty_candidate_list_is_handled(self):
        eligible, verdicts = filter_for().filter([], costs=costs())
        assert eligible == [] and verdicts == []


class TestTheMandatedLogBlock:
    def test_every_required_line_is_emitted(self, caplog):
        caplog.set_level(logging.DEBUG)
        filter_for().assess(signal(stop_pct=0.005, target_pct=0.025), costs=costs())

        # The `[ACTUAL ELIGIBILITY]` prefix is the record's tag, which the
        # console formatter renders — caplog shows only the message.
        tags = {getattr(r, "tag", None) for r in caplog.records}
        assert tags == {"ACTUAL ELIGIBILITY"}, tags

        text = caplog.text
        for fragment in (
            "strategy", "stop distance", "target distance", "estimated cost",
            "expected net profit", "reason",
        ):
            assert fragment in text, fragment
        assert ELIGIBLE in text

    def test_a_refusal_says_shadow_only_and_why(self, caplog):
        caplog.set_level(logging.DEBUG)
        filter_for().assess(signal(stop_pct=0.0004, target_pct=0.0006), costs=costs())

        assert {getattr(r, "tag", None) for r in caplog.records} == {"ACTUAL ELIGIBILITY"}
        assert SHADOW_ONLY in caplog.text
        assert "reason" in caplog.text

    def test_the_verdict_serialises_every_number_for_the_journal(self):
        verdict = filter_for().assess(
            signal(stop_pct=0.005, target_pct=0.025), costs=costs()
        )
        data = verdict.as_dict()

        for key in (
            "eligible", "label", "strategy_id", "timeframe", "reason",
            "stop_distance_pct", "target_distance_pct", "cost_pct",
            "expected_net_profit_pct", "net_reward_risk", "target_to_cost",
        ):
            assert key in data, key


class TestCounters:
    def test_eligible_and_shadow_only_are_counted_separately(self):
        stats = EligibilityStats()
        filt = filter_for()
        filt.stats = stats

        filt.assess(signal(stop_pct=0.005, target_pct=0.025), costs=costs())
        filt.assess(signal(stop_pct=0.0004, target_pct=0.0006), costs=costs())
        filt.assess(signal(stop_pct=0.002, target_pct=0.003), costs=costs())

        assert stats.assessed == 3
        assert stats.eligible == 1
        assert stats.shadow_only == 2

    def test_rejection_reasons_are_grouped_for_the_dashboard(self):
        filt = filter_for()
        for _ in range(3):
            filt.assess(signal(stop_pct=0.0004, target_pct=0.0006), costs=costs())

        reasons = filt.stats.as_dict()["top_reasons"]
        assert reasons and reasons[0][1] == 3
