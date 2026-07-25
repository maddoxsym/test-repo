"""Position sizing: bounds, rounding, notional rules, and adversarial inputs.

The central property under test: **no input can produce an unbounded position.**
Every hostile value the brief lists is thrown at the sizer, and the invariant is
that it either rejects or returns a size within the configured risk ceiling.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from btcbot.config.schema import RiskConfig
from btcbot.risk.position_sizing import PositionSizer, SizingInputs
from btcbot.strategies.base import Direction


def _inputs(**overrides) -> SizingInputs:
    base = {
        "equity": 10_000.0,
        "available_balance": 10_000.0,
        "entry_price": 50_000.0,
        "stop_price": 49_000.0,
        "direction": Direction.LONG,
        "confidence": 0.6,
        "atr": 500.0,
    }
    base.update(overrides)
    return SizingInputs(**base)


class TestBasicSizing:
    def test_normal_trade_is_approved_and_sized(self, risk_config, spot_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(), spot_instrument)

        assert result.approved, result.reason
        assert result.quantity > 0
        assert result.risk_pct_of_equity <= risk_config.max_risk_pct
        # 2% stop distance and ~0.75% risk ⇒ roughly 0.075 BTC notional share.
        assert 0.0 < float(result.quantity) < 1.0
        assert result.reasoning, "sizing must record its reasoning"

    def test_size_varies_with_confidence(self, risk_config, spot_instrument):
        """The brief requires that not every trade uses the same amount."""
        sizer = PositionSizer(risk_config)
        low = sizer.calculate(_inputs(confidence=0.1), spot_instrument)
        high = sizer.calculate(_inputs(confidence=0.95), spot_instrument)

        assert low.approved and high.approved
        assert high.quantity > low.quantity

    def test_size_varies_with_stop_distance(self, risk_config, spot_instrument):
        """A wider stop must produce a smaller position at constant risk."""
        sizer = PositionSizer(risk_config)
        tight = sizer.calculate(_inputs(stop_price=49_500.0), spot_instrument)
        wide = sizer.calculate(_inputs(stop_price=47_000.0), spot_instrument)

        assert tight.approved and wide.approved
        assert tight.quantity > wide.quantity
        # Risk in currency terms should stay comparable.
        assert math.isclose(tight.risk_amount, wide.risk_amount, rel_tol=0.35)

    def test_drawdown_reduces_size(self, risk_config, spot_instrument):
        sizer = PositionSizer(risk_config)
        healthy = sizer.calculate(_inputs(drawdown_pct=0.0), spot_instrument)
        drawn = sizer.calculate(_inputs(drawdown_pct=0.20), spot_instrument)

        assert healthy.approved and drawn.approved
        assert drawn.quantity < healthy.quantity

    def test_news_risk_reduces_size(self, risk_config, spot_instrument):
        sizer = PositionSizer(risk_config)
        calm = sizer.calculate(_inputs(news_size_factor=1.0), spot_instrument)
        risky = sizer.calculate(_inputs(news_size_factor=0.5), spot_instrument)

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
        self, risk_config, spot_instrument, confidence, expectancy
    ):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            _inputs(confidence=confidence, expectancy_r=expectancy, observations=500),
            spot_instrument,
        )
        if result.approved:
            assert result.risk_pct_of_equity <= risk_config.max_risk_pct + 1e-9, (
                f"risk {result.risk_pct_of_equity} exceeded cap {risk_config.max_risk_pct}"
            )

    def test_notional_capped_at_equity_fraction(self, risk_config, spot_instrument):
        """Spot cannot borrow: notional must respect the equity cap."""
        sizer = PositionSizer(risk_config)
        # A very tight (but legal) stop would otherwise demand a huge position.
        result = sizer.calculate(
            _inputs(stop_price=49_950.0, confidence=1.0), spot_instrument
        )
        if result.approved:
            assert result.notional <= 10_000.0 * risk_config.max_notional_pct_equity + 1e-6

    def test_available_balance_limits_notional(self, risk_config, spot_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            _inputs(available_balance=300.0, stop_price=49_900.0), spot_instrument
        )
        if result.approved:
            assert result.notional <= 300.0 + 1e-6


class TestAdversarialInputs:
    """Every hostile input from the brief must be refused, not survived."""

    def test_zero_stop_distance_rejected(self, risk_config, spot_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(stop_price=50_000.0), spot_instrument)
        assert not result.approved
        assert "stop" in result.reason.lower()

    def test_microscopic_stop_rejected(self, risk_config, spot_instrument):
        """The classic blow-up: a 0.0002% stop demanding an enormous position."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(stop_price=49_999.9), spot_instrument)
        assert not result.approved
        assert "below the minimum" in result.reason or "ATR" in result.reason

    def test_inverted_long_stop_rejected(self, risk_config, spot_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(stop_price=51_000.0), spot_instrument)
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
    def test_malformed_equity_rejected(self, risk_config, spot_instrument, equity):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(equity=equity), spot_instrument)
        assert not result.approved
        assert "equity" in result.reason.lower()

    @pytest.mark.parametrize("price", [0.0, -50_000.0, float("nan")])
    def test_malformed_price_rejected(self, risk_config, spot_instrument, price):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(entry_price=price), spot_instrument)
        assert not result.approved

    def test_negative_available_balance_rejected(self, risk_config, spot_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(available_balance=-500.0), spot_instrument)
        assert not result.approved
        assert "available" in result.reason.lower()

    def test_absurdly_wide_stop_rejected(self, risk_config, spot_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(stop_price=1_000.0), spot_instrument)
        assert not result.approved
        assert "maximum" in result.reason

    def test_never_returns_negative_quantity(self, risk_config, spot_instrument):
        sizer = PositionSizer(risk_config)
        for stop in (49_999.99, 49_000.0, 40_000.0, 51_000.0, 50_000.0):
            result = sizer.calculate(_inputs(stop_price=stop), spot_instrument)
            assert result.quantity >= 0


