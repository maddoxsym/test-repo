"""The ten-layer decision engine and the risk-state machine.

Two properties matter most and are asserted directly:

* a signal that fails any layer is **refused**, and
* the refusal is **journaled** with the exact layer that refused it.

"Why didn't it trade?" must always be answerable from the database.
"""

from __future__ import annotations

import pytest
from conftest import make_candles

from btcbot.config.schema import RiskConfig, SafetyConfig
from btcbot.decision.engine import (
    NEWS_ELEVATED,
    NEWS_EXTREME,
    NEWS_HIGH,
    NEWS_NORMAL,
    DecisionEngine,
    news_risk_label,
)
from btcbot.decision.risk_state import (
    DEFENSIVE,
    NORMAL,
    PAUSED,
    REDUCED,
    RiskStateSnapshot,
    RiskStateTracker,
)
from btcbot.features.engine import MultiTimeframeFeatures
from btcbot.regime.classifier import Regime, RegimeSnapshot
from btcbot.safety.circuit_breakers import CircuitBreakers
from btcbot.strategies.base import (
    Direction,
    ExitMechanism,
    ExitPolicy,
    StrategyCategory,
    StrategyContext,
    StrategySignal,
)
from btcbot.strategies.trend import EmaTrendCross


def _signal(**overrides) -> StrategySignal:
    base = dict(
        strategy_id="ema_trend_cross_15m",
        strategy_version="1.0",
        direction=Direction.LONG,
        symbol="BTC-USDT-SWAP",
        timeframe="15",
        bar_open_ms=1_700_000_000_000,
        entry_reference=50_000.0,
        stop_price=49_000.0,
        target_price=53_000.0,
        confidence=0.75,
        setup_key="setup_1",
        rationale="test signal",
        exit_policy=ExitPolicy(mechanisms=frozenset({ExitMechanism.FIXED_RR})),
        regime=Regime.TREND_UP,
        regime_confidence=0.8,
    )
    base.update(overrides)
    return StrategySignal(**base)


def _context(**overrides) -> StrategyContext:
    from btcbot.features.engine import FeatureEngine

    candles = make_candles([50_000.0 + i * 10 for i in range(300)], timeframe="15")
    features = FeatureEngine().compute_from_candles("BTC-USDT-SWAP", "15", candles)
    assert features is not None

    mtf_kwargs = {
        "symbol": "BTC-USDT-SWAP",
        "by_timeframe": {"15": features, "60": features, "240": features},
        "orderbook_imbalance": 0.1,
        "trade_flow_imbalance": 0.1,
        "spread_bps": 1.0,
        "orderbook_valid": True,
    }
    for key in list(overrides):
        if key in {"orderbook_imbalance", "orderbook_valid", "funding_rate",
                   "open_interest", "open_interest_prev"}:
            mtf_kwargs[key] = overrides.pop(key)

    regime = overrides.pop("regime_snapshot", None) or RegimeSnapshot(
        regime=Regime.TREND_UP, confidence=0.8, evidence={}, bar_open_ms=1_700_000_000_000,
        symbol="BTC-USDT-SWAP", timeframe="60",
    )
    base = dict(
        symbol="BTC-USDT-SWAP",
        features=MultiTimeframeFeatures(**mtf_kwargs),
        regime=regime,
        news_risk=0.0,
        news_blocks_entry=False,
        spread_bps=1.0,
        equity=10_000.0,
    )
    base.update(overrides)
    return StrategyContext(**base)


@pytest.fixture
def engine(repos) -> DecisionEngine:
    return DecisionEngine(repos.rejected, experiment_id="exp_test")


@pytest.fixture
def strategy() -> EmaTrendCross:
    return EmaTrendCross()


def _evaluate(engine, strategy, signal, context, *, state=None, healthy=True, detail=""):
    return engine.evaluate(
        strategy, signal, context,
        data_healthy=healthy,
        data_detail=detail or ("healthy" if healthy else "ticker stale"),
        risk_state=state or RiskStateSnapshot(NORMAL, "normal", 0.0, 0.0),
        signal_id="sig_1",
        setup_id="set_1",
    )


class TestAcceptance:
    def test_clean_signal_passes_all_seven_layers(self, engine, strategy):
        outcome = _evaluate(engine, strategy, _signal(), _context())
        assert outcome.accepted, outcome.describe()
        assert len(outcome.layers) == 7
        assert all(layer.passed for layer in outcome.layers)
        assert outcome.news_state == NEWS_NORMAL
        assert outcome.risk_state == NORMAL

    def test_layers_are_numbered_in_order(self, engine, strategy):
        outcome = _evaluate(engine, strategy, _signal(), _context())
        assert [layer.index for layer in outcome.layers] == [1, 2, 3, 4, 5, 6, 7]


