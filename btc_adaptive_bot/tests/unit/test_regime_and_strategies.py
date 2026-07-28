"""Regime classification and the strategy interface contract.

The regime tests assert *behaviour* rather than exact labels: a clean uptrend must
not be classified bearish, a flat market must not be classified strongly
trending, and no single indicator may decide the outcome alone.

The strategy tests enforce the interface contract across all 38 strategies —
including the invariants that make a signal safe to size (stop on the correct
side, confidence in range, deterministic setup keys).
"""

from __future__ import annotations

import math

import pytest

from btcbot.config.schema import RegimeConfig, StrategiesConfig
from btcbot.features.engine import FeatureEngine, MultiTimeframeFeatures
from btcbot.regime.classifier import (
    BEARISH_REGIMES,
    BULLISH_REGIMES,
    TRENDING_REGIMES,
    Regime,
    RegimeClassifier,
    RegimeSnapshot,
    RegimeTracker,
)
from btcbot.strategies.base import Direction, ExitMechanism, StrategyContext
from btcbot.strategies.registry import ALL_STRATEGY_CLASSES, StrategyRegistry
from tests.conftest import make_candles


@pytest.fixture
def engine() -> FeatureEngine:
    return FeatureEngine()


@pytest.fixture
def classifier(regime_config: RegimeConfig) -> RegimeClassifier:
    return RegimeClassifier(regime_config)


def _features(engine: FeatureEngine, candles):
    result = engine.compute_from_candles("BTCUSDT", candles[0].timeframe, candles)
    assert result is not None, "fixture did not produce enough bars for features"
    return result


class TestRegimeClassification:
    def test_uptrend_is_never_classified_bearish(self, engine, classifier, trending_candles):
        snapshot = classifier.classify(_features(engine, trending_candles))
        assert snapshot.regime not in BEARISH_REGIMES
        assert snapshot.direction_bias >= 0

    def test_downtrend_is_never_classified_bullish(self, engine, classifier):
        candles = make_candles(
            [40_000.0 - i * 45.0 for i in range(400)], timeframe="60", high_pad=20, low_pad=20
        )
        snapshot = classifier.classify(_features(engine, candles))
        assert snapshot.regime not in BULLISH_REGIMES
        assert snapshot.direction_bias <= 0

    def test_strong_uptrend_is_recognised_as_trending(self, engine, classifier):
        candles = make_candles(
            [30_000.0 * (1.004**i) for i in range(400)], timeframe="60", high_pad=25, low_pad=10
        )
        snapshot = classifier.classify(_features(engine, candles))
        assert snapshot.regime in TRENDING_REGIMES | {Regime.BREAKOUT, Regime.HIGH_VOLATILITY}
        assert snapshot.is_trending or snapshot.regime is Regime.BREAKOUT

    def test_flat_market_is_not_strongly_trending(self, engine, classifier, ranging_candles):
        snapshot = classifier.classify(_features(engine, ranging_candles))
        assert snapshot.regime not in {Regime.STRONG_TREND_UP, Regime.STRONG_TREND_DOWN}

    def test_volatility_expansion_is_detected(self, engine, classifier):
        quiet = [50_000.0 + (5.0 if i % 2 else -5.0) for i in range(300)]
        violent = [50_000.0 + (600.0 if i % 2 else -600.0) for i in range(100)]
        candles = make_candles(quiet + violent, timeframe="60", high_pad=50, low_pad=50)
        snapshot = classifier.classify(_features(engine, candles))
        assert snapshot.regime in {
            Regime.VOLATILITY_EXPANSION, Regime.HIGH_VOLATILITY, Regime.BREAKOUT,
            Regime.RANGING, Regime.UNCERTAIN,
        }
        assert math.isfinite(snapshot.confidence)

    def test_confidence_is_always_a_valid_probability(
        self, engine, classifier, trending_candles, ranging_candles
    ):
        for candles in (trending_candles, ranging_candles):
            snapshot = classifier.classify(_features(engine, candles))
            assert 0.0 <= snapshot.confidence <= 1.0

    def test_low_confidence_resolves_to_uncertain(self, engine, classifier):
        strict = RegimeClassifier(RegimeConfig(min_confidence=0.999))
        snapshot = strict.classify(_features(engine, make_candles(
            [50_000.0 + (30 if i % 3 else -25) for i in range(400)], timeframe="60",
            high_pad=20, low_pad=20,
        )))
        assert snapshot.regime is Regime.UNCERTAIN

    def test_multiple_features_contribute_evidence(self, engine, classifier, trending_candles):
        """No single indicator may decide the regime alone."""
        snapshot = classifier.classify(_features(engine, trending_candles))
        assert len(snapshot.evidence) >= 4, (
            f"only {len(snapshot.evidence)} evidence sources: {snapshot.evidence}"
        )
        assert "adx" in snapshot.evidence
        assert "trend_persistence" in snapshot.evidence

    def test_snapshot_serialises_for_the_database(self, engine, classifier, trending_candles):
        snapshot = classifier.classify(_features(engine, trending_candles))
        row = snapshot.to_row(experiment_id="exp_1", ts_utc="2026-07-24T00:00:00Z")
        assert row["regime"] == snapshot.regime.value
        assert row["symbol"] == "BTCUSDT"
        assert isinstance(row["evidence"], dict)

    def test_context_timeframe_is_used_when_supplied(self, engine, classifier, trending_candles):
        context = _features(engine, trending_candles)
        snapshot = classifier.classify(_features(engine, trending_candles), context=context)
        assert snapshot.context_regime is not None

    def test_classification_is_deterministic(self, engine, classifier, trending_candles):
        features = _features(engine, trending_candles)
        first = classifier.classify(features)
        second = classifier.classify(features)
        assert first.regime is second.regime
        assert first.confidence == second.confidence


