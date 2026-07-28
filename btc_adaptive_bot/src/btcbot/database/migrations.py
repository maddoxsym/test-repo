"""Versioned schema migrations.

Migrations are an ordered, append-only list. Each runs exactly once inside a
transaction and is recorded in ``schema_migrations``. To change the schema, add a
new entry — never edit an applied one, because an existing 14-day experiment's
database must keep opening cleanly across restarts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..utils.errors import MigrationError
from ..utils.logging import get_logger
from .db import Database

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    # Optional Python step, run after ``sql``. Used for operations SQLite
    # cannot express idempotently in DDL (e.g. ADD COLUMN guarded by a
    # table_info check).
    python: Callable[[Database], None] | None = None


def _add_column_if_missing(db: Database, table: str, column: str, decl: str) -> None:
    """Idempotent ADD COLUMN — SQLite has no ``ADD COLUMN IF NOT EXISTS``."""
    existing = {row["name"] for row in db.query(f"PRAGMA table_info({table})")}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="core_schema",
        sql="""
        -- ============ experiments ============
        CREATE TABLE IF NOT EXISTS experiments (
            experiment_id           TEXT PRIMARY KEY,
            name                    TEXT NOT NULL,
            mode                    TEXT NOT NULL,             -- research | champion
            status                  TEXT NOT NULL,             -- pending|running|finalizing|complete|aborted
            start_ts_utc            TEXT NOT NULL,
            scheduled_end_ts_utc    TEXT NOT NULL,
            actual_end_ts_utc       TEXT,
            duration_days           INTEGER NOT NULL,
            starting_demo_equity    REAL NOT NULL,
            expected_demo_equity    REAL NOT NULL,
            shadow_equity_per_strategy REAL NOT NULL,
            enabled_strategies      TEXT NOT NULL,             -- JSON array
            strategy_versions       TEXT NOT NULL,             -- JSON object id->version
            config_hash             TEXT NOT NULL,
            software_version        TEXT NOT NULL,
            git_commit              TEXT NOT NULL,
            primary_symbol          TEXT NOT NULL,
            demo_category           TEXT NOT NULL,
            outage_policy           TEXT NOT NULL,
            metadata                TEXT,                      -- JSON
            created_at              TEXT NOT NULL
        );

        -- ============ strategies ============
        CREATE TABLE IF NOT EXISTS strategies (
            strategy_id     TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            category        TEXT NOT NULL,
            hypothesis      TEXT NOT NULL,
            primary_timeframe TEXT NOT NULL,
            supports_short  INTEGER NOT NULL,
            exit_mechanisms TEXT NOT NULL,                     -- JSON array
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS strategy_versions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id     TEXT NOT NULL REFERENCES strategies(strategy_id),
            version         TEXT NOT NULL,
            parameters      TEXT NOT NULL,                     -- JSON
            is_production   INTEGER NOT NULL DEFAULT 0,
            source          TEXT NOT NULL,                     -- initial | promoted_candidate
            created_at      TEXT NOT NULL,
            UNIQUE (strategy_id, version)
        );

        -- ============ signals (journal of every decision) ============
        CREATE TABLE IF NOT EXISTS signals (
            signal_id        TEXT PRIMARY KEY,
            experiment_id    TEXT NOT NULL,
            setup_id         TEXT NOT NULL,
            strategy_id      TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            ts_utc           TEXT NOT NULL,
            bar_open_ms      INTEGER NOT NULL,
            symbol           TEXT NOT NULL,
            timeframe        TEXT NOT NULL,
            direction        TEXT NOT NULL,                    -- long | short
            regime           TEXT NOT NULL,
            regime_confidence REAL NOT NULL,
            entry_reference  REAL NOT NULL,
            stop_price       REAL NOT NULL,
            target_price     REAL,
            confidence       REAL NOT NULL,
            rr_ratio         REAL,
            accepted         INTEGER NOT NULL,
            rejection_reason TEXT,
            routed_to        TEXT NOT NULL,                    -- shadow | demo | none
            market_features  TEXT,                             -- JSON
            news_features    TEXT,                             -- JSON
            explanation      TEXT,
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_id, ts_utc);
        CREATE INDEX IF NOT EXISTS idx_signals_experiment ON signals(experiment_id, ts_utc);
        CREATE INDEX IF NOT EXISTS idx_signals_setup ON signals(setup_id);
        CREATE INDEX IF NOT EXISTS idx_signals_accepted ON signals(accepted, strategy_id);

        -- ============ shadow trading (Layer 2) ============
        CREATE TABLE IF NOT EXISTS shadow_accounts (
            strategy_id      TEXT PRIMARY KEY,
            experiment_id    TEXT NOT NULL,
            initial_equity   REAL NOT NULL,
            equity           REAL NOT NULL,
            available        REAL NOT NULL,
            realized_pnl     REAL NOT NULL DEFAULT 0,
            unrealized_pnl   REAL NOT NULL DEFAULT 0,
            fees_paid        REAL NOT NULL DEFAULT 0,
            slippage_cost    REAL NOT NULL DEFAULT 0,
            peak_equity      REAL NOT NULL,
            max_drawdown     REAL NOT NULL DEFAULT 0,
            open_position    TEXT,                             -- JSON or NULL
            updated_at       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shadow_trades (
            trade_id         TEXT PRIMARY KEY,
            experiment_id    TEXT NOT NULL,
            signal_id        TEXT,
            setup_id         TEXT,
            strategy_id      TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            symbol           TEXT NOT NULL,
            timeframe        TEXT NOT NULL,
            direction        TEXT NOT NULL,
            sizing_model     TEXT NOT NULL,
            entry_ts_utc     TEXT NOT NULL,
            exit_ts_utc      TEXT,
            entry_price      REAL NOT NULL,
            exit_price       REAL,
            stop_price       REAL NOT NULL,
            target_price     REAL,
            quantity         REAL NOT NULL,
            notional         REAL NOT NULL,
            fees             REAL NOT NULL DEFAULT 0,
            slippage_cost    REAL NOT NULL DEFAULT 0,
            spread_cost      REAL NOT NULL DEFAULT 0,
            pnl              REAL,
            pnl_pct          REAL,
            r_multiple       REAL,
            mfe              REAL,
            mae              REAL,
            duration_seconds INTEGER,
            exit_reason      TEXT,
            entry_regime     TEXT NOT NULL,
            exit_regime      TEXT,
            news_state       TEXT,
            confidence       REAL NOT NULL,
            hour_utc         INTEGER,
            weekday_utc      INTEGER,
            volatility_state TEXT,
            is_open          INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_shadow_strategy ON shadow_trades(strategy_id, is_open);
        CREATE INDEX IF NOT EXISTS idx_shadow_exit ON shadow_trades(exit_ts_utc);
        CREATE INDEX IF NOT EXISTS idx_shadow_experiment ON shadow_trades(experiment_id);

        -- ============ actual demo execution (Layer 3) ============
        CREATE TABLE IF NOT EXISTS demo_orders (
            client_order_id  TEXT PRIMARY KEY,                 -- exchange client order id
            exchange_order_id TEXT,
            experiment_id    TEXT NOT NULL,
            signal_id        TEXT,
            setup_id         TEXT NOT NULL,
            strategy_id      TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            signal_ts_utc    TEXT NOT NULL,
            submitted_ts_utc TEXT NOT NULL,
            symbol           TEXT NOT NULL,
            category         TEXT NOT NULL,
            side             TEXT NOT NULL,
            order_type       TEXT NOT NULL,
            intent           TEXT NOT NULL,                    -- entry | exit
            quantity         REAL NOT NULL,
            quantity_str     TEXT NOT NULL,
            price            REAL,
            estimated_notional REAL NOT NULL,
            stop_price       REAL,
            target_price     REAL,
            estimated_risk_pct REAL NOT NULL,
            regime           TEXT NOT NULL,
            confidence       REAL NOT NULL,
            status           TEXT NOT NULL,                    -- submitted|accepted|rejected|filled|cancelled|failed
            reject_reason    TEXT,
            raw_response     TEXT,
            sizing_reasoning TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            UNIQUE (setup_id, intent)
        );
        CREATE INDEX IF NOT EXISTS idx_demo_orders_strategy ON demo_orders(strategy_id);
        CREATE INDEX IF NOT EXISTS idx_demo_orders_status ON demo_orders(status);
        CREATE INDEX IF NOT EXISTS idx_demo_orders_exchange ON demo_orders(exchange_order_id);

        CREATE TABLE IF NOT EXISTS demo_fills (
            fill_id          TEXT PRIMARY KEY,                 -- exchange execId
            client_order_id  TEXT,
            exchange_order_id TEXT,
            experiment_id    TEXT NOT NULL,
            strategy_id      TEXT,
            symbol           TEXT NOT NULL,
            side             TEXT NOT NULL,
            price            REAL NOT NULL,
            quantity         REAL NOT NULL,
            fee              REAL NOT NULL DEFAULT 0,
            fee_currency     TEXT,
            is_maker         INTEGER,
            exec_ts_utc      TEXT NOT NULL,
            raw              TEXT,
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_demo_fills_order ON demo_fills(client_order_id);

        -- master position ledger: why a position exists, who owns it, when it closes
        CREATE TABLE IF NOT EXISTS positions (
            position_id      TEXT PRIMARY KEY,
            experiment_id    TEXT NOT NULL,
            strategy_id      TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            setup_id         TEXT NOT NULL,
            signal_id        TEXT,
            symbol           TEXT NOT NULL,
            category         TEXT NOT NULL,
            direction        TEXT NOT NULL,
            reason           TEXT NOT NULL,                    -- WHY this position exists
            opened_ts_utc    TEXT NOT NULL,
            closed_ts_utc    TEXT,
            entry_price      REAL NOT NULL,
            exit_price       REAL,
            quantity         REAL NOT NULL,
            remaining_qty    REAL NOT NULL,
            stop_price       REAL NOT NULL,
            target_price     REAL,
            planned_exit     TEXT NOT NULL,                    -- WHEN it should be closed
            entry_order_id   TEXT,
            exit_order_id    TEXT,
            fees             REAL NOT NULL DEFAULT 0,
            realized_pnl     REAL,
            r_multiple       REAL,
            mfe              REAL,
            mae              REAL,
            exit_reason      TEXT,
            entry_regime     TEXT NOT NULL,
            news_state       TEXT,
            confidence       REAL NOT NULL,
            is_open          INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(is_open, symbol);
        CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy_id);

        CREATE TABLE IF NOT EXISTS balances (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id    TEXT NOT NULL,
            ts_utc           TEXT NOT NULL,
            account_type     TEXT NOT NULL,
            total_equity     REAL NOT NULL,
            available        REAL NOT NULL,
            wallet_balance   REAL NOT NULL,
            unrealized_pnl   REAL NOT NULL DEFAULT 0,
            coins            TEXT,                             -- JSON
            source           TEXT NOT NULL                     -- rest | ws
        );
        CREATE INDEX IF NOT EXISTS idx_balances_ts ON balances(ts_utc);

        -- ============ market context ============
        CREATE TABLE IF NOT EXISTS market_regimes (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id    TEXT,
            ts_utc           TEXT NOT NULL,
            bar_open_ms      INTEGER NOT NULL,
            symbol           TEXT NOT NULL,
            timeframe        TEXT NOT NULL,
            regime           TEXT NOT NULL,
            confidence       REAL NOT NULL,
            adx              REAL,
            atr              REAL,
            atr_pct          REAL,
            realized_vol     REAL,
            ma_slope         REAL,
            trend_persistence REAL,
            range_percentile REAL,
            volume_z         REAL,
            vwap_deviation   REAL,
            evidence         TEXT,                             -- JSON: per-feature votes
            UNIQUE (symbol, timeframe, bar_open_ms)
        );
        CREATE INDEX IF NOT EXISTS idx_regimes_ts ON market_regimes(ts_utc);

        CREATE TABLE IF NOT EXISTS news_events (
            event_id         TEXT PRIMARY KEY,
            provider         TEXT NOT NULL,
            source           TEXT NOT NULL,
            headline         TEXT NOT NULL,
            url              TEXT,
            published_ts_utc TEXT NOT NULL,
            received_ts_utc  TEXT NOT NULL,
            btc_relevance    REAL NOT NULL,
            category         TEXT NOT NULL,
            sentiment        REAL NOT NULL,
            impact           TEXT NOT NULL,                    -- low | medium | high
            freshness_minutes REAL NOT NULL,
            confidence       REAL NOT NULL,
            duplicate_cluster_id TEXT,
            raw              TEXT,
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_news_received ON news_events(received_ts_utc);
        CREATE INDEX IF NOT EXISTS idx_news_cluster ON news_events(duplicate_cluster_id);

        CREATE TABLE IF NOT EXISTS features (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc           TEXT NOT NULL,
            bar_open_ms      INTEGER NOT NULL,
            symbol           TEXT NOT NULL,
            timeframe        TEXT NOT NULL,
            payload          TEXT NOT NULL,                    -- JSON snapshot
            UNIQUE (symbol, timeframe, bar_open_ms)
        );

        -- ============ performance & learning ============
        CREATE TABLE IF NOT EXISTS performance_snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id    TEXT NOT NULL,
            ts_utc           TEXT NOT NULL,
            day_index        INTEGER NOT NULL,
            strategy_id      TEXT NOT NULL,
            layer            TEXT NOT NULL,                    -- historical|walkforward|shadow|demo
            metrics          TEXT NOT NULL,                    -- JSON
            score            REAL,
            UNIQUE (experiment_id, ts_utc, strategy_id, layer)
        );
        CREATE INDEX IF NOT EXISTS idx_perf_strategy ON performance_snapshots(strategy_id, layer);

        CREATE TABLE IF NOT EXISTS parameter_candidates (
            candidate_id     TEXT PRIMARY KEY,
            experiment_id    TEXT NOT NULL,
            strategy_id      TEXT NOT NULL,
            base_version     TEXT NOT NULL,
            candidate_version TEXT NOT NULL,
            parameters       TEXT NOT NULL,                    -- JSON
            proposed_ts_utc  TEXT NOT NULL,
            proposal_basis   TEXT NOT NULL,
            validation_ts_utc TEXT,
            validation_metrics TEXT,                           -- JSON
            production_metrics TEXT,                           -- JSON
            status           TEXT NOT NULL,                    -- proposed|validating|accepted|rejected
            decision_reason  TEXT,
            stability_score  REAL,
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_candidates_strategy ON parameter_candidates(strategy_id, status);

        CREATE TABLE IF NOT EXISTS optimization_runs (
            run_id           TEXT PRIMARY KEY,
            experiment_id    TEXT NOT NULL,
            strategy_id      TEXT NOT NULL,
            started_ts_utc   TEXT NOT NULL,
            finished_ts_utc  TEXT,
            method           TEXT NOT NULL,
            search_space     TEXT NOT NULL,                    -- JSON
            train_window     TEXT NOT NULL,
            validation_window TEXT NOT NULL,
            best_parameters  TEXT,                             -- JSON
            best_score       REAL,
            stability_report TEXT,                             -- JSON
            trials           INTEGER NOT NULL DEFAULT 0,
            status           TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS champion_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id    TEXT NOT NULL,
            ts_utc           TEXT NOT NULL,
            previous_champion TEXT,
            new_champion     TEXT NOT NULL,
            champion_type    TEXT NOT NULL,                    -- single | ensemble
            reason           TEXT NOT NULL,
            evidence         TEXT NOT NULL,                    -- JSON
            metrics          TEXT NOT NULL,                    -- JSON
            confidence       TEXT NOT NULL,                    -- LOW | MEDIUM | HIGH
            score            REAL
        );

        CREATE TABLE IF NOT EXISTS allocator_state (
            strategy_id      TEXT PRIMARY KEY,
            experiment_id    TEXT NOT NULL,
            observations     INTEGER NOT NULL DEFAULT 0,
            demo_observations INTEGER NOT NULL DEFAULT 0,
            reward_sum       REAL NOT NULL DEFAULT 0,
            reward_sq_sum    REAL NOT NULL DEFAULT 0,
            last_allocated_ts_utc TEXT,
            allocations      INTEGER NOT NULL DEFAULT 0,
            prior_mean       REAL NOT NULL DEFAULT 0,
            prior_strength   REAL NOT NULL DEFAULT 1,
            updated_at       TEXT NOT NULL
        );

        -- ============ system ============
        CREATE TABLE IF NOT EXISTS system_events (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id    TEXT,
            ts_utc           TEXT NOT NULL,
            level            TEXT NOT NULL,
            category         TEXT NOT NULL,
            message          TEXT NOT NULL,
            payload          TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts ON system_events(ts_utc);
        CREATE INDEX IF NOT EXISTS idx_events_category ON system_events(category);

        CREATE TABLE IF NOT EXISTS outages (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id    TEXT NOT NULL,
            component        TEXT NOT NULL,
            started_ts_utc   TEXT NOT NULL,
            ended_ts_utc     TEXT,
            duration_seconds INTEGER,
            reason           TEXT NOT NULL,
            recovered        INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS backtest_results (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id    TEXT,
            strategy_id      TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            run_ts_utc       TEXT NOT NULL,
            segment          TEXT NOT NULL,                    -- train|validation|oos|walkforward
            window_label     TEXT NOT NULL,
            symbol           TEXT NOT NULL,
            timeframe        TEXT NOT NULL,
            start_ts_utc     TEXT NOT NULL,
            end_ts_utc       TEXT NOT NULL,
            stress_multiplier REAL NOT NULL DEFAULT 1.0,
            metrics          TEXT NOT NULL,                    -- JSON
            trade_count      INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_backtest_strategy
            ON backtest_results(strategy_id, segment, stress_multiplier);

        CREATE TABLE IF NOT EXISTS kline_cache (
            symbol           TEXT NOT NULL,
            timeframe        TEXT NOT NULL,
            open_ms          INTEGER NOT NULL,
            open             REAL NOT NULL,
            high             REAL NOT NULL,
            low              REAL NOT NULL,
            close            REAL NOT NULL,
            volume           REAL NOT NULL,
            turnover         REAL NOT NULL,
            PRIMARY KEY (symbol, timeframe, open_ms)
        );
        """,
    ),
    Migration(
        version=2,
        name="news_effectiveness_tracking",
        sql="""
        -- Tracks whether the news filter actually improves out-of-sample results.
        -- If gated-out signals would have performed better, influence is reduced.
        CREATE TABLE IF NOT EXISTS news_effectiveness (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id    TEXT NOT NULL,
            ts_utc           TEXT NOT NULL,
            gated_trades     INTEGER NOT NULL,
            ungated_trades   INTEGER NOT NULL,
            gated_expectancy REAL NOT NULL,
            ungated_expectancy REAL NOT NULL,
            influence        REAL NOT NULL,
            decision         TEXT NOT NULL
        );
        """,
    ),
    Migration(
        version=3,
        name="confidence_calibration",
        sql="""
        -- Predicted-vs-realised win rate per confidence bucket, per strategy.
        CREATE TABLE IF NOT EXISTS confidence_calibration (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id    TEXT NOT NULL,
            strategy_id      TEXT NOT NULL,
            ts_utc           TEXT NOT NULL,
            bucket_low       REAL NOT NULL,
            bucket_high      REAL NOT NULL,
            predicted_rate   REAL NOT NULL,
            realized_rate    REAL NOT NULL,
            sample_size      INTEGER NOT NULL,
            calibration_error REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_calibration_strategy
            ON confidence_calibration(strategy_id, ts_utc);
        """,
    ),
    Migration(
        version=4,
        name="okx_perpetual_support",
        sql="""
        -- ============ leverage decisions (DYNAMIC_LEVERAGE_ENGINE audit) ============
        -- One row per leverage decision, approved or not: what was chosen,
        -- from which inputs, with the projected liquidation buffer.
        CREATE TABLE IF NOT EXISTS leverage_decisions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id      TEXT NOT NULL,
            ts_utc             TEXT NOT NULL,
            setup_id           TEXT NOT NULL,
            strategy_id        TEXT NOT NULL,
            inst_id            TEXT NOT NULL,
            direction          TEXT NOT NULL,
            approved           INTEGER NOT NULL,
            leverage           REAL NOT NULL,
            confidence         REAL NOT NULL,
            volatility_pct     REAL,
            regime             TEXT NOT NULL,
            regime_confidence  REAL NOT NULL,
            drawdown_pct       REAL NOT NULL,
            risk_state         TEXT NOT NULL,
            stop_distance_pct  REAL NOT NULL,
            est_liq_distance_pct REAL NOT NULL,
            liq_buffer_ratio   REAL NOT NULL,
            confirmed_by_exchange INTEGER NOT NULL DEFAULT 0,
            reason             TEXT NOT NULL,
            reasoning          TEXT,                            -- JSON list
            adjustments        TEXT                             -- JSON object
        );
        CREATE INDEX IF NOT EXISTS idx_leverage_setup ON leverage_decisions(setup_id);
        CREATE INDEX IF NOT EXISTS idx_leverage_strategy
            ON leverage_decisions(strategy_id, ts_utc);

        -- ============ rejected signals (per-layer decision journal) ============
        -- Every signal the decision engine refused, with the layer that refused
        -- it and the full context — so "why didn't it trade?" is answerable.
        CREATE TABLE IF NOT EXISTS rejected_signals (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id    TEXT NOT NULL,
            ts_utc           TEXT NOT NULL,
            signal_id        TEXT,
            setup_id         TEXT,
            strategy_id      TEXT NOT NULL,
            strategy_version TEXT,
            inst_id          TEXT,
            direction        TEXT,
            layer_index      INTEGER NOT NULL,
            layer_name       TEXT NOT NULL,
            reason           TEXT NOT NULL,
            detail           TEXT,                              -- JSON context
            regime           TEXT,
            confidence       REAL
        );
        CREATE INDEX IF NOT EXISTS idx_rejected_strategy
            ON rejected_signals(strategy_id, ts_utc);
        CREATE INDEX IF NOT EXISTS idx_rejected_layer ON rejected_signals(layer_name);

        -- ============ funding events (perp settlement economics) ============
        -- Realised funding payments, matched from the account bills. Funding is
        -- part of every PnL figure and every strategy score.
        CREATE TABLE IF NOT EXISTS funding_events (
            bill_id          TEXT PRIMARY KEY,
            experiment_id    TEXT NOT NULL,
            ts_utc           TEXT NOT NULL,
            inst_id          TEXT NOT NULL,
            amount           REAL NOT NULL,                     -- signed: + received, - paid
            currency         TEXT,
            funding_rate     REAL,
            position_id      TEXT,
            strategy_id      TEXT,
            raw              TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_funding_ts ON funding_events(ts_utc);
        CREATE INDEX IF NOT EXISTS idx_funding_position ON funding_events(position_id);
        """,
        python=lambda db: _okx_columns(db),
    ),
)


def _okx_columns(db: Database) -> None:
    """Perp-specific columns on the existing order/position tables."""
    _add_column_if_missing(db, "positions", "leverage", "REAL NOT NULL DEFAULT 1")
    _add_column_if_missing(db, "positions", "margin_mode", "TEXT NOT NULL DEFAULT 'isolated'")
    _add_column_if_missing(db, "positions", "contracts", "REAL")
    _add_column_if_missing(db, "positions", "liq_price_at_entry", "REAL")
    _add_column_if_missing(db, "positions", "funding_fees", "REAL NOT NULL DEFAULT 0")
    _add_column_if_missing(db, "demo_orders", "pos_side", "TEXT")
    _add_column_if_missing(db, "demo_orders", "td_mode", "TEXT")
    _add_column_if_missing(db, "demo_orders", "leverage", "REAL")
    _add_column_if_missing(db, "demo_orders", "contracts", "REAL")


def _applied_versions(db: Database) -> set[int]:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    return {int(row["version"]) for row in db.query("SELECT version FROM schema_migrations")}


def run_migrations(db: Database) -> int:
    """Apply outstanding migrations in order. Returns how many ran."""
    from ..utils.timeutil import iso, now_utc

    applied = _applied_versions(db)
    pending = [m for m in MIGRATIONS if m.version not in applied]
    if not pending:
        log.debug("DB", f"Schema up to date (version {max(applied) if applied else 0})")
        return 0

    for migration in sorted(pending, key=lambda m: m.version):
        # SQLite's executescript() implicitly commits before running, so a DDL
        # script cannot be wrapped in an explicit transaction. Every migration is
        # written to be idempotent (``CREATE ... IF NOT EXISTS``) instead, which
        # makes replay after an interrupted migration safe — the version row is
        # only written once the script has fully succeeded.
        try:
            db.executescript(migration.sql)
            if migration.python is not None:
                migration.python(db)
            db.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, iso(now_utc())),
            )
        except Exception as exc:
            raise MigrationError(
                f"migration {migration.version} ({migration.name}) failed: {exc}"
            ) from exc
        log.info("DB", f"Applied migration {migration.version}: {migration.name}")

    return len(pending)


def schema_version(db: Database) -> int:
    """Highest applied migration version (0 when the database is empty)."""
    try:
        return int(db.scalar("SELECT MAX(version) FROM schema_migrations", default=0) or 0)
    except Exception:
        return 0
