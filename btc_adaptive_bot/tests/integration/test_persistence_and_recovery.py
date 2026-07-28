"""Database persistence, the 14-day timer, and restart recovery.

The property under test throughout: **a restart resumes, it does not reset.**
Process death, machine reboot, and reconnection must all leave the experiment
clock, the shadow equities, the allocator's learning, and any open position
exactly where they were.
"""

from __future__ import annotations

import math
from datetime import timedelta

import pytest

from btcbot.app.experiment import (
    ExperimentManager,
    ExperimentMode,
    ExperimentStatus,
    Preconditions,
)
from btcbot.config.schema import AllocatorConfig, AppConfig, ShadowConfig
from btcbot.database.db import Database
from btcbot.database.migrations import MIGRATIONS, run_migrations, schema_version
from btcbot.database.repositories import Repositories
from btcbot.utils.errors import ExperimentError
from btcbot.utils.timeutil import iso, now_utc, parse_iso

pytestmark = pytest.mark.integration


def _met() -> Preconditions:
    return Preconditions(
        config_valid=True,
        demo_authenticated=True,
        demo_verified=True,
        market_data_ready=True,
        migrations_applied=True,
    )


class TestMigrations:
    def test_migrations_apply_once(self, tmp_path):
        db = Database(tmp_path / "m.db")
        applied = run_migrations(db)
        assert applied == len(MIGRATIONS)
        assert run_migrations(db) == 0, "migrations re-applied on a second run"
        assert schema_version(db) == max(m.version for m in MIGRATIONS)
        db.close()

    def test_migrations_are_idempotent_after_interruption(self, tmp_path):
        """Replay must be safe, since DDL scripts cannot be transactional."""
        db = Database(tmp_path / "m.db")
        run_migrations(db)
        # Simulate a crash that applied the DDL but not the version row.
        db.execute("DELETE FROM schema_migrations WHERE version = ?", (1,))
        assert run_migrations(db) == 1
        assert schema_version(db) == max(m.version for m in MIGRATIONS)
        db.close()

    def test_every_expected_table_exists(self, temp_db):
        rows = temp_db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        names = {row["name"] for row in rows}
        for expected in (
            "experiments", "strategies", "strategy_versions", "signals", "shadow_trades",
            "shadow_accounts", "demo_orders", "demo_fills", "positions", "balances",
            "market_regimes", "news_events", "features", "performance_snapshots",
            "parameter_candidates", "optimization_runs", "champion_history", "system_events",
            "outages", "backtest_results", "kline_cache", "allocator_state",
            "news_effectiveness", "confidence_calibration",
        ):
            assert expected in names, f"table '{expected}' is missing"

    def test_wal_mode_is_enabled(self, temp_db):
        assert temp_db.query_one("PRAGMA journal_mode")[0].lower() == "wal"

    def test_foreign_keys_are_enforced(self, temp_db):
        assert temp_db.query_one("PRAGMA foreign_keys")[0] == 1

    def test_integrity_check_passes(self, temp_db):
        assert temp_db.integrity_check()


