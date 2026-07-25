"""Quantity/tick rounding and safe arithmetic.

Rounding is where a correct position size becomes a rejected order, so these are
exhaustive. Two rules under test:

* quantity always rounds **down** (never over-risk or over-spend)
* prices round to the nearest tick, and nothing is ever emitted in scientific
  notation, which Bybit rejects
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from btcbot.utils.numeric import (
    clamp,
    decimals_for_step,
    format_price,
    format_qty,
    is_finite_positive,
    pct_change,
    round_step_down,
    round_step_up,
    round_to_tick,
    safe_div,
    to_decimal,
)


class TestDecimalConversion:
    def test_float_avoids_binary_noise(self):
        assert to_decimal(0.1) == Decimal("0.1")
        assert to_decimal(0.000001) == Decimal("0.000001")

    def test_string_and_decimal_pass_through(self):
        assert to_decimal("0.001") == Decimal("0.001")
        assert to_decimal(Decimal("2.5")) == Decimal("2.5")

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_floats_are_refused(self, value):
        with pytest.raises(ValueError):
            to_decimal(value)

    def test_garbage_string_is_refused(self):
        with pytest.raises(ValueError):
            to_decimal("not a number")


class TestQuantityRounding:
    @pytest.mark.parametrize(
        ("value", "step", "expected"),
        [
            (0.123456789, "0.000001", "0.123456"),
            (0.0000119, "0.000001", "0.000011"),
            (1.9999999, "0.001", "1.999"),
            (0.5, "0.001", "0.5"),
            (100.0, "0.001", "100"),
            (0.0000009, "0.000001", "0"),
        ],
    )
    def test_rounds_down_to_step(self, value, step, expected):
        assert round_step_down(value, step) == Decimal(expected)

    def test_never_rounds_up(self):
        """Rounding up could exceed the risk budget or the available balance."""
        for value in (0.1239999, 0.9999, 0.0019999):
            rounded = round_step_down(value, "0.001")
            assert float(rounded) <= value

    def test_round_up_reaches_a_minimum(self):
        assert round_step_up(0.0000101, "0.000001") == Decimal("0.000011")
        assert round_step_up(0.1, "0.001") == Decimal("0.1")

    @pytest.mark.parametrize("step", [0, -0.001])
    def test_invalid_step_is_refused(self, step):
        with pytest.raises(ValueError):
            round_step_down(1.0, step)

    def test_result_is_always_a_step_multiple(self):
        step = Decimal("0.000001")
        for raw in (0.123456789, 1.0000005, 0.0000015, 55.5555555):
            assert round_step_down(raw, step) % step == 0


class TestPriceRounding:
    @pytest.mark.parametrize(
        ("price", "tick", "expected"),
        [
            (50_000.04, "0.1", "50000.0"),
            (50_000.06, "0.1", "50000.1"),
            (50_000.05, "0.1", "50000.1"),   # half-up
            (1.23456, "0.0001", "1.2346"),
            (50_003.0, "5", "50005"),
        ],
    )
    def test_rounds_to_nearest_tick(self, price, tick, expected):
        assert round_to_tick(price, tick) == Decimal(expected)

    def test_invalid_tick_is_refused(self):
        with pytest.raises(ValueError):
            round_to_tick(100.0, 0)


class TestFormatting:
    @pytest.mark.parametrize(
        ("step", "places"),
        [("0.000001", 6), ("0.001", 3), ("1", 0), ("0.1", 1), ("0.00000001", 8)],
    )
    def test_decimals_for_step(self, step, places):
        assert decimals_for_step(step) == places

    def test_qty_never_uses_scientific_notation(self):
        """`1e-05` in an order body is rejected by Bybit."""
        for value in (0.00001, 0.000001, 1e-8, 0.0):
            text = format_qty(value, "0.00000001")
            assert "e" not in text.lower(), f"{text} used scientific notation"

    def test_qty_has_the_exact_decimal_count(self):
        assert format_qty(0.1, "0.000001") == "0.100000"
        assert format_qty(1, "0.001") == "1.000"
        assert format_qty(0.123456789, "0.000001") == "0.123456"

    def test_price_formatting_matches_tick(self):
        assert format_price(50_000.0, "0.1") == "50000.0"
        assert format_price(50_000.06, "0.1") == "50000.1"
        assert format_price(1.23456, "0.0001") == "1.2346"

    def test_integer_step_formats_without_a_decimal_point(self):
        assert format_qty(5.9, "1") == "5"


class TestSafeArithmetic:
    def test_division_by_zero_returns_the_default(self):
        assert safe_div(1.0, 0.0) == 0.0
        assert safe_div(1.0, 0.0, default=-1.0) == -1.0

    @pytest.mark.parametrize(
        ("numerator", "denominator"),
        [
            (float("nan"), 1.0),
            (1.0, float("nan")),
            (float("inf"), 1.0),
            (1.0, float("inf")),
        ],
    )
    def test_non_finite_inputs_return_the_default(self, numerator, denominator):
        assert safe_div(numerator, denominator, default=0.0) == 0.0

    def test_normal_division_is_exact(self):
        assert safe_div(10.0, 4.0) == 2.5

    def test_clamp_bounds_values(self):
        assert clamp(5.0, 0.0, 1.0) == 1.0
        assert clamp(-5.0, 0.0, 1.0) == 0.0
        assert clamp(0.5, 0.0, 1.0) == 0.5

    def test_clamp_rejects_inverted_bounds(self):
        with pytest.raises(ValueError):
            clamp(0.5, 1.0, 0.0)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(1.0, True), (0.0, False), (-1.0, False), (float("nan"), False),
         (float("inf"), False), (None, False)],
    )
    def test_is_finite_positive(self, value, expected):
        assert is_finite_positive(value) is expected

    def test_pct_change(self):
        assert math.isclose(pct_change(110.0, 100.0), 0.1)
        assert math.isclose(pct_change(90.0, 100.0), -0.1)
        assert pct_change(1.0, 0.0) == 0.0


class TestInstrumentValidation:
    def test_quantity_bounds_are_enforced(self, spot_instrument):
        ok, _ = spot_instrument.qty_within_bounds(Decimal("0.001"))
        assert ok

        ok, message = spot_instrument.qty_within_bounds(Decimal("0"))
        assert not ok and "zero" in message

        ok, message = spot_instrument.qty_within_bounds(Decimal("0.000001"))
        assert not ok and "below minimum" in message

        ok, message = spot_instrument.qty_within_bounds(Decimal("1000"))
        assert not ok and "above maximum" in message

    def test_notional_bounds_use_min_order_amt(self, spot_instrument):
        """Bybit marks spot minOrderQty deprecated in favour of minOrderAmt."""
        ok, message = spot_instrument.notional_within_bounds(Decimal("3"))
        assert not ok and "below exchange minimum" in message

        ok, _ = spot_instrument.notional_within_bounds(Decimal("50"))
        assert ok

    def test_linear_has_no_notional_minimum(self, linear_instrument):
        ok, _ = linear_instrument.notional_within_bounds(Decimal("1"))
        assert ok

    def test_instrument_rounding_helpers(self, spot_instrument):
        assert spot_instrument.round_qty(0.1234567) == Decimal("0.123456")
        assert spot_instrument.round_price(50_000.06) == Decimal("50000.1")

    def test_capability_discovery_not_assumption(self, spot_instrument, linear_instrument):
        from btcbot.exchange.models import Capability

        assert not spot_instrument.supports(Capability.SHORT)
        assert not spot_instrument.supports(Capability.LEVERAGE)
        assert linear_instrument.supports(Capability.SHORT)
        assert linear_instrument.supports(Capability.LEVERAGE)

    def test_spot_parsing_matches_documented_response(self):
        """Parsed from the exact JSON shape in Bybit's instruments-info docs."""
        from btcbot.exchange.models import Category, InstrumentSpec

        payload = {
            "symbolId": 9, "symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT",
            "innovation": "0", "status": "Trading", "marginTrading": "utaOnly", "stTag": "0",
            "lotSizeFilter": {
                "basePrecision": "0.000001", "quotePrecision": "0.0000001",
                "minOrderQty": "0.000011", "maxOrderQty": "83", "minOrderAmt": "5",
                "maxOrderAmt": "8000000", "maxLimitOrderQty": "83",
                "maxMarketOrderQty": "41.5", "postOnlyMaxLimitOrderSize": "60000",
            },
            "priceFilter": {"tickSize": "0.1"},
            "riskParameters": {"priceLimitRatioX": "0.005", "priceLimitRatioY": "0.01"},
        }
        spec = InstrumentSpec.from_response(payload, Category.SPOT)
        assert spec.symbol == "BTCUSDT"
        assert spec.is_tradable
        assert spec.tick_size == Decimal("0.1")
        assert spec.qty_step == Decimal("0.000001")
        assert spec.min_order_amt == Decimal("5")
        assert spec.max_market_order_qty == Decimal("41.5")
        assert spec.max_order_qty == Decimal("83")

    def test_linear_parsing_matches_documented_response(self):
        from btcbot.exchange.models import Capability, Category, InstrumentSpec

        payload = {
            "symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading",
            "priceFilter": {"tickSize": "0.10"},
            "lotSizeFilter": {"maxOrderQty": "1190.000", "minOrderQty": "0.001",
                              "qtyStep": "0.001"},
        }
        spec = InstrumentSpec.from_response(payload, Category.LINEAR)
        assert spec.qty_step == Decimal("0.001")
        assert spec.min_order_qty == Decimal("0.001")
        assert spec.supports(Capability.SHORT)
        assert spec.min_order_amt is None