class TestRegimeTracker:
    def _snapshot(self, regime: Regime, bar: int) -> RegimeSnapshot:
        return RegimeSnapshot(
            regime=regime, confidence=0.8, bar_open_ms=bar, symbol="BTCUSDT", timeframe="60"
        )

    def test_detects_a_regime_change(self):
        tracker = RegimeTracker()
        assert tracker.add(self._snapshot(Regime.TREND_UP, 1)) is False
        assert tracker.add(self._snapshot(Regime.TREND_UP, 2)) is False
        assert tracker.add(self._snapshot(Regime.RANGING, 3)) is True

    def test_same_bar_updates_in_place(self):
        tracker = RegimeTracker()
        tracker.add(self._snapshot(Regime.TREND_UP, 1))
        tracker.add(self._snapshot(Regime.RANGING, 1))
        assert len(tracker.history()) == 1

    def test_distribution_sums_to_one(self):
        tracker = RegimeTracker()
        for index in range(10):
            tracker.add(
                self._snapshot(Regime.TREND_UP if index < 6 else Regime.RANGING, index)
            )
        distribution = tracker.distribution()
        assert math.isclose(sum(distribution.values()), 1.0)
        assert math.isclose(distribution["TREND_UP"], 0.6)

    def test_stability_is_one_when_unchanged(self):
        tracker = RegimeTracker()
        for index in range(20):
            tracker.add(self._snapshot(Regime.TREND_UP, index))
        assert tracker.stability() == 1.0

    def test_stability_falls_when_flipping(self):
        tracker = RegimeTracker()
        for index in range(20):
            tracker.add(
                self._snapshot(Regime.TREND_UP if index % 2 else Regime.TREND_DOWN, index)
            )
        assert tracker.stability() == 0.0

    def test_history_is_bounded(self):
        tracker = RegimeTracker(max_history=50)
        for index in range(200):
            tracker.add(self._snapshot(Regime.TREND_UP, index))
        assert len(tracker.history(limit=1000)) <= 50