class TestDatabasePersistence:
    def test_data_survives_reopening(self, tmp_path):
        path = tmp_path / "persist.db"

        first = Database(path)
        run_migrations(first)
        Repositories(first).system.event("test", "hello", payload={"n": 1})
        first.close()

        second = Database(path)
        events = Repositories(second).system.recent_events(10)
        assert len(events) == 1
        assert events[0]["message"] == "hello"
        second.close()

    def test_transaction_rolls_back_on_error(self, temp_db):
        repos = Repositories(temp_db)
        repos.system.event("before", "kept")
        with pytest.raises(RuntimeError):
            with temp_db.transaction() as conn:
                conn.execute(
                    "INSERT INTO system_events (ts_utc, level, category, message) "
                    "VALUES (?, ?, ?, ?)",
                    (iso(now_utc()), "INFO", "in_txn", "should vanish"),
                )
                raise RuntimeError("boom")
        messages = {row["message"] for row in repos.system.recent_events(10)}
        assert "kept" in messages
        assert "should vanish" not in messages

    def test_backup_and_restore_round_trip(self, tmp_path):
        path = tmp_path / "backup_me.db"
        db = Database(path)
        run_migrations(db)
        Repositories(db).system.event("test", "original")

        backup = db.backup(tmp_path / "backups")
        assert backup.exists()

        Repositories(db).system.event("test", "after backup")
        assert len(Repositories(db).system.recent_events(10)) == 2

        db.restore_from(backup)
        events = Repositories(db).system.recent_events(10)
        assert len(events) == 1
        assert events[0]["message"] == "original"
        db.close()

    def test_prune_backups_keeps_the_newest(self, tmp_path):
        db = Database(tmp_path / "p.db")
        run_migrations(db)
        for _ in range(5):
            db.backup(tmp_path / "backups")
        removed = db.prune_backups(tmp_path / "backups", keep=2)
        remaining = list((tmp_path / "backups").glob("btcbot-*.db"))
        assert len(remaining) + removed == 5
        assert len(remaining) <= 2
        db.close()

    def test_kline_cache_upserts_without_duplicating(self, repos):
        candles = [(1_000, 1.0, 2.0, 0.5, 1.5, 10.0, 15.0)]
        repos.market.save_klines("BTCUSDT", "5", candles)
        repos.market.save_klines("BTCUSDT", "5", candles)
        assert len(repos.market.load_klines("BTCUSDT", "5")) == 1

        updated = [(1_000, 1.0, 9.0, 0.5, 8.0, 10.0, 15.0)]
        repos.market.save_klines("BTCUSDT", "5", updated)
        rows = repos.market.load_klines("BTCUSDT", "5")
        assert len(rows) == 1 and rows[0]["high"] == 9.0

    def test_signal_ids_are_unique(self, repos):
        signal = {
            "signal_id": "sig_dupe", "experiment_id": "exp_1", "setup_id": "set_1",
            "strategy_id": "s", "strategy_version": "1.0", "ts_utc": iso(now_utc()),
            "bar_open_ms": 1_000, "symbol": "BTCUSDT", "timeframe": "5", "direction": "long",
            "regime": "TREND_UP", "regime_confidence": 0.8, "entry_reference": 50_000.0,
            "stop_price": 49_000.0, "target_price": 52_000.0, "confidence": 0.7,
            "rr_ratio": 2.0, "accepted": True, "rejection_reason": None,
            "routed_to": "shadow", "market_features": {}, "news_features": {},
            "explanation": "test",
        }
        assert repos.signals.record(signal) is True
        assert repos.signals.record(signal) is False, "a duplicate signal was stored twice"

    def test_regime_rows_are_unique_per_bar(self, repos):
        row = {
            "experiment_id": "exp_1", "ts_utc": iso(now_utc()), "bar_open_ms": 1_000,
            "symbol": "BTCUSDT", "timeframe": "60", "regime": "TREND_UP", "confidence": 0.8,
            "adx": 30.0, "atr": 100.0, "atr_pct": 0.002, "realized_vol": 0.01,
            "ma_slope": 0.001, "trend_persistence": 0.8, "range_percentile": 50.0,
            "volume_z": 0.5, "vwap_deviation": 0.001, "evidence": {},
        }
        assert repos.market.record_regime(row) is True
        assert repos.market.record_regime(row) is False