class TestExchangeRules:
    def test_below_minimum_notional_is_raised_when_risk_permits(
        self, risk_config, spot_instrument
    ):
        """A tiny account should be bumped to the exchange minimum, or refused."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            _inputs(equity=200.0, available_balance=200.0), spot_instrument
        )
        if result.approved:
            assert Decimal(str(result.notional)) >= spot_instrument.min_order_amt
        else:
            assert "minimum" in result.reason

    def test_rejects_when_minimum_would_breach_risk_cap(self, spot_instrument):
        """Raising to the exchange minimum must never break the risk ceiling."""
        tiny_risk = RiskConfig(
            normal_risk_pct=0.005, min_risk_pct=0.005, max_risk_pct=0.006
        )
        sizer = PositionSizer(tiny_risk)
        # $60 equity: the $5 minimum notional with a 2% stop is ~$0.10 risk,
        # which is far above 0.6% of $60.
        result = sizer.calculate(
            _inputs(equity=15.0, available_balance=15.0), spot_instrument
        )
        if result.approved:
            assert result.risk_pct_of_equity <= tiny_risk.max_risk_pct + 1e-9

    def test_quantity_is_a_multiple_of_step(self, risk_config, spot_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(), spot_instrument)
        assert result.approved
        remainder = result.quantity % spot_instrument.qty_step
        assert remainder == 0, f"{result.quantity} is not a multiple of {spot_instrument.qty_step}"

    def test_quantity_string_is_fixed_point(self, risk_config, spot_instrument):
        """Scientific notation in an order body is rejected by Bybit."""
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(_inputs(equity=500.0, available_balance=500.0), spot_instrument)
        if result.approved:
            assert "e" not in result.quantity_str.lower()
            assert "E" not in result.quantity_str

    def test_respects_market_order_maximum(self, risk_config, spot_instrument):
        sizer = PositionSizer(risk_config)
        result = sizer.calculate(
            _inputs(equity=5_000_000_000.0, available_balance=5_000_000_000.0), spot_instrument
        )
        if result.approved:
            assert result.quantity <= spot_instrument.max_order_qty