class TestLayerRejections:
    def test_layer_1_stale_data(self, engine, strategy, repos):
        outcome = _evaluate(engine, strategy, _signal(), _context(), healthy=False)
        assert not outcome.accepted
        assert outcome.rejection.index == 1
        assert outcome.rejection.name == "data_health"
        rows = repos.rejected.recent(5)
        assert rows and rows[0]["layer_name"] == "data_health"

    def test_layer_2_poor_reward_to_risk(self, engine, strategy):
        # 1000 stop, 200 reward ⇒ R/R 0.2, below the 0.5 floor.
        outcome = _evaluate(
            engine, strategy, _signal(target_price=50_200.0), _context()
        )
        assert not outcome.accepted
        assert outcome.rejection.index == 2

    def test_layer_3_strategy_regime_mismatch(self, engine, strategy, repos):
        """The brief's explicit requirement: REJECTED — STRATEGY-REGIME MISMATCH."""
        outcome = _evaluate(
            engine, strategy, _signal(regime=Regime.LOW_VOLATILITY), _context()
        )
        assert not outcome.accepted
        assert outcome.rejection.index == 3
        assert outcome.rejection.name == "regime_alignment"
        assert "outside the strategy's allowed set" in outcome.rejection.detail

    def test_layer_4_counter_trend_veto(self, engine, strategy):
        """A short against a confident strong uptrend is refused."""
        regime = RegimeSnapshot(
            regime=Regime.STRONG_TREND_UP, confidence=0.9, evidence={},
            bar_open_ms=1_700_000_000_000, symbol="BTC-USDT-SWAP", timeframe="60",
        )
        outcome = _evaluate(
            engine, strategy,
            _signal(direction=Direction.SHORT, stop_price=51_000.0,
                    target_price=47_000.0, regime=Regime.STRONG_TREND_UP),
            _context(regime_snapshot=regime),
        )
        assert not outcome.accepted
        assert outcome.rejection.index == 4
        assert "counter-trend veto" in outcome.rejection.detail

    def test_layer_4_allows_reversal_families(self, engine):
        """Mean-reversion strategies exist to trade against the move — the
        counter-trend veto must not disqualify their entire hypothesis."""
        from btcbot.strategies.mean_reversion import VwapReversion

        reversal = VwapReversion()
        assert reversal.category is StrategyCategory.MEAN_REVERSION
        regime = RegimeSnapshot(
            regime=Regime.STRONG_TREND_UP, confidence=0.9, evidence={},
            bar_open_ms=1_700_000_000_000, symbol="BTC-USDT-SWAP", timeframe="60",
        )
        outcome = _evaluate(
            engine, reversal,
            _signal(strategy_id=reversal.id, direction=Direction.SHORT,
                    stop_price=51_000.0, target_price=47_000.0,
                    regime=Regime.RANGING),
            _context(regime_snapshot=regime),
        )
        # It may still fail a later layer, but never layer 4.
        if not outcome.accepted:
            assert outcome.rejection.index != 4

    def test_layer_5_order_flow_opposes_entry(self, engine, strategy):
        outcome = _evaluate(
            engine, strategy, _signal(), _context(orderbook_imbalance=-0.9)
        )
        assert not outcome.accepted
        assert outcome.rejection.index == 5

    def test_layer_5_is_skipped_when_the_book_is_invalid(self, engine, strategy):
        """A syncing book must not block trading — later layers compensate."""
        outcome = _evaluate(
            engine, strategy, _signal(),
            _context(orderbook_imbalance=-0.9, orderbook_valid=False),
        )
        assert outcome.accepted, outcome.describe()

    def test_layer_6_news_blocks_entry(self, engine, strategy, repos):
        outcome = _evaluate(
            engine, strategy, _signal(), _context(news_risk=0.9, news_blocks_entry=True)
        )
        assert not outcome.accepted
        assert outcome.rejection.index == 6
        assert outcome.news_state == NEWS_EXTREME

    def test_layer_7_paused_risk_state(self, engine, strategy):
        outcome = _evaluate(
            engine, strategy, _signal(), _context(),
            state=RiskStateSnapshot(PAUSED, "daily loss limit", 0.05, 0.11),
        )
        assert not outcome.accepted
        assert outcome.rejection.index == 7
        assert "daily loss limit" in outcome.rejection.detail

    def test_layer_7_defensive_requires_high_conviction(self, engine, strategy):
        defensive = RiskStateSnapshot(DEFENSIVE, "drawdown 9%", 0.09, 0.0)
        low = _evaluate(engine, strategy, _signal(confidence=0.5), _context(), state=defensive)
        assert not low.accepted
        assert low.rejection.index == 7

        high = _evaluate(engine, strategy, _signal(confidence=0.85), _context(), state=defensive)
        assert high.accepted, high.describe()


