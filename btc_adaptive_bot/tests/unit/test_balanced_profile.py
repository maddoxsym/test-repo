"""The balanced profile relaxes what is redundant and nothing else.

The change: target-to-cost 3.00x -> 2.00x, equivalently "costs may be at most
50% of target" instead of 33%. Everything that decides whether a trade can lose
money is untouched — expected net profit must still be positive, net
reward:risk must still clear 1.20, and the stop floor is still the position
sizer's own 0.080%.

The point worth pinning down, and the reason this file exists: at a 0.56%
round trip the relaxed multiple is almost inert. Net reward:risk >= 1.20
requires

    target >= 1.2 x stop + 2.2 x cost  =  1.2 x stop + 1.232%

which is stricter than the 2.0x rule (target >= 1.12%) for *every* positive
stop. So the balanced profile cannot admit a trade that RR would refuse — the
relaxation is bounded by a gate that was never loosened. Tests below assert
that directly, because it is the difference between "we relaxed a redundant
constraint" and "we relaxed the one that was protecting us".
"""

from __future__ import annotations

import pytest

from btcbot.config.schema import ActualEligibilityConfig, RiskConfig
from btcbot.execution.eligibility import (
    BALANCED_PROFILE,
    SHADOW_ONLY,
    STRICT_PROFILE,
    ActualTradeEligibility,
    TradeCosts,
)
from btcbot.regime.classifier import Regime
from btcbot.strategies.base import Direction, ExitMechanism, ExitPolicy, StrategySignal

ENTRY = 118_000.0
DEMO_TAKER = 0.0025          # verified: 0.25% per side
ROUND_TRIP = 0.0056          # 0.50% fees + 0.02% spread + 0.04% slippage

#: The smallest target-to-cost multiple that can ever satisfy net RR >= 1.20,
#: reached at the tightest legal stop (0.080%). Below this nothing is
#: admissible whatever the profile says — which is why the balanced 2.0x
#: threshold is inert and the strict 3.0x one was not.
MIN_FEASIBLE_MULTIPLE = 2.371


def costs(*, taker: float = DEMO_TAKER, spread_bps: float = 2.0,
          source: str = "exchange") -> TradeCosts:
    return TradeCosts(
        taker_fee_rate=taker, spread_bps=spread_bps,
        slippage_bps=2.0, source=source,
    )


def signal(*, stop_pct: float, target_pct: float | None,
           timeframe: str = "15", strategy_id: str = "ema_trend_cross_15m",
           direction: Direction = Direction.LONG) -> StrategySignal:
    sign = 1 if direction is Direction.LONG else -1
    return StrategySignal(
        strategy_id=strategy_id,
        strategy_version="1.0",
        direction=direction,
        symbol="BTC-USDT-SWAP",
        timeframe=timeframe,
        bar_open_ms=1_700_000_000_000,
        entry_reference=ENTRY,
        stop_price=ENTRY * (1 - sign * stop_pct),
        target_price=ENTRY * (1 + sign * target_pct) if target_pct is not None else None,
        confidence=0.75,
        setup_key="k",
        rationale="test",
        exit_policy=ExitPolicy(mechanisms=frozenset({ExitMechanism.FIXED_RR})),
        regime=Regime.TREND_UP,
        regime_confidence=0.8,
    )


def profile(name: str, **overrides) -> ActualTradeEligibility:
    base = STRICT_PROFILE if name == "strict" else BALANCED_PROFILE
    config = ActualEligibilityConfig(**{**base, **overrides})
    return ActualTradeEligibility(config, RiskConfig())


def target_for_cost_multiple(multiple: float) -> float:
    return multiple * ROUND_TRIP


class TestTargetsBelowTheirCostsStayBlocked:
    """The invariant that must survive any relaxation."""

    @pytest.mark.parametrize("multiple", [0.32, 0.53])
    def test_a_target_a_fraction_of_its_costs_is_always_shadow_only(self, multiple):
        """0.32x and 0.53x costs — losses by construction, under either profile."""
        setup = signal(stop_pct=0.002, target_pct=target_for_cost_multiple(multiple))

        for name in ("strict", "balanced"):
            verdict = profile(name, min_target_distance_pct=0.0001).assess(
                setup, costs=costs()
            )
            assert not verdict.eligible, f"{name} admitted a {multiple}x-cost target"
            assert verdict.label == SHADOW_ONLY
            assert verdict.expected_net_profit_pct < 0

    def test_a_target_exactly_equal_to_costs_is_blocked(self):
        """Break-even before slippage is not a trade, it is a coin flip with a fee."""
        setup = signal(stop_pct=0.002, target_pct=ROUND_TRIP)
        verdict = profile("balanced", min_target_distance_pct=0.0001).assess(
            setup, costs=costs()
        )

        assert not verdict.eligible
        # Exactly at cost the net profit is zero to within float noise, so the
        # refusal can land on either the net-profit gate or the multiple. Both
        # are correct; what matters is that it cannot make money.
        assert verdict.expected_net_profit_pct == pytest.approx(0.0, abs=1e-9)
        assert verdict.target_to_cost == pytest.approx(1.0, abs=1e-6)

    def test_the_cost_share_gate_and_the_multiple_gate_agree(self):
        """Two views of one constraint; the config validator keeps them equal."""
        config = ActualEligibilityConfig(**BALANCED_PROFILE)
        assert config.max_cost_pct_of_target == pytest.approx(
            1.0 / config.min_target_to_cost_multiple
        )

    def test_inconsistent_profitability_knobs_are_rejected_at_load(self):
        """They cannot be allowed to drift apart into two different rules."""
        with pytest.raises(ValueError, match="same constraint"):
            ActualEligibilityConfig(
                min_target_to_cost_multiple=2.0, max_cost_pct_of_target=0.90
            )