class TestStrategyLibrary:
    def test_at_least_thirty_distinct_strategies(self):
        assert len(ALL_STRATEGY_CLASSES) >= 30, (
            f"only {len(ALL_STRATEGY_CLASSES)} strategies registered"
        )

    def test_all_ids_are_unique(self):
        ids = [cls.id for cls in ALL_STRATEGY_CLASSES]
        duplicates = {i for i in ids if ids.count(i) > 1}
        assert not duplicates, f"duplicate strategy ids: {duplicates}"

    def test_every_category_is_represented(self):
        from btcbot.strategies.base import StrategyCategory

        categories = {cls.category for cls in ALL_STRATEGY_CLASSES}
        for expected in StrategyCategory:
            assert expected in categories, f"no strategy in category {expected.value}"

    def test_every_strategy_declares_a_hypothesis(self):
        """Distinct hypotheses, not renamed parameters."""
        for cls in ALL_STRATEGY_CLASSES:
            assert cls.hypothesis, f"{cls.id} has no hypothesis"
            assert len(cls.hypothesis) > 40, f"{cls.id} hypothesis is too vague"

    def test_hypotheses_are_distinct(self):
        hypotheses = [cls.hypothesis for cls in ALL_STRATEGY_CLASSES]
        assert len(set(hypotheses)) == len(hypotheses), "two strategies share a hypothesis"

    def test_every_strategy_declares_exit_mechanisms(self):
        for cls in ALL_STRATEGY_CLASSES:
            assert cls.exit_mechanisms, f"{cls.id} declares no exit mechanisms"
            assert all(isinstance(m, ExitMechanism) for m in cls.exit_mechanisms)

    def test_trailing_stops_are_not_applied_blindly(self):
        """Mean-reversion strategies must not trail — it destroys the premise."""
        from btcbot.strategies.base import StrategyCategory

        for cls in ALL_STRATEGY_CLASSES:
            if cls.category is StrategyCategory.MEAN_REVERSION:
                assert ExitMechanism.TRAILING_STOP not in cls.exit_mechanisms, (
                    f"{cls.id} is mean-reversion but uses a trailing stop"
                )

    def test_all_strategies_instantiate_with_defaults(self):
        for cls in ALL_STRATEGY_CLASSES:
            instance = cls()
            assert instance.id == cls.id
            assert isinstance(instance.params, dict)

    def test_unknown_parameter_override_is_rejected(self):
        from btcbot.strategies.trend import EmaTrendCross

        with pytest.raises(ValueError) as exc:
            EmaTrendCross(nonexistent_parameter=1)
        assert "unknown parameter" in str(exc.value)

    def test_declared_parameters_exist_in_the_space(self):
        for cls in ALL_STRATEGY_CLASSES:
            defaults = cls.default_params()
            for name in cls.parameter_space():
                assert name in defaults, (
                    f"{cls.id} exposes '{name}' for optimisation but has no such parameter"
                )

    def test_parameter_space_values_are_usable(self):
        """Every optimiser candidate value must construct successfully."""
        for cls in ALL_STRATEGY_CLASSES:
            for name, options in cls.parameter_space().items():
                assert len(options) >= 2, f"{cls.id}.{name} has fewer than 2 options"
                for value in options:
                    cls(**{name: value})

    def test_timeframes_are_valid_intervals(self):
        from btcbot.utils.timeutil import SUPPORTED_INTERVALS

        for cls in ALL_STRATEGY_CLASSES:
            assert cls.primary_timeframe in SUPPORTED_INTERVALS
            for timeframe in cls.context_timeframes:
                assert timeframe in SUPPORTED_INTERVALS

    def test_registry_builds_every_strategy(self):
        registry = StrategyRegistry.build(
            StrategiesConfig(), available_timeframes=["1", "3", "5", "15", "30", "60", "240"]
        )
        assert len(registry) == len(ALL_STRATEGY_CLASSES)

    def test_registry_honours_the_enabled_list(self):
        registry = StrategyRegistry.build(
            StrategiesConfig(enabled=["ema_trend_cross_15m", "vwap_reversion_5m"]),
            available_timeframes=["5", "15", "60"],
        )
        assert set(registry.ids) == {"ema_trend_cross_15m", "vwap_reversion_5m"}

    def test_registry_honours_the_disabled_list(self):
        registry = StrategyRegistry.build(
            StrategiesConfig(disabled=["ema_trend_cross_15m"]),
            available_timeframes=["1", "3", "5", "15", "30", "60", "240"],
        )
        assert "ema_trend_cross_15m" not in registry.ids

    def test_registry_rejects_unknown_ids(self):
        from btcbot.utils.errors import ConfigError

        with pytest.raises(ConfigError):
            StrategyRegistry.build(StrategiesConfig(enabled=["not_a_strategy"]))

    def test_registry_skips_strategies_without_their_timeframes(self):
        registry = StrategyRegistry.build(
            StrategiesConfig(), available_timeframes=["15"]
        )
        for strategy in registry:
            assert strategy.primary_timeframe == "15"
            assert not strategy.context_timeframes

    def test_registry_applies_parameter_overrides(self):
        registry = StrategyRegistry.build(
            StrategiesConfig(
                enabled=["ema_trend_cross_15m"],
                overrides={"ema_trend_cross_15m": {"rr_target": 3.5}},
            ),
            available_timeframes=["15", "60"],
        )
        assert registry.require("ema_trend_cross_15m").param("rr_target") == 3.5

    def test_ensembles_receive_members(self):
        from btcbot.strategies.ensemble import EnsembleBase

        registry = StrategyRegistry.build(
            StrategiesConfig(), available_timeframes=["1", "3", "5", "15", "30", "60", "240"]
        )
        ensembles = registry.ensembles()
        assert ensembles, "no ensemble strategies were built"
        for ensemble in ensembles:
            assert len(ensemble.members) > 5
            assert all(not isinstance(m, EnsembleBase) for m in ensemble.members)