class TestJournaling:
    def test_every_rejection_records_its_layer_and_context(self, engine, strategy, repos):
        _evaluate(engine, strategy, _signal(regime=Regime.LOW_VOLATILITY), _context())
        rows = repos.rejected.recent(5)
        assert rows
        row = rows[0]
        assert row["layer_index"] == 3
        assert row["layer_name"] == "regime_alignment"
        assert row["strategy_id"] == "ema_trend_cross_15m"
        assert row["inst_id"] == "BTC-USDT-SWAP"
        assert row["direction"] == "long"
        assert row["signal_id"] == "sig_1"
        assert row["setup_id"] == "set_1"
        assert row["reason"]
        assert row["detail"]

    def test_accepted_signals_are_not_journaled_as_rejections(self, engine, strategy, repos):
        outcome = _evaluate(engine, strategy, _signal(), _context())
        assert outcome.accepted
        assert repos.rejected.recent(5) == []

    def test_counts_by_layer_summarise_the_funnel(self, engine, strategy, repos):
        _evaluate(engine, strategy, _signal(regime=Regime.LOW_VOLATILITY), _context())
        _evaluate(engine, strategy, _signal(), _context(), healthy=False)
        counts = repos.rejected.counts_by_layer("exp_test")
        assert counts.get("regime_alignment") == 1
        assert counts.get("data_health") == 1

    def test_stats_track_the_acceptance_rate(self, engine, strategy):
        _evaluate(engine, strategy, _signal(), _context())
        _evaluate(engine, strategy, _signal(regime=Regime.LOW_VOLATILITY), _context())
        stats = engine.stats()
        assert stats["evaluated"] == 2
        assert stats["accepted"] == 1


class TestNewsRiskLabels:
    @pytest.mark.parametrize(
        ("risk", "expected"),
        [(0.0, NEWS_NORMAL), (0.2, NEWS_NORMAL), (0.35, NEWS_ELEVATED),
         (0.65, NEWS_HIGH), (0.9, NEWS_EXTREME)],
    )
    def test_thresholds(self, risk, expected):
        assert news_risk_label(risk, blocks_entry=False) == expected

    def test_blocking_always_reads_extreme(self):
        assert news_risk_label(0.0, blocks_entry=True) == NEWS_EXTREME


class TestRiskStateTracker:
    @pytest.fixture
    def tracker(self) -> RiskStateTracker:
        return RiskStateTracker(RiskConfig(), CircuitBreakers(SafetyConfig()))

    def test_healthy_account_is_normal(self, tracker):
        tracker.start_of_day(10_000.0)
        state = tracker.evaluate(equity=10_000.0, peak_equity=10_000.0)
        assert state.state == NORMAL

    def test_early_drawdown_reduces(self, tracker):
        tracker.start_of_day(10_000.0)
        # 5% drawdown: past half the 8% de-risk threshold, below it.
        state = tracker.evaluate(equity=9_500.0, peak_equity=10_000.0)
        assert state.state == REDUCED

    def test_threshold_drawdown_is_defensive(self, tracker):
        tracker.start_of_day(10_000.0)
        state = tracker.evaluate(equity=9_100.0, peak_equity=10_000.0)
        assert state.state == DEFENSIVE

    def test_daily_loss_limit_pauses(self, tracker):
        tracker.start_of_day(10_000.0)
        # 11% down on the day, past the 10% limit.
        state = tracker.evaluate(equity=8_900.0, peak_equity=10_000.0)
        assert state.state == PAUSED
        assert "daily loss" in state.reason

    def test_safe_mode_pauses_regardless_of_equity(self):
        breakers = CircuitBreakers(SafetyConfig())
        tracker = RiskStateTracker(RiskConfig(), breakers)
        tracker.start_of_day(10_000.0)
        breakers.check_price(0.0)   # trip a breaker
        state = tracker.evaluate(equity=10_000.0, peak_equity=10_000.0)
        assert state.state == PAUSED
        assert "SAFE_MODE" in state.reason

    def test_new_day_resets_the_daily_baseline(self, tracker):
        tracker.start_of_day(10_000.0)
        assert tracker.evaluate(equity=8_900.0, peak_equity=10_000.0).state == PAUSED
        # A new UTC day re-bases the daily loss measurement.
        tracker.start_of_day(8_900.0)
        state = tracker.evaluate(equity=8_900.0, peak_equity=10_000.0)
        assert state.state != PAUSED

    def test_snapshot_serialises_for_the_dashboard(self, tracker):
        tracker.start_of_day(10_000.0)
        payload = tracker.evaluate(equity=9_500.0, peak_equity=10_000.0).as_dict()
        assert payload["state"] == REDUCED
        assert payload["drawdown_pct"] == 5.0
        assert "reason" in payload
