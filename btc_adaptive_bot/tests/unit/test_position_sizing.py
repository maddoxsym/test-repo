"""Position sizing: bounds, contract conversion, margin rules, adversarial inputs.

The central property under test: **no input can produce an unbounded position.**
Every hostile value the brief lists is thrown at the sizer, and the invariant is
that it either rejects or returns a size within the configured risk ceiling.

Perp specifics under test: base→contract conversion through the discovered
``ctVal``/``ctMult``/``lotSz``, the contract minimum, and the margin budget
(leverage changes margin, never the risk cap).
"""

from __future__ import annotations

import math

import pytest
from conftest import make_perp_instrument

from btcbot.risk.position_sizing import PositionSizer, SizingInputs
from btcbot.strategies.base import Direction


def _inputs(**overrides) -> SizingInputs:
    base = {
        "equity": 10_000.0,
        "available_balance": 10_000.0,
        "entry_price": 50_000.0,
        "stop_price": 49_000.0,
        "direction": Direction.LONG,
        "leverage": 2.0,
        "confidence": 0.6,
        "atr": 500.0,
    }
    base.update(overrides)
    return SizingInputs(**base)


class TestBasicSizing:
    def test_normal_trade_is_approved_and_sized(self, risk_config, perp_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(), perp_instrument)

        assert result.approved, result.reason
        assert result.quantity > 0
        assert result.contracts > 0
        assert result.risk_pct_of_equity <= risk_config.max_risk_pct
        # 2% stop distance and ~0.75% risk ⇒ roughly 0.075 BTC.
        assert 0.0 < float(result.quantity) < 1.0
        assert result.reasoning, "sizing must record its reasoning"

    def test_base_quantity_matches_contracts(self, risk_config, perp_instrument):
        """quantity (base) must equal contracts × ctVal × ctMult exactly."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(), perp_instrument)
        assert result.approved
        assert result.quantity == perp_instrument.base_from_contracts(result.contracts)

    def test_size_varies_with_confidence(self, risk_config, perp_instrument):
        """The brief requires that not every trade uses the same amount."""
        sizer = PositionSizer(risk_config)
        low = sizer.calculate(_inputs(confidence=0.1), perp_instrument)
        high = sizer.calculate(_inputs(confidence=0.95), perp_instrument)

        assert low.approved and high.approved
        assert high.quantity > low.quantity

    def test_size_varies_with_stop_distance(self, risk_config, perp_instrument):
        """A wider stop must produce a smaller position at constant risk."""
        sizer = PositionSizer(risk_config)
        tight = sizer.calculate(_inputs(stop_price=49_500.0), perp_instrument)
        wide = sizer.calculate(_inputs(stop_price=47_000.0), perp_instrument)

        assert tight.approved and wide.approved
        assert tight.quantity > wide.quantity
        # Risk in currency terms should stay comparable.
        assert math.isclose(tight.risk_amount, wide.risk_amount, rel_tol=0.35)

    def test_drawdown_reduces_size(self, risk_config, perp_instrument):
        sizer = PositionSizer(risk_config)
        healthy = sizer.calculate(_inputs(drawdown_pct=0.0), perp_instrument)
        drawn = sizer.calculate(_inputs(drawdown_pct=0.20), perp_instrument)

        assert healthy.approved and drawn.approved
        assert drawn.quantity < healthy.quantity

    def test_news_risk_reduces_size(self, risk_config, perp_instrument):
        sizer = PositionSizer(risk_config)
        calm = sizer.calculate(_inputs(news_size_factor=1.0), perp_instrument)
        risky = sizer.calculate(_inputs(news_size_factor=0.5), perp_instrument)

        assert calm.approved and risky.approved
        assert risky.quantity < calm.quantity

    def test_short_direction_sizes_correctly(self, risk_config, linear_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            _inputs(direction=Direction.SHORT, entry_price=50_000.0, stop_price=51_000.0),
            linear_instrument,
        )
        assert result.approved, result.reason
        assert result.quantity > 0


class TestHardBounds:
    """The risk ceiling must hold regardless of the inputs."""

    @pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0, 5.0, -3.0])
    @pytest.mark.parametrize("expectancy", [-5.0, 0.0, 10.0])
    def test_risk_never_exceeds_maximum(
        self, risk_config, perp_instrument, confidence, expectancy
    ):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            _inputs(confidence=confidence, expectancy_r=expectancy, observations=500),
            perp_instrument,
        )
        if result.approved:
            assert result.risk_pct_of_equity <= risk_config.max_risk_pct + 1e-9, (
                f"risk {result.risk_pct_of_equity} exceeded cap {risk_config.max_risk_pct}"
            )

    @pytest.mark.parametrize("leverage", [1.0, 2.0, 5.0, 10.0])
    def test_leverage_never_raises_the_risk_cap(self, risk_config, perp_instrument, leverage):
        """Leverage changes margin, never the stop-out risk ceiling."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(leverage=leverage), perp_instrument)
        if result.approved:
            assert result.risk_pct_of_equity <= risk_config.max_risk_pct + 1e-9

    def test_notional_capped_by_leverage_and_equity(self, risk_config, perp_instrument):
        sizer = PositionSizer(risk_config)
        # A very tight (but legal) stop would otherwise demand a huge position.
        result = sizer.calculate(
            _inputs(stop_price=49_950.0, confidence=1.0, leverage=2.0), perp_instrument
        )
        if result.approved:
            cap = 10_000.0 * risk_config.max_notional_pct_equity * 2.0
            assert result.notional <= cap + 1e-6

    def test_available_margin_limits_notional(self, risk_config, perp_instrument):
        """With $300 available at 2x, the margin budget caps notional at
        300 × margin_utilization_cap × 2."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            _inputs(available_balance=300.0, stop_price=49_900.0, leverage=2.0),
            perp_instrument,
        )
        if result.approved:
            budget = 300.0 * risk_config.leverage.margin_utilization_cap
            assert result.required_margin <= budget + 1e-6
            assert result.notional <= budget * 2.0 + 1e-6

    def test_required_margin_is_notional_over_leverage(self, risk_config, perp_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(leverage=4.0), perp_instrument)
        assert result.approved
        assert math.isclose(result.required_margin, result.notional / 4.0, rel_tol=1e-9)


class TestAdversarialInputs:
    """Every hostile input from the brief must be refused, not survived."""

    def test_zero_stop_distance_rejected(self, risk_config, perp_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(stop_price=50_000.0), perp_instrument)
        assert not result.approved
        assert "stop" in result.reason.lower()

    def test_microscopic_stop_rejected(self, risk_config, perp_instrument):
        """The classic blow-up: a 0.0002% stop demanding an enormous position."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(stop_price=49_999.9), perp_instrument)
        assert not result.approved
        assert "below the minimum" in result.reason or "ATR" in result.reason

    def test_inverted_long_stop_rejected(self, risk_config, perp_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(stop_price=51_000.0), perp_instrument)
        assert not result.approved
        assert "above entry" in result.reason

    def test_inverted_short_stop_rejected(self, risk_config, linear_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            _inputs(direction=Direction.SHORT, stop_price=49_000.0), linear_instrument
        )
        assert not result.approved
        assert "below entry" in result.reason

    @pytest.mark.parametrize("equity", [0.0, -100.0, float("nan"), float("inf")])
    def test_malformed_equity_rejected(self, risk_config, perp_instrument, equity):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(equity=equity), perp_instrument)
        assert not result.approved
        assert "equity" in result.reason.lower()

    @pytest.mark.parametrize("price", [0.0, -50_000.0, float("nan")])
    def test_malformed_price_rejected(self, risk_config, perp_instrument, price):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(entry_price=price), perp_instrument)
        assert not result.approved

    @pytest.mark.parametrize("leverage", [0.0, -1.0, 11.0, float("nan"), float("inf")])
    def test_malformed_leverage_rejected(self, risk_config, perp_instrument, leverage):
        """The sizer independently refuses leverage outside (0, 10]."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(leverage=leverage), perp_instrument)
        assert not result.approved
        assert "leverage" in result.reason.lower()

    def test_negative_available_balance_rejected(self, risk_config, perp_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(available_balance=-500.0), perp_instrument)
        assert not result.approved
        assert "available" in result.reason.lower()

    def test_absurdly_wide_stop_rejected(self, risk_config, perp_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(stop_price=1_000.0), perp_instrument)
        assert not result.approved
        assert "maximum" in result.reason

    def test_never_returns_negative_quantity(self, risk_config, perp_instrument):
        sizer = PositionSizer(risk_config)
        for stop in (49_999.99, 49_000.0, 40_000.0, 51_000.0, 50_000.0):
            result = sizer.calculate(_inputs(stop_price=stop), perp_instrument)
            assert result.quantity >= 0
            assert result.contracts >= 0


class TestExchangeRules:
    def test_below_contract_minimum_is_rejected(self, risk_config):
        """A tiny account whose risk-based size rounds under minSz must be
        refused — the sizer never bumps risk up to reach a contract minimum."""
        chunky = make_perp_instrument(ct_val="0.01", lot_size="1", min_size="1")
        sizer = PositionSizer(risk_config)
        # ~0.75% of $200 = $1.5 risk over a $1000 stop ⇒ 0.0015 BTC ⇒ 0.15
        # contracts of 0.01 BTC ⇒ floors to 0 at lot 1.
        result = sizer.calculate(
            _inputs(equity=200.0, available_balance=200.0), chunky
        )
        assert not result.approved
        assert "zero" in result.reason or "minimum" in result.reason

    def test_contracts_are_a_multiple_of_lot(self, risk_config, perp_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(), perp_instrument)
        assert result.approved
        remainder = result.contracts % perp_instrument.lot_size
        assert remainder == 0, f"{result.contracts} is not a multiple of {perp_instrument.lot_size}"

    def test_quantity_string_is_fixed_point(self, risk_config, perp_instrument):
        """Scientific notation in an order body is rejected by the exchange."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            _inputs(equity=2_000.0, available_balance=2_000.0), perp_instrument
        )
        if result.approved:
            assert "e" not in result.quantity_str.lower()
            assert "E" not in result.quantity_str

    def test_respects_market_order_maximum(self, risk_config, perp_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            _inputs(equity=5_000_000_000.0, available_balance=5_000_000_000.0),
            perp_instrument,
        )
        if result.approved:
            assert result.contracts <= perp_instrument.max_mkt_size

    def test_rounding_down_keeps_risk_at_or_below_target(self, risk_config):
        """Contract flooring must only ever *reduce* risk."""
        chunky = make_perp_instrument(ct_val="0.01", lot_size="0.5", min_size="0.5")
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(), chunky)
        if result.approved:
            assert result.risk_pct_of_equity <= risk_config.max_risk_pct + 1e-9