class TestExperimentTimer:
    def test_timer_requires_all_preconditions(self, repos, app_config):
        manager = ExperimentManager(
            repos.experiments, repos.system, app_config, config_hash="abc123"
        )
        incomplete = Preconditions(config_valid=True, demo_authenticated=True)
        with pytest.raises(ExperimentError) as exc:
            manager.start_or_resume(
                mode=ExperimentMode.RESEARCH,
                preconditions=incomplete,
                starting_demo_equity=10_000.0,
                enabled_strategies=["s1"],
                strategy_versions={"s1": "1.0"},
                demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
            )
        message = str(exc.value)
        assert "demo environment verification" in message
        assert "BTC market data" in message

    def test_new_experiment_records_everything_required(self, repos, app_config):
        manager = ExperimentManager(
            repos.experiments, repos.system, app_config, config_hash="abc123"
        )
        state = manager.start_or_resume(
            mode=ExperimentMode.RESEARCH,
            preconditions=_met(),
            starting_demo_equity=9_876.54,
            enabled_strategies=["s1", "s2"],
            strategy_versions={"s1": "1.0", "s2": "2.1"},
            demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        assert state.experiment_id.startswith("exp_")
        assert state.duration_days == 14
        assert math.isclose(state.starting_demo_equity, 9_876.54)
        assert state.enabled_strategies == ["s1", "s2"]
        assert state.strategy_versions == {"s1": "1.0", "s2": "2.1"}
        assert state.config_hash == "abc123"
        assert state.software_version
        assert state.git_commit
        assert state.resumed is False
        # Exactly 14 calendar days, weekends included.
        assert math.isclose(
            (state.scheduled_end - state.start).total_seconds(), 14 * 86_400, rel_tol=1e-6
        )

    def test_restart_resumes_the_same_experiment(self, repos, app_config):
        first = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="abc123")
        original = first.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_000.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )

        # A brand-new manager, as if the process had been restarted.
        second = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="abc123")
        resumed = second.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_500.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )

        assert resumed.experiment_id == original.experiment_id, "a new experiment was created"
        assert resumed.start == original.start, "the 14-day timer was restarted"
        assert resumed.scheduled_end == original.scheduled_end
        assert resumed.resumed is True
        # The originally recorded starting equity must not be overwritten.
        assert math.isclose(resumed.starting_demo_equity, 10_000.0)

    def test_many_restarts_never_move_the_clock(self, repos, app_config):
        manager = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="h")
        first = manager.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_000.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        for _ in range(10):
            fresh = ExperimentManager(
                repos.experiments, repos.system, app_config, config_hash="h"
            )
            state = fresh.start_or_resume(
                mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=1.0,
                enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
            )
            assert state.start == first.start
            assert state.experiment_id == first.experiment_id

    def test_config_drift_is_recorded_but_not_fatal(self, repos, app_config):
        first = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="hash_a")
        first.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_000.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        second = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="hash_b")
        state = second.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_000.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        assert state.resumed
        categories = {e["category"] for e in repos.system.recent_events(20)}
        assert "config_drift" in categories

    def test_day_index_and_progress(self, repos, app_config):
        manager = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="h")
        state = manager.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_000.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        assert state.day == 1
        assert 0.0 <= state.progress_pct < 1.0
        assert not state.is_complete

        # Rewind both ends together to simulate day 8 of the same 14-day window.
        state.start = now_utc() - timedelta(days=7, hours=3)
        state.scheduled_end = state.start + timedelta(days=14)
        assert state.day == 8
        assert 45.0 < state.progress_pct < 60.0

    def test_completion_is_detected_at_the_scheduled_end(self, repos, app_config):
        manager = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="h")
        state = manager.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_000.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        assert not state.is_complete
        state.scheduled_end = now_utc() - timedelta(seconds=1)
        assert state.is_complete

    def test_countdown_lines_contain_the_required_fields(self, repos, app_config):
        manager = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="h")
        manager.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_000.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        text = "\n".join(manager.require_state().countdown_lines())
        for expected in ("DAY 1 / 14", "TIME ELAPSED", "TIME REMAINING", "START TIME", "END TIME"):
            assert expected in text

    def test_default_policy_does_not_extend_for_outages(self, repos, app_config):
        manager = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="h")
        state = manager.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_000.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        original_end = state.scheduled_end
        assert manager.apply_outage_policy(7_200) is False
        assert state.scheduled_end == original_end, "the window was silently extended"

    def test_explicit_policy_does_extend(self, repos):
        config = AppConfig(experiment={"outage_adjustment_policy": "extend_by_outage"})
        manager = ExperimentManager(repos.experiments, repos.system, config, config_hash="h")
        state = manager.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_000.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        original_end = state.scheduled_end
        assert manager.apply_outage_policy(3_600) is True
        assert state.scheduled_end == original_end + timedelta(seconds=3_600)

        stored = repos.experiments.get(state.experiment_id)
        assert parse_iso(stored["scheduled_end_ts_utc"]) == state.scheduled_end

    def test_completed_experiment_is_not_resumed(self, repos, app_config):
        manager = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="h")
        first = manager.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_000.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        manager.complete()
        assert repos.experiments.get(first.experiment_id)["status"] == ExperimentStatus.COMPLETE

        fresh = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="h")
        second = fresh.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_000.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        assert second.experiment_id != first.experiment_id
        assert second.resumed is False

    def test_start_banner_contains_the_required_lines(self, repos, app_config):
        manager = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="h")
        manager.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=9_500.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        text = "\n".join(manager.start_banner(strategy_count=38, actual_equity=9_500.0))
        assert "Environment:               DEMO" in text
        assert "Real Money:                DISABLED" in text
        assert "$9,500.00" in text
        assert "Shadow Equity Per Strategy: $10,000.00" in text
        assert "Strategies:                38" in text
        assert "Duration:                  14 days" in text
        assert "Scheduled End:" in text

    def test_outage_recording_round_trip(self, repos, app_config):
        manager = ExperimentManager(repos.experiments, repos.system, app_config, config_hash="h")
        state = manager.start_or_resume(
            mode=ExperimentMode.RESEARCH, preconditions=_met(), starting_demo_equity=10_000.0,
            enabled_strategies=["s1"], strategy_versions={"s1": "1.0"}, demo_category="SWAP", primary_symbol="BTC-USDT-SWAP",
        )
        outage_id = repos.system.start_outage(state.experiment_id, "market_data", "ws down")
        assert repos.system.open_outage(state.experiment_id, "market_data") is not None
        repos.system.end_outage(outage_id, 125)
        assert repos.system.open_outage(state.experiment_id, "market_data") is None
        assert repos.system.total_outage_seconds(state.experiment_id) == 125