class TestAGenuinelyProfitableSetupPasses:
    def test_a_2_2x_cost_target_cannot_also_clear_net_rr_1_20(self):
        """2.2x costs and RR >= 1.20 have no common solution.

        RR 1.20 requires target >= 1.2*stop + 2.2*cost. A target of exactly
        2.2*cost therefore needs a stop of zero. Since the stop floor is
        0.080%, the smallest target-to-cost multiple that can ever pass is
        MIN_FEASIBLE_MULTIPLE below — anything under it is unreachable, not
        merely rare.
        """
        setup = signal(stop_pct=0.0009, target_pct=target_for_cost_multiple(2.2))
        verdict = profile("balanced").assess(setup, costs=costs())

        assert not verdict.eligible
        assert "reward:risk" in verdict.reason
        assert verdict.expected_net_profit_pct > 0, "the gross edge was positive"

    def test_the_smallest_feasible_multiple_is_set_by_rr_not_by_the_profile(self):
        stop_floor = RiskConfig().min_stop_distance_pct
        smallest_target = 1.20 * stop_floor + 2.2 * ROUND_TRIP
        assert smallest_target / ROUND_TRIP == pytest.approx(MIN_FEASIBLE_MULTIPLE, abs=0.01)
        # Which is above the balanced threshold, so that threshold never binds.
        assert BALANCED_PROFILE["min_target_to_cost_multiple"] < MIN_FEASIBLE_MULTIPLE

    def test_a_setup_just_above_the_feasible_floor_passes_balanced(self):
        """Positive net PnL, net RR at or above 1.20, and it is admitted."""
        setup = signal(stop_pct=0.0008, target_pct=target_for_cost_multiple(2.45))
        verdict = profile("balanced").assess(setup, costs=costs())

        assert verdict.eligible, verdict.reason
        assert verdict.expected_net_profit_pct > 0
        assert verdict.net_reward_risk >= 1.20

    def test_the_band_the_relaxation_actually_recovers_is_between_2_37x_and_3x(self):
        """Strict refused these; balanced admits them. This is the whole gain."""
        setup = signal(stop_pct=0.0008, target_pct=target_for_cost_multiple(2.6))

        strict = profile("strict").assess(setup, costs=costs())
        balanced = profile("balanced").assess(setup, costs=costs())

        assert not strict.eligible
        assert "3.00×" in strict.reason
        assert balanced.eligible, balanced.reason

    def test_a_wide_1h_trend_setup_passes_both_profiles(self):
        setup = signal(stop_pct=0.006, target_pct=0.035, timeframe="60",
                       strategy_id="ema_adx_trend_1h")

        for name in ("strict", "balanced"):
            verdict = profile(name).assess(setup, costs=costs())
            assert verdict.eligible, f"{name}: {verdict.reason}"

    def test_shorts_are_measured_the_same_way(self):
        setup = signal(stop_pct=0.006, target_pct=0.035, direction=Direction.SHORT)
        verdict = profile("balanced").assess(setup, costs=costs())

        assert verdict.eligible, verdict.reason
        assert verdict.stop_distance_pct == pytest.approx(0.006)


