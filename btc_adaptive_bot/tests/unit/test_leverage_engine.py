"""DYNAMIC_LEVERAGE_ENGINE: bounds, liquidation buffer, risk-state caps.

The central properties: leverage never leaves [min, min(10, exchange max)],
and no approved decision has a projected liquidation distance closer than the
configured multiple of the stop distance.
"""

from __future__ import annotations

import pytest

from btcbot.config.schema import LeverageConfig
from btcbot.risk.leverage_engine import LeverageEngine, LeverageInputs


def _inputs(**overrides) -> LeverageInputs:
    base = {
        "entry_price": 50_000.0,
        "stop_price": 49_000.0,   # 2% stop
        "direction": "long",
        "confidence": 0.6,
        "volatility_pct": 0.01,
        "regime": "TREND_UP",
        "regime_confidence": 0.7,
        "drawdown_pct": 0.0,
        "risk_state": "NORMAL",
        "max_exchange_leverage": 100.0,
    }
    base.update(overrides)
    return LeverageInputs(**base)


@pytest.fixture
def engine() -> LeverageEngine:
    return LeverageEngine(LeverageConfig())


class TestBounds:
    def test_normal_decision_is_bounded(self, engine):
        decision = engine.decide(_inputs())
        assert decision.approved, decision.reason
        assert 1.0 <= decision.leverage <= 10.0
        assert decision.reasoning

    @pytest.mark.parametrize("confidence", [-5.0, 0.0, 0.5, 1.0, 99.0])
    @pytest.mark.parametrize("vol", [None, 0.001, 0.01, 0.10])
    def test_leverage_never_exceeds_ten(self, engine, confidence, vol):
        decision = engine.decide(_inputs(confidence=confidence, volatility_pct=vol))
        if decision.approved:
            assert decision.leverage <= 10.0 + 1e-9
            assert decision.leverage >= 1.0 - 1e-9

    def test_exchange_maximum_is_respected(self):
        """A discovered exchange max below ours becomes the binding cap."""
        engine = LeverageEngine(LeverageConfig(base_leverage=5.0, max_leverage=10.0))
        decision = engine.decide(_inputs(max_exchange_leverage=3.0, confidence=1.0))
        if decision.approved:
            assert decision.leverage <= 3.0

    def test_confidence_raises_leverage(self, engine):
        low = engine.decide(_inputs(confidence=0.1))
        high = engine.decide(_inputs(confidence=0.95))
        assert low.approved and high.approved
        assert high.leverage >= low.leverage

    def test_high_volatility_caps_leverage(self):
        config = LeverageConfig(base_leverage=6.0, high_vol_leverage_cap=3.0)
        engine = LeverageEngine(config)
        decision = engine.decide(_inputs(volatility_pct=0.05, confidence=1.0))
        if decision.approved:
            assert decision.leverage <= config.high_vol_leverage_cap + 1e-9


class TestRiskStates:
    def test_paused_state_refuses(self, engine):
        decision = engine.decide(_inputs(risk_state="PAUSED"))
        assert not decision.approved
        assert "PAUSED" in decision.reason

    @pytest.mark.parametrize("state", ["REDUCED", "DEFENSIVE"])
    def test_defensive_states_cap_leverage(self, state):
        config = LeverageConfig(base_leverage=6.0, defensive_leverage_cap=2.0)
        engine = LeverageEngine(config)
        decision = engine.decide(_inputs(risk_state=state, confidence=1.0))
        if decision.approved:
            assert decision.leverage <= config.defensive_leverage_cap + 1e-9

    def test_drawdown_caps_leverage(self):
        config = LeverageConfig(base_leverage=6.0, defensive_leverage_cap=2.0)
        engine = LeverageEngine(config)
        decision = engine.decide(_inputs(drawdown_pct=0.12, confidence=1.0))
        if decision.approved:
            assert decision.leverage <= config.defensive_leverage_cap + 1e-9


class TestLiquidationProtection:
    def test_liq_buffer_holds_for_every_approval(self, engine):
        """Approved decisions must satisfy the configured stop-to-liq ratio."""
        for stop in (49_900.0, 49_500.0, 49_000.0, 47_500.0, 45_000.0):
            decision = engine.decide(_inputs(stop_price=stop, confidence=1.0))
            if decision.approved:
                assert (
                    decision.liq_buffer_ratio
                    >= engine.config.liq_buffer_stop_ratio - 1e-9
                ), f"stop {stop}: buffer {decision.liq_buffer_ratio}"

    def test_wide_stop_steps_leverage_down(self, engine):
        """A wide stop forces lower leverage to preserve the liq buffer."""
        tight = engine.decide(_inputs(stop_price=49_750.0, confidence=0.9))
        wide = engine.decide(_inputs(stop_price=46_500.0, confidence=0.9))
        assert tight.approved
        if wide.approved:
            assert wide.leverage <= tight.leverage

    def test_impossible_buffer_is_rejected(self):
        """A stop so wide no leverage satisfies the buffer must be refused."""
        config = LeverageConfig(liq_buffer_stop_ratio=20.0)
        engine = LeverageEngine(config)
        # 10% stop needs liq distance ≥ 200% — impossible even at 1x.
        decision = engine.decide(_inputs(stop_price=45_000.0))
        assert not decision.approved
        assert "liquidation" in decision.reason.lower() or "refused" in decision.reason.lower()

    def test_discovered_mmr_tightens_the_estimate(self, engine):
        loose = engine.decide(_inputs(maintenance_margin_rate=0.004, confidence=0.9))
        tight = engine.decide(_inputs(maintenance_margin_rate=0.04, confidence=0.9))
        assert loose.approved
        if tight.approved:
            assert tight.estimated_liq_distance_pct <= loose.estimated_liq_distance_pct


class TestAdversarialInputs:
    @pytest.mark.parametrize("entry,stop", [(0.0, 49_000.0), (50_000.0, 0.0), (-1.0, -2.0)])
    def test_invalid_prices_rejected(self, engine, entry, stop):
        decision = engine.decide(_inputs(entry_price=entry, stop_price=stop))
        assert not decision.approved

    def test_zero_stop_distance_rejected(self, engine):
        decision = engine.decide(_inputs(stop_price=50_000.0))
        assert not decision.approved

    def test_every_decision_carries_audit_fields(self, engine):
        decision = engine.decide(_inputs())
        payload = decision.as_dict()
        for key in (
            "approved",
            "leverage",
            "reasoning",
            "adjustments",
            "estimated_liq_distance_pct",
            "stop_distance_pct",
            "liq_buffer_ratio",
        ):
            assert key in payload