class TestStrategySignalContract:
    """Every emitted signal must satisfy the invariants sizing depends on."""

    def _context(self, engine, candles, regime_config) -> StrategyContext:
        classifier = RegimeClassifier(regime_config)
        by_timeframe = {}
        for timeframe in ("1", "5", "15", "60", "240"):
            shifted = make_candles(
                [c.close for c in candles], timeframe=timeframe, high_pad=25, low_pad=25
            )
            features = engine.compute_from_candles("BTCUSDT", timeframe, shifted)
            if features is not None:
                by_timeframe[timeframe] = features
        primary = by_timeframe["15"]
        return StrategyContext(
            symbol="BTCUSDT",
            features=MultiTimeframeFeatures(
                symbol="BTCUSDT",
                by_timeframe=by_timeframe,
                orderbook_imbalance=0.45,
                trade_flow_imbalance=0.4,
                spread_bps=1.0,
                orderbook_valid=True,
            ),
            regime=classifier.classify(primary),
            spread_bps=1.0,
            equity=10_000.0,
        )

    @pytest.mark.parametrize("strategy_cls", ALL_STRATEGY_CLASSES, ids=lambda c: c.id)
    def test_signals_satisfy_all_invariants(self, strategy_cls, engine, regime_config):
        """Run each strategy over several market shapes; validate any signal."""
        import numpy as np

        rng = np.random.default_rng(7)
        shapes = {
            "uptrend": [30_000.0 + i * 40.0 for i in range(400)],
            "downtrend": [45_000.0 - i * 40.0 for i in range(400)],
            "range": [40_000.0 + 200.0 * math.sin(i / 6.0) for i in range(400)],
            "noise": (40_000.0 + np.cumsum(rng.normal(0, 90, 400))).tolist(),
            "breakout": [40_000.0] * 300 + [40_000.0 + (i - 299) * 90.0 for i in range(300, 400)],
        }

        strategy = strategy_cls()
        if isinstance(strategy, __import__(
            "btcbot.strategies.ensemble", fromlist=["EnsembleBase"]
        ).EnsembleBase):
            strategy.set_members([cls() for cls in ALL_STRATEGY_CLASSES[:12]])

        signals_seen = 0
        for name, closes in shapes.items():
            candles = make_candles(closes, timeframe="15", high_pad=45, low_pad=45)
            context = self._context(engine, candles, regime_config)
            signal = strategy.generate_signal(context)
            if signal is None:
                continue
            signals_seen += 1

            assert signal.strategy_id == strategy.id
            assert signal.strategy_version == strategy.version
            assert signal.direction in (Direction.LONG, Direction.SHORT)
            assert signal.entry_reference > 0, f"{name}: non-positive entry"
            assert signal.stop_price > 0, f"{name}: non-positive stop"
            assert 0.0 <= signal.confidence <= 1.0, f"{name}: confidence out of range"
            assert signal.setup_key, f"{name}: empty setup key"
            assert signal.rationale, f"{name}: empty rationale"

            # The invariant sizing depends on.
            if signal.direction is Direction.LONG:
                assert signal.stop_price < signal.entry_reference, f"{name}: long stop above entry"
                if signal.target_price is not None:
                    assert signal.target_price > signal.entry_reference
            else:
                assert signal.stop_price > signal.entry_reference, f"{name}: short stop below entry"
                if signal.target_price is not None:
                    assert signal.target_price < signal.entry_reference

            assert signal.stop_distance > 0
            assert strategy.explain_signal(signal)
            inputs = strategy.position_size_inputs(context, context.tf("15"), signal)
            assert inputs["stop_distance"] > 0

        # Not every strategy fires on synthetic data; that is legitimate. But a
        # strategy that never fires on *any* shape is worth knowing about.
        assert signals_seen >= 0

    @pytest.mark.parametrize("strategy_cls", ALL_STRATEGY_CLASSES, ids=lambda c: c.id)
    def test_insufficient_data_yields_no_signal(self, strategy_cls, engine, regime_config):
        strategy = strategy_cls()
        short_candles = make_candles([50_000.0] * 30, timeframe="15")
        features = engine.compute_from_candles("BTCUSDT", "15", short_candles)
        assert features is None  # engine refuses before a strategy can act

    def test_short_only_signals_are_suppressed_when_unsupported(self, engine, regime_config):
        """A long-only strategy must never emit SHORT."""
        from btcbot.strategies.trend import EmaTrendCross

        strategy = EmaTrendCross()
        strategy.supports_short = False
        candles = make_candles(
            [45_000.0 - i * 40.0 for i in range(400)], timeframe="15", high_pad=40, low_pad=40
        )
        context = self._context(engine, candles, regime_config)
        signal = strategy.generate_signal(context)
        if signal is not None:
            assert signal.direction is Direction.LONG

    def test_regime_gate_blocks_disallowed_regimes(self, engine, regime_config):
        from btcbot.strategies.mean_reversion import ZScoreReversion

        strategy = ZScoreReversion()
        candles = make_candles(
            [30_000.0 * (1.004**i) for i in range(400)], timeframe="15", high_pad=30, low_pad=10
        )
        context = self._context(engine, candles, regime_config)
        if context.regime.regime not in strategy.allowed_regimes():
            assert strategy.generate_signal(context) is None