class TestRestartRecovery:
    def test_open_position_is_restored_from_the_ledger(self, repos):
        """A restart must re-attach the position, not restart flat."""
        from btcbot.execution.position_ledger import PositionLedger
        from btcbot.regime.classifier import Regime
        from btcbot.strategies.base import (
            Direction,
            ExitMechanism,
            ExitPolicy,
            StrategySignal,
        )

        signal = StrategySignal(
            strategy_id="s1", strategy_version="1.0", direction=Direction.LONG,
            symbol="BTCUSDT", timeframe="15", bar_open_ms=1_000,
            entry_reference=50_000.0, stop_price=49_000.0, target_price=52_000.0,
            confidence=0.7, setup_key="k", rationale="test setup",
            exit_policy=ExitPolicy(mechanisms=frozenset({ExitMechanism.FIXED_RR})),
            regime=Regime.TREND_UP, regime_confidence=0.8,
        )

        first = PositionLedger(repos.positions, experiment_id="exp_1")
        opened = first.open(
            signal=signal, setup_id="set_1", signal_id="sig_1", category="spot",
            entry_price=50_000.0, quantity=0.01, entry_order_id="order_1",
            news_state="calm", atr=500.0,
        )
        assert first.has_open_position

        # Fresh ledger, as after a process restart.
        second = PositionLedger(repos.positions, experiment_id="exp_1")
        assert not second.has_open_position
        restored = second.restore()

        assert len(restored) == 1
        assert second.has_open_position
        position = second.current()
        assert position.position_id == opened.position_id
        assert position.strategy_id == "s1"
        assert math.isclose(position.entry_price, 50_000.0)
        assert math.isclose(position.remaining_qty, 0.01)
        # WHY / WHICH / WHEN all survive.
        assert position.reason == "test setup"
        assert position.planned_exit

    def test_closed_positions_are_not_restored(self, repos):
        from btcbot.execution.position_ledger import PositionLedger
        from btcbot.regime.classifier import Regime
        from btcbot.strategies.base import (
            Direction,
            ExitMechanism,
            ExitPolicy,
            StrategySignal,
        )

        signal = StrategySignal(
            strategy_id="s1", strategy_version="1.0", direction=Direction.LONG,
            symbol="BTCUSDT", timeframe="15", bar_open_ms=1_000, entry_reference=50_000.0,
            stop_price=49_000.0, target_price=52_000.0, confidence=0.7, setup_key="k",
            rationale="r", exit_policy=ExitPolicy(mechanisms=frozenset({ExitMechanism.FIXED_RR})),
            regime=Regime.TREND_UP, regime_confidence=0.8,
        )
        ledger = PositionLedger(repos.positions, experiment_id="exp_1")
        position = ledger.open(
            signal=signal, setup_id="set_1", signal_id="sig_1", category="spot",
            entry_price=50_000.0, quantity=0.01, entry_order_id="o1",
            news_state="calm", atr=500.0,
        )
        trade = ledger.close(
            position, exit_price=52_000.0, exit_reason="take_profit",
            exit_order_id="o2", fees=1.0,
        )
        assert math.isclose(trade["pnl"], 0.01 * 2_000 - 1.0)

        fresh = PositionLedger(repos.positions, experiment_id="exp_1")
        assert fresh.restore() == []

    def test_allocator_learning_survives_restart(self, repos):
        from btcbot.execution.allocator import DemoAllocator

        first = DemoAllocator(
            AllocatorConfig(), repos.allocator, repos.demo_orders, experiment_id="exp_1"
        )
        first.initialise(["s1", "s2"])
        for r in (1.5, -1.0, 2.0, 0.5):
            first.record_result("s1", r)
        first.confirm_allocation("s1")
        expected = first.arms["s1"].demo_expectancy
        first.persist()

        second = DemoAllocator(
            AllocatorConfig(), repos.allocator, repos.demo_orders, experiment_id="exp_1"
        )
        second.initialise(["s1", "s2"])
        assert second.arms["s1"].demo_observations == 4, "observation count was lost"
        assert math.isclose(second.arms["s1"].demo_expectancy, expected, rel_tol=1e-9)
        assert second.arms["s1"].allocations == 1
        assert second.arms["s2"].demo_observations == 0

    def test_shadow_equity_survives_restart(self, repos):
        from btcbot.shadow.engine import ShadowEngine

        first = ShadowEngine(ShadowConfig(), repos.shadow, experiment_id="exp_1", symbol="BTCUSDT")
        first.initialise(["s1", "s2"])
        first.accounts["s1"].book_trade(-1_234.0, 5.0, 2.0)
        first.persist()

        second = ShadowEngine(ShadowConfig(), repos.shadow, experiment_id="exp_1", symbol="BTCUSDT")
        second.initialise(["s1", "s2"])
        assert math.isclose(second.accounts["s1"].equity, 8_766.0)
        assert math.isclose(second.accounts["s2"].equity, 10_000.0)

    def test_in_flight_orders_are_discoverable_after_a_crash(self, repos):
        order = {
            "client_order_id": "b_crash_1", "experiment_id": "exp_1", "signal_id": "sig",
            "setup_id": "set_crash", "strategy_id": "s1", "strategy_version": "1.0",
            "signal_ts_utc": iso(now_utc()), "submitted_ts_utc": iso(now_utc()),
            "symbol": "BTCUSDT", "category": "spot", "side": "Buy", "order_type": "Market",
            "intent": "entry", "quantity": 0.001, "quantity_str": "0.001000", "price": None,
            "estimated_notional": 50.0, "stop_price": 49_000.0, "target_price": 52_000.0,
            "estimated_risk_pct": 0.0075, "regime": "TREND_UP", "confidence": 0.7,
            "sizing_reasoning": "test",
        }
        assert repos.demo_orders.reserve(order) is True
        in_flight = repos.demo_orders.in_flight()
        assert len(in_flight) == 1
        assert in_flight[0]["client_order_id"] == "b_crash_1"

        repos.demo_orders.mark_result("b_crash_1", status="filled")
        assert repos.demo_orders.in_flight() == []

    def test_news_influence_survives_restart(self, repos):
        from btcbot.config.schema import NewsConfig
        from btcbot.news.engine import NewsEngine

        repos.news.record_effectiveness(
            {
                "experiment_id": "exp_1", "ts_utc": iso(now_utc()), "gated_trades": 20,
                "ungated_trades": 20, "gated_expectancy": 0.5, "ungated_expectancy": 0.1,
                "influence": 0.65, "decision": "reduced",
            }
        )
        engine = NewsEngine(NewsConfig(providers=[]), repos.news, experiment_id="exp_1")
        engine.restore_influence()
        assert math.isclose(engine.influence, 0.65)