class TestNetRewardRiskIsTheBindingGate:
    """Why relaxing the multiple alone changes so little."""

    @pytest.mark.parametrize("stop_pct", [0.001, 0.002, 0.003, 0.005, 0.008])
    def test_rr_demands_more_target_than_the_2x_rule_at_every_stop(self, stop_pct):
        rr_required = 1.2 * stop_pct + 2.2 * ROUND_TRIP
        multiple_required = 2.0 * ROUND_TRIP
        assert rr_required > multiple_required, (
            "the 2.0x rule would bind before RR — the relaxation is not bounded"
        )

    def test_a_setup_released_by_the_multiple_is_still_caught_by_rr(self):
        """Sits between 2.0x and 3.0x costs, but its stop is too wide for RR."""
        setup = signal(stop_pct=0.004, target_pct=target_for_cost_multiple(2.5))

        strict = profile("strict").assess(setup, costs=costs())
        balanced = profile("balanced").assess(setup, costs=costs())

        assert not strict.eligible
        assert not balanced.eligible, "the balanced profile admitted it"
        assert "reward:risk" in balanced.reason, balanced.reason

    def test_net_reward_risk_is_1_20_in_both_profiles(self):
        assert STRICT_PROFILE["min_net_reward_risk"] == 1.20
        assert BALANCED_PROFILE["min_net_reward_risk"] == 1.20

    def test_only_the_profitability_multiple_differs_between_profiles(self):
        differing = {
            k for k in STRICT_PROFILE
            if STRICT_PROFILE[k] != BALANCED_PROFILE[k]
        }
        assert differing == {"min_target_to_cost_multiple", "max_cost_pct_of_target"}


class TestTheUntouchedGatesAreUntouched:
    @pytest.mark.parametrize("stop_pct", [0.0004, 0.00034, 0.00043, 0.0007999])
    def test_stops_below_0_080_percent_stay_blocked_under_balanced(self, stop_pct):
        setup = signal(stop_pct=stop_pct, target_pct=0.05, timeframe="1",
                       strategy_id="trade_flow_imbalance_1m")
        verdict = profile("balanced").assess(setup, costs=costs())

        assert not verdict.eligible
        assert "0.0800%" in verdict.reason

    def test_the_stop_floor_still_comes_from_the_position_sizer(self):
        risk = RiskConfig()
        filt = ActualTradeEligibility(
            ActualEligibilityConfig(**BALANCED_PROFILE), risk
        )
        assert filt.risk is risk
        assert risk.min_stop_distance_pct == 0.0008

    def test_an_unverified_fee_rate_blocks_every_actual_entry(self):
        """A cost model built on a guess is not a basis for a real order."""
        setup = signal(stop_pct=0.006, target_pct=0.035)
        verdict = profile("balanced").assess(
            setup, costs=costs(source="config fallback")
        )

        assert not verdict.eligible
        assert "fee schedule has not been verified" in verdict.reason

    def test_the_fee_gate_is_on_by_default(self):
        assert ActualEligibilityConfig().block_when_fee_rate_unverified is True

    def test_a_verified_fee_rate_lets_the_same_setup_through(self):
        setup = signal(stop_pct=0.006, target_pct=0.035)
        assert profile("balanced").assess(setup, costs=costs(source="exchange")).eligible

    def test_liquidity_and_orderbook_gates_are_unchanged(self):
        setup = signal(stop_pct=0.006, target_pct=0.035)

        wide = profile("balanced").assess(setup, costs=costs(spread_bps=40.0))
        assert not wide.eligible and "liquidity" in wide.reason

        blind = profile("balanced").assess(
            setup, costs=costs(), orderbook_valid=False
        )
        assert not blind.eligible and "order book" in blind.reason


class TestMicroStrategiesRemainShadowOnlyUnlessIndividuallyEligible:
    @pytest.mark.parametrize(
        "strategy_id", ["orderbook_imbalance_1m", "trade_flow_imbalance_1m"]
    )
    def test_their_normal_setups_are_shadow_only(self, strategy_id):
        """Typical shape: a few basis points of stop and target."""
        setup = signal(stop_pct=0.0005, target_pct=0.0008, timeframe="1",
                       strategy_id=strategy_id)
        verdict = profile("balanced").assess(setup, costs=costs())

        assert not verdict.eligible
        assert verdict.label == SHADOW_ONLY

    @pytest.mark.parametrize(
        "strategy_id", ["orderbook_imbalance_1m", "trade_flow_imbalance_1m"]
    )
    def test_an_individually_qualifying_setup_is_allowed_through(self, strategy_id):
        """No blanket ban: the setup is judged, not the strategy's name."""
        setup = signal(stop_pct=0.005, target_pct=0.03, timeframe="1",
                       strategy_id=strategy_id)
        verdict = profile("balanced").assess(setup, costs=costs())

        assert verdict.eligible, verdict.reason

    def test_no_strategy_name_or_timeframe_appears_in_the_filter(self):
        """The rule must stay a measurement, not a list."""
        import inspect

        from btcbot.execution import eligibility

        source = inspect.getsource(eligibility)
        for banned in ("_1m", "orderbook_imbalance", "trade_flow_imbalance"):
            assert banned not in source.replace('"""', "|||").split("|||")[2], banned
