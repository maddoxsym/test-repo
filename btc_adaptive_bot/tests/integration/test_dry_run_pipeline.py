"""End-to-end pipeline test driving the real engine components.

This is the offline equivalent of ``btcbot dry-run``. It wires up the genuine
market-data store, feature engine, regime classifier, strategy registry, shadow
engine, allocator, database, and report generator, feeds them realistic candles,
and asserts that evidence actually accumulates at every layer.

It exists because a dry run against the live exchange proves nothing about the
*pipeline* — it mostly proves the network works. This proves the pipeline.

Explicitly asserted at the end: the 14-day experiment timer does **not** start.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from btcbot.config.schema import AppConfig
from btcbot.exchange.models import Candle
from btcbot.features.engine import FeatureEngine, MultiTimeframeFeatures
from btcbot.learning.metrics import compute_metrics, regime_expectancy_map
from btcbot.market_data.store import MarketDataStore
from btcbot.regime.classifier import RegimeClassifier, RegimeTracker
from btcbot.reporting.export import CsvExporter
from btcbot.reporting.reports import ReportGenerator
from btcbot.shadow.engine import ShadowEngine
from btcbot.strategies.base import StrategyContext
from btcbot.strategies.registry import StrategyRegistry
from btcbot.utils.ids import setup_id as make_setup_id
from btcbot.utils.ids import signal_id as make_signal_id
from btcbot.utils.timeutil import interval_ms, iso, now_ms, now_utc

pytestmark = pytest.mark.integration

TIMEFRAMES = ("1", "5", "15", "60", "240")
BARS = 460


def realistic_series(timeframe: str, *, seed: int, bars: int = BARS) -> list[Candle]:
    """A BTC-like series: drift, volatility clustering, and a regime shift.

    Not a random walk with constant variance — that produces markets no strategy
    reacts to. This has a trending phase, a chop phase, and a volatile expansion,
    so the different strategy families all get something to bite on.
    """
    rng = np.random.default_rng(seed)
    step = interval_ms(timeframe)
    # End on a fully closed bar so nothing in the pipeline sees a forming candle.
    end = ((now_ms() // step) * step) - step
    start = end - (bars - 1) * step

    price = 50_000.0
    closes: list[float] = []
    for index in range(bars):
        phase = index / bars
        if phase < 0.4:
            drift, vol = 0.0006, 0.004      # trending up
        elif phase < 0.7:
            drift, vol = 0.0, 0.0025        # chop
        else:
            drift, vol = -0.0004, 0.009     # volatile decline
        price *= math.exp(drift + vol * float(rng.normal()))
        closes.append(price)

    candles: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        wick = abs(close - previous) * 0.6 + close * 0.0006
        candles.append(
            Candle(
                open_ms=start + index * step,
                open=previous,
                high=max(previous, close) + wick,
                low=min(previous, close) - wick,
                close=close,
                volume=float(abs(rng.normal(120.0, 45.0))) + 10.0,
                turnover=close * 100.0,
                timeframe=timeframe,
                confirmed=True,
            )
        )
        previous = close
    return candles


@pytest.fixture
def config() -> AppConfig:
    return AppConfig()


@pytest.fixture
def store(config) -> MarketDataStore:
    market = MarketDataStore(
        "BTCUSDT", list(TIMEFRAMES), candle_buffer=config.data.candle_buffer
    )
    for index, timeframe in enumerate(TIMEFRAMES):
        market.series[timeframe].extend(realistic_series(timeframe, seed=100 + index))
    market.update_ticker_from_ws(
        {
            "symbol": "BTCUSDT", "lastPrice": "50000.0", "bid1Price": "49999.5",
            "ask1Price": "50000.5", "volume24h": "12000", "turnover24h": "600000000",
            "price24hPcnt": "0.01",
        }
    )
    market.update_orderbook(
        {
            "b": [[str(49_999.5 - i), "1.5"] for i in range(20)],
            "a": [[str(50_000.5 + i), "1.2"] for i in range(20)],
        },
        "snapshot",
    )
    market.update_trades(
        [{"T": now_ms() - i * 100, "S": "Buy" if i % 3 else "Sell", "v": "0.3"} for i in range(60)]
    )
    return market


class TestMarketDataLayer:
    def test_candles_loaded_for_every_timeframe(self, store):
        for timeframe in TIMEFRAMES:
            assert store.bars_available(timeframe) >= FeatureEngine.MIN_BARS, (
                f"{timeframe}m has too few closed bars"
            )

    def test_no_forming_candle_is_exposed(self, store):
        for timeframe in TIMEFRAMES:
            assert store.series[timeframe].forming() is None

    def test_price_and_spread_are_sane(self, store):
        assert store.last_price > 0
        assert store.spread > 0
        assert 0 < store.spread_bps < 100

    def test_microstructure_is_available(self, store):
        assert store.orderbook.valid
        assert store.orderbook.best_bid > 0 < store.orderbook.best_ask
        assert store.orderbook.imbalance() > 0      # bid-heavy book above
        assert store.trade_flow.imbalance() != 0.0

    def test_data_health_is_good(self, store):
        health = store.health()
        assert health.healthy, health.describe()

    def test_coverage_has_no_gaps(self, store):
        for timeframe, report in store.coverage_report().items():
            assert report["gaps"] == 0, f"{timeframe}m has {report['gaps']} gap(s)"
            assert math.isclose(report["coverage"], 1.0, rel_tol=1e-6)


class TestFeatureAndRegimeLayer:
    def test_features_compute_for_every_timeframe(self, store):
        engine = FeatureEngine()
        for timeframe in TIMEFRAMES:
            features = engine.compute(store.series[timeframe])
            assert features is not None, f"no features for {timeframe}m"
            for name in ("ema21", "rsi14", "atr14", "adx14", "bb_upper", "vwap48", "volume_z"):
                assert np.isfinite(features.last(name)), f"{name} is not finite on {timeframe}m"

    def test_regime_is_classified_with_evidence(self, store, config):
        engine = FeatureEngine()
        classifier = RegimeClassifier(config.regime)
        features = engine.compute(store.series[config.market.regime_timeframe])
        snapshot = classifier.classify(features)
        assert snapshot.regime is not None
        assert 0.0 <= snapshot.confidence <= 1.0
        assert len(snapshot.evidence) >= 4

    def test_regime_history_accumulates(self, store, config):
        engine = FeatureEngine()
        classifier = RegimeClassifier(config.regime)
        tracker = RegimeTracker()
        candles = store.series[config.market.regime_timeframe].closed()

        for cut in range(FeatureEngine.MIN_BARS, len(candles), 10):
            features = engine.compute_from_candles("BTCUSDT", "60", candles[:cut])
            if features is not None:
                tracker.add(classifier.classify(features))

        assert len(tracker.history(limit=1000)) > 10
        distribution = tracker.distribution()
        assert distribution and math.isclose(sum(distribution.values()), 1.0)


class TestFullPipeline:
    def test_signals_shadow_trades_and_reports_all_materialise(
        self, store, config, repos, tmp_path
    ):
        """The complete offline dry run."""
        engine = FeatureEngine()
        classifier = RegimeClassifier(config.regime)
        registry = StrategyRegistry.build(
            config.strategies, available_timeframes=list(TIMEFRAMES)
        )
        assert len(registry) >= 30

        experiment_id = "exp_dryrun"
        shadow = ShadowEngine(
            config.shadow, repos.shadow, experiment_id=experiment_id, symbol="BTCUSDT"
        )
        shadow.initialise(registry.ids)

        # Replay the 15m timeline, re-evaluating every strategy on each new bar.
        primary_candles = store.series["15"].closed()
        signals_recorded = 0
        shadow_entries = 0
        regimes_recorded = 0
        closed_trades: list[dict] = []

        for cut in range(FeatureEngine.MIN_BARS, len(primary_candles)):
            window = primary_candles[:cut]
            by_timeframe = {}
            for timeframe in TIMEFRAMES:
                series = store.series[timeframe].closed()
                if timeframe == "15":
                    subset = window
                else:
                    boundary = window[-1].open_ms
                    subset = [c for c in series if c.open_ms <= boundary]
                features = engine.compute_from_candles("BTCUSDT", timeframe, subset)
                if features is not None:
                    by_timeframe[timeframe] = features
            if "15" not in by_timeframe:
                continue

            snapshot = classifier.classify(by_timeframe["15"])
            if repos.market.record_regime(
                snapshot.to_row(experiment_id=experiment_id, ts_utc=iso(now_utc()))
            ):
                regimes_recorded += 1

            context = StrategyContext(
                symbol="BTCUSDT",
                features=MultiTimeframeFeatures(
                    symbol="BTCUSDT",
                    by_timeframe=by_timeframe,
                    orderbook_imbalance=store.orderbook.imbalance(),
                    trade_flow_imbalance=store.trade_flow.imbalance(),
                    spread_bps=store.spread_bps,
                    orderbook_valid=True,
                ),
                regime=snapshot,
                spread_bps=store.spread_bps,
                equity=10_000.0,
            )

            # Advance shadow exits on this bar first.
            closed_trades.extend(
                shadow.on_bar(window[-1], atr_by_timeframe={"15": by_timeframe["15"].last("atr14")})
            )

            for strategy in registry.by_timeframe("15"):
                signal = strategy.generate_signal(context)
                if signal is None:
                    continue

                signal_uid = make_signal_id(
                    strategy.id, strategy.version, signal.symbol, signal.timeframe,
                    signal.bar_open_ms, signal.direction.value,
                )
                setup_uid = make_setup_id(
                    strategy.id, signal.symbol, signal.timeframe,
                    signal.bar_open_ms, signal.setup_key,
                )
                inserted = repos.signals.record(
                    {
                        "signal_id": signal_uid, "experiment_id": experiment_id,
                        "setup_id": setup_uid, "strategy_id": strategy.id,
                        "strategy_version": strategy.version, "ts_utc": iso(now_utc()),
                        "bar_open_ms": signal.bar_open_ms, "symbol": signal.symbol,
                        "timeframe": signal.timeframe, "direction": signal.direction.value,
                        "regime": signal.regime.value,
                        "regime_confidence": signal.regime_confidence,
                        "entry_reference": signal.entry_reference,
                        "stop_price": signal.stop_price, "target_price": signal.target_price,
                        "confidence": signal.confidence, "rr_ratio": signal.rr_ratio,
                        "accepted": True, "rejection_reason": None, "routed_to": "shadow",
                        "market_features": signal.features_snapshot, "news_features": {},
                        "explanation": strategy.explain_signal(signal),
                    }
                )
                if not inserted:
                    continue
                signals_recorded += 1

                atr = by_timeframe["15"].last("atr14")
                if shadow.on_signal(
                    strategy, signal, atr=float(atr) if np.isfinite(atr) else 0.0,
                    news_state="calm", volatility_state="normal",
                ):
                    shadow_entries += 1

        # --- assertions on the accumulated evidence --------------------
        assert signals_recorded > 0, "no strategy produced a signal across the whole replay"
        assert shadow_entries > 0, "no shadow positions were opened"
        assert regimes_recorded > 0, "no regime rows were persisted"

        shadow.persist()
        stored_signals = repos.signals.recent(5_000, experiment_id=experiment_id)
        assert len(stored_signals) == signals_recorded

        # Distinct strategies must be participating, not just one.
        participating = {row["strategy_id"] for row in stored_signals}
        assert len(participating) >= 3, f"only {participating} produced signals"

        # Shadow accounts stayed isolated and correctly initialised.
        assert len(shadow.accounts) == len(registry)
        for account in shadow.accounts.values():
            assert account.initial_equity == 10_000.0
            assert account.equity > 0

        # Closed trades produce usable metrics.
        stored_trades = repos.shadow.closed_trades(experiment_id=experiment_id)
        if stored_trades:
            by_strategy: dict[str, list[dict]] = {}
            for trade in stored_trades:
                by_strategy.setdefault(trade["strategy_id"], []).append(trade)
            metrics_map = {
                sid: compute_metrics(
                    trades, strategy_id=sid, layer="shadow",
                    initial_equity=10_000.0, bootstrap_samples=200,
                )
                for sid, trades in by_strategy.items()
            }
            for metrics in metrics_map.values():
                assert metrics.total_trades > 0
                assert math.isfinite(metrics.expectancy_r)
                assert math.isfinite(metrics.profit_factor)
                assert 0.0 <= metrics.win_rate <= 1.0
            assert isinstance(regime_expectancy_map(metrics_map), dict)

        # --- reports and exports actually write ------------------------
        report_config = AppConfig(
            reporting={
                "output_dir": str(tmp_path / "reports"),
                "export_dir": str(tmp_path / "reports" / "exports"),
            }
        )
        generator = ReportGenerator(repos, report_config, registry=registry)
        strategy_report = generator.strategy_report(experiment_id)
        execution_report = generator.execution_report(experiment_id)
        learning_report = generator.learning_report(experiment_id)
        for path in (strategy_report, execution_report, learning_report):
            assert path.exists() and path.stat().st_size > 0, f"{path.name} is empty"

        exporter = CsvExporter(repos, tmp_path / "reports" / "exports")
        written = exporter.export_all(experiment_id)
        assert len(written) >= 8
        names = {p.name.split("_2")[0] for p in written}
        for expected in ("signals", "shadow_trades", "demo_orders", "news", "regimes"):
            assert any(expected in name for name in names), f"missing {expected} export"

        # --- the 14-day timer must NOT have started --------------------
        assert repos.experiments.find_active() is None, (
            "a dry run created an experiment and started the 14-day timer"
        )

    def test_dashboard_state_is_json_serialisable(self, store, config, repos):
        """The dashboard must never be able to crash the engine."""
        import json

        from btcbot.dashboard.server import create_app

        registry = StrategyRegistry.build(
            config.strategies, available_timeframes=list(TIMEFRAMES)
        )
        shadow = ShadowEngine(
            config.shadow, repos.shadow, experiment_id="exp_dryrun", symbol="BTCUSDT"
        )
        shadow.initialise(registry.ids)

        state = {
            "system": {"running": True, "demo_verified": False},
            "experiment": None,
            "market": store.snapshot(),
            "research": {
                "strategies": len(registry),
                "shadow": shadow.snapshot(),
                "leaderboard": shadow.leaderboard(10),
            },
            "news": repos.news.recent(5),
            "champion": repos.champion.current(),
        }
        encoded = json.dumps(state, default=str)
        assert len(encoded) > 100
        assert create_app(lambda: state) is not None

    def test_report_generation_survives_an_empty_database(self, config, repos, tmp_path):
        """Day 1 has no data; reports must still render."""
        report_config = AppConfig(
            reporting={
                "output_dir": str(tmp_path / "reports"),
                "export_dir": str(tmp_path / "reports" / "exports"),
            }
        )
        generator = ReportGenerator(repos, report_config)
        for path in (
            generator.strategy_report("exp_empty"),
            generator.execution_report("exp_empty"),
            generator.learning_report("exp_empty"),
        ):
            assert path.exists()

        written = CsvExporter(repos, tmp_path / "exports").export_all("exp_empty")
        assert written


class TestFinalReportRendering:
    def test_final_report_html_and_markdown_render(self, config, repos, tmp_path):
        from btcbot.app.experiment import ExperimentManager, ExperimentMode, Preconditions
        from btcbot.scoring.champion import ChampionSelector
        from btcbot.scoring.scorer import StrategyEvidence, StrategyScorer, rank_strategies

        manager = ExperimentManager(
            repos.experiments, repos.system, config, config_hash="dryrun_hash"
        )
        state = manager.start_or_resume(
            mode=ExperimentMode.RESEARCH,
            preconditions=Preconditions(True, True, True, True, True),
            starting_demo_equity=10_000.0,
            enabled_strategies=["s1", "s2"],
            strategy_versions={"s1": "1.0", "s2": "1.0"},
            demo_category="spot",
        )

        def evidence(strategy_id: str, r: float, trades: int) -> StrategyEvidence:
            rows = [
                {
                    "pnl": 50.0 * r, "r_multiple": r, "regime": "TREND_UP" if i % 2 else "RANGING",
                    "timeframe": "15", "hour_utc": 10, "weekday_utc": 3, "confidence": 0.6,
                    "exit_reason": "take_profit" if r > 0 else "stop_loss", "direction": "long",
                    "duration_seconds": 1800, "fees": 0.5, "slippage_cost": 0.2,
                    "spread_cost": 0.0, "mfe": 60.0, "mae": -20.0, "notional": 1_000.0,
                    "volatility_state": "normal", "news_state": "calm",
                    "exit_ts_utc": "2026-07-24T00:00:00Z",
                }
                for i in range(trades)
            ]
            metrics = compute_metrics(
                rows, strategy_id=strategy_id, layer="shadow", initial_equity=10_000.0
            )
            return StrategyEvidence(
                strategy_id=strategy_id, shadow=metrics, historical=metrics, demo=metrics,
                parameters={"rr_target": 2.0},
            )

        evidence_map = {"s1": evidence("s1", 0.9, 60), "s2": evidence("s2", -0.3, 40)}
        scorer = StrategyScorer(config.scoring)
        ranked = rank_strategies([scorer.score(e) for e in evidence_map.values()])
        selection = ChampionSelector(config.champion, repos.champion).select(
            ranked, evidence_map, experiment_id=state.experiment_id
        )

        report_config = AppConfig(
            reporting={
                "output_dir": str(tmp_path / "reports"),
                "export_dir": str(tmp_path / "reports" / "exports"),
            }
        )
        generator = ReportGenerator(repos, report_config)
        paths = generator.final_report(state, ranked, selection, evidence_map)

        names = {p.name for p in paths}
        assert "final_14_day_report.md" in names
        assert "final_14_day_report.html" in names
        assert "final_14_day_metrics.csv" in names

        html = next(p for p in paths if p.suffix == ".html").read_text(encoding="utf-8")
        assert "<!doctype html>" in html.lower()
        assert "REAL MONEY: DISABLED" in html
        assert "14-Day Bybit Demo Research" in html
        # No external asset may be referenced.
        assert "http://" not in html.replace("http://www.w3.org", "")
        for column in ("Sortino", "Max DD %", "Robust", "Final", "Confidence"):
            assert column in html

        markdown = next(p for p in paths if p.suffix == ".md").read_text(encoding="utf-8")
        assert "14-DAY RESEARCH COMPLETE" in markdown
        assert "CHAMPION" in markdown
        assert state.experiment_id in markdown

        csv_text = next(p for p in paths if p.suffix == ".csv").read_text(encoding="utf-8")
        header = csv_text.splitlines()[0]
        for column in (
            "strategy", "version", "trades", "wins", "losses", "win_rate_pct", "return_pct",
            "net_pnl", "profit_factor", "expectancy", "avg_r", "sharpe", "sortino",
            "max_drawdown_pct", "best_regime", "worst_regime", "best_timeframe",
            "historical_score", "walk_forward_score", "shadow_score", "bybit_demo_score",
            "robustness_score", "final_score",
        ):
            assert column in header, f"the metrics CSV is missing '{column}'"
