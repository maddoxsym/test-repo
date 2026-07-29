"""Repositories — the only place that knows SQL for each entity.

Engine code calls these methods rather than writing queries inline, which keeps
the schema changeable and makes the persistence layer independently testable.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..utils.errors import DatabaseError
from ..utils.timeutil import iso, now_utc
from .db import Database


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str, separators=(",", ":"))


def _unjson(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class BaseRepository:
    def __init__(self, db: Database) -> None:
        self.db = db


# ---------------------------------------------------------------- experiments


class ExperimentRepository(BaseRepository):
    """The 14-day experiment record — created once, resumed on every restart."""

    def create(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO experiments (
                experiment_id, name, mode, status, start_ts_utc, scheduled_end_ts_utc,
                actual_end_ts_utc, duration_days, starting_demo_equity, expected_demo_equity,
                shadow_equity_per_strategy, enabled_strategies, strategy_versions, config_hash,
                software_version, git_commit, primary_symbol, demo_category, outage_policy,
                metadata, created_at
            ) VALUES (
                :experiment_id, :name, :mode, :status, :start_ts_utc, :scheduled_end_ts_utc,
                :actual_end_ts_utc, :duration_days, :starting_demo_equity, :expected_demo_equity,
                :shadow_equity_per_strategy, :enabled_strategies, :strategy_versions, :config_hash,
                :software_version, :git_commit, :primary_symbol, :demo_category, :outage_policy,
                :metadata, :created_at
            )
            """,
            {
                **record,
                "enabled_strategies": _json(record.get("enabled_strategies", [])),
                "strategy_versions": _json(record.get("strategy_versions", {})),
                "metadata": _json(record.get("metadata")),
                "created_at": iso(now_utc()),
            },
        )

    def get(self, experiment_id: str) -> dict[str, Any] | None:
        return self._hydrate(
            self.db.query_one("SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,))
        )

    def find_active(self, name: str | None = None) -> dict[str, Any] | None:
        """The running experiment to resume, if any.

        This is what makes a restart resume rather than restart the timer: on
        boot the system looks for an existing non-terminal experiment before it
        would ever consider creating a new one.
        """
        if name:
            row = self.db.query_one(
                """
                SELECT * FROM experiments
                WHERE name = ? AND status IN ('pending', 'running', 'finalizing')
                ORDER BY start_ts_utc DESC LIMIT 1
                """,
                (name,),
            )
        else:
            row = self.db.query_one(
                """
                SELECT * FROM experiments
                WHERE status IN ('pending', 'running', 'finalizing')
                ORDER BY start_ts_utc DESC LIMIT 1
                """
            )
        return self._hydrate(row)

    def find_latest_complete(self, name: str | None = None) -> dict[str, Any] | None:
        if name:
            row = self.db.query_one(
                "SELECT * FROM experiments WHERE name = ? AND status = 'complete' "
                "ORDER BY start_ts_utc DESC LIMIT 1",
                (name,),
            )
        else:
            row = self.db.query_one(
                "SELECT * FROM experiments WHERE status = 'complete' "
                "ORDER BY start_ts_utc DESC LIMIT 1"
            )
        return self._hydrate(row)

    def update_status(
        self, experiment_id: str, status: str, *, actual_end_ts_utc: str | None = None
    ) -> None:
        if actual_end_ts_utc:
            self.db.execute(
                "UPDATE experiments SET status = ?, actual_end_ts_utc = ? WHERE experiment_id = ?",
                (status, actual_end_ts_utc, experiment_id),
            )
        else:
            self.db.execute(
                "UPDATE experiments SET status = ? WHERE experiment_id = ?", (status, experiment_id)
            )

    def extend_end(self, experiment_id: str, new_end_ts_utc: str) -> None:
        """Only ever called when the outage policy explicitly permits it."""
        self.db.execute(
            "UPDATE experiments SET scheduled_end_ts_utc = ? WHERE experiment_id = ?",
            (new_end_ts_utc, experiment_id),
        )

    @staticmethod
    def _hydrate(row: sqlite3.Row | None) -> dict[str, Any] | None:
        data = row_to_dict(row)
        if data is None:
            return None
        data["enabled_strategies"] = _unjson(data.get("enabled_strategies"), [])
        data["strategy_versions"] = _unjson(data.get("strategy_versions"), {})
        data["metadata"] = _unjson(data.get("metadata"), {})
        return data


# ---------------------------------------------------------------- strategies


class StrategyRepository(BaseRepository):
    def upsert_strategy(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO strategies (
                strategy_id, name, category, hypothesis, primary_timeframe,
                supports_short, exit_mechanisms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id) DO UPDATE SET
                name = excluded.name,
                category = excluded.category,
                hypothesis = excluded.hypothesis,
                primary_timeframe = excluded.primary_timeframe,
                supports_short = excluded.supports_short,
                exit_mechanisms = excluded.exit_mechanisms
            """,
            (
                record["strategy_id"],
                record["name"],
                record["category"],
                record["hypothesis"],
                record["primary_timeframe"],
                int(bool(record["supports_short"])),
                _json(record.get("exit_mechanisms", [])),
                iso(now_utc()),
            ),
        )

    def upsert_version(
        self,
        strategy_id: str,
        version: str,
        parameters: dict[str, Any],
        *,
        is_production: bool = True,
        source: str = "initial",
    ) -> None:
        self.db.execute(
            """
            INSERT INTO strategy_versions
                (strategy_id, version, parameters, is_production, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id, version) DO UPDATE SET
                parameters = excluded.parameters,
                is_production = excluded.is_production
            """,
            (strategy_id, version, _json(parameters), int(is_production), source, iso(now_utc())),
        )

    def set_production_version(self, strategy_id: str, version: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE strategy_versions SET is_production = 0 WHERE strategy_id = ?",
                (strategy_id,),
            )
            conn.execute(
                "UPDATE strategy_versions SET is_production = 1 "
                "WHERE strategy_id = ? AND version = ?",
                (strategy_id, version),
            )

    def production_version(self, strategy_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM strategy_versions WHERE strategy_id = ? AND is_production = 1",
            (strategy_id,),
        )
        data = row_to_dict(row)
        if data:
            data["parameters"] = _unjson(data.get("parameters"), {})
        return data

    def all_strategies(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM strategies ORDER BY strategy_id")
        out = []
        for row in rows:
            data = dict(row)
            data["exit_mechanisms"] = _unjson(data.get("exit_mechanisms"), [])
            data["supports_short"] = bool(data.get("supports_short"))
            out.append(data)
        return out

    def version_map(self) -> dict[str, str]:
        rows = self.db.query(
            "SELECT strategy_id, version FROM strategy_versions WHERE is_production = 1"
        )
        return {row["strategy_id"]: row["version"] for row in rows}


# ---------------------------------------------------------------- signals


class SignalRepository(BaseRepository):
    def record(self, signal: dict[str, Any]) -> bool:
        """Insert a signal. Returns False when it already existed.

        Signal IDs are deterministic, so re-evaluating the same closed bar after
        a restart hits the primary key and is silently ignored — which is exactly
        the duplicate protection we want.
        """
        try:
            self.db.execute(
                """
                INSERT INTO signals (
                    signal_id, experiment_id, setup_id, strategy_id, strategy_version, ts_utc,
                    bar_open_ms, symbol, timeframe, direction, regime, regime_confidence,
                    entry_reference, stop_price, target_price, confidence, rr_ratio, accepted,
                    rejection_reason, routed_to, market_features, news_features, explanation,
                    created_at
                ) VALUES (
                    :signal_id, :experiment_id, :setup_id, :strategy_id, :strategy_version, :ts_utc,
                    :bar_open_ms, :symbol, :timeframe, :direction, :regime, :regime_confidence,
                    :entry_reference, :stop_price, :target_price, :confidence, :rr_ratio, :accepted,
                    :rejection_reason, :routed_to, :market_features, :news_features, :explanation,
                    :created_at
                )
                """,
                {
                    **signal,
                    "accepted": int(bool(signal.get("accepted"))),
                    "market_features": _json(signal.get("market_features")),
                    "news_features": _json(signal.get("news_features")),
                    "created_at": iso(now_utc()),
                },
            )
            return True
        except DatabaseError as exc:
            if "UNIQUE constraint failed" in str(exc):
                return False
            raise

    def exists(self, signal_id: str) -> bool:
        return self.db.query_one("SELECT 1 FROM signals WHERE signal_id = ?", (signal_id,)) is not None

    def recent(self, limit: int = 100, *, experiment_id: str | None = None) -> list[dict[str, Any]]:
        if experiment_id:
            rows = self.db.query(
                "SELECT * FROM signals WHERE experiment_id = ? ORDER BY ts_utc DESC LIMIT ?",
                (experiment_id, limit),
            )
        else:
            rows = self.db.query("SELECT * FROM signals ORDER BY ts_utc DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def rejection_summary(self, experiment_id: str) -> list[dict[str, Any]]:
        """Why signals were rejected — used to judge whether filters earn their keep."""
        rows = self.db.query(
            """
            SELECT strategy_id, rejection_reason, COUNT(*) AS count
            FROM signals
            WHERE experiment_id = ? AND accepted = 0 AND rejection_reason IS NOT NULL
            GROUP BY strategy_id, rejection_reason
            ORDER BY count DESC
            """,
            (experiment_id,),
        )
        return [dict(row) for row in rows]

    def counts_by_strategy(self, experiment_id: str) -> dict[str, dict[str, int]]:
        rows = self.db.query(
            """
            SELECT strategy_id,
                   SUM(accepted) AS accepted,
                   COUNT(*) - SUM(accepted) AS rejected,
                   COUNT(*) AS total
            FROM signals WHERE experiment_id = ?
            GROUP BY strategy_id
            """,
            (experiment_id,),
        )
        return {
            row["strategy_id"]: {
                "accepted": int(row["accepted"] or 0),
                "rejected": int(row["rejected"] or 0),
                "total": int(row["total"] or 0),
            }
            for row in rows
        }


# ---------------------------------------------------------------- shadow


class ShadowRepository(BaseRepository):
    def upsert_account(self, account: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO shadow_accounts (
                strategy_id, experiment_id, initial_equity, equity, available, realized_pnl,
                unrealized_pnl, fees_paid, slippage_cost, peak_equity, max_drawdown,
                open_position, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id) DO UPDATE SET
                equity = excluded.equity,
                available = excluded.available,
                realized_pnl = excluded.realized_pnl,
                unrealized_pnl = excluded.unrealized_pnl,
                fees_paid = excluded.fees_paid,
                slippage_cost = excluded.slippage_cost,
                peak_equity = excluded.peak_equity,
                max_drawdown = excluded.max_drawdown,
                open_position = excluded.open_position,
                updated_at = excluded.updated_at
            """,
            (
                account["strategy_id"], account["experiment_id"], account["initial_equity"],
                account["equity"], account["available"], account.get("realized_pnl", 0.0),
                account.get("unrealized_pnl", 0.0), account.get("fees_paid", 0.0),
                account.get("slippage_cost", 0.0), account["peak_equity"],
                account.get("max_drawdown", 0.0), _json(account.get("open_position")),
                iso(now_utc()),
            ),
        )

    def load_accounts(self, experiment_id: str) -> dict[str, dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM shadow_accounts WHERE experiment_id = ?", (experiment_id,)
        )
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            data = dict(row)
            data["open_position"] = _unjson(data.get("open_position"))
            out[data["strategy_id"]] = data
        return out

    def open_trade(self, trade: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO shadow_trades (
                trade_id, experiment_id, signal_id, setup_id, strategy_id, strategy_version,
                symbol, timeframe, direction, sizing_model, entry_ts_utc, entry_price, stop_price,
                target_price, quantity, notional, fees, slippage_cost, spread_cost, entry_regime,
                news_state, confidence, hour_utc, weekday_utc, volatility_state, is_open, created_at
            ) VALUES (
                :trade_id, :experiment_id, :signal_id, :setup_id, :strategy_id, :strategy_version,
                :symbol, :timeframe, :direction, :sizing_model, :entry_ts_utc, :entry_price,
                :stop_price, :target_price, :quantity, :notional, :fees, :slippage_cost,
                :spread_cost, :entry_regime, :news_state, :confidence, :hour_utc, :weekday_utc,
                :volatility_state, 1, :created_at
            )
            """,
            {**trade, "created_at": iso(now_utc())},
        )

    def close_trade(self, trade_id: str, closure: dict[str, Any]) -> None:
        self.db.execute(
            """
            UPDATE shadow_trades SET
                exit_ts_utc = :exit_ts_utc, exit_price = :exit_price, fees = :fees,
                slippage_cost = :slippage_cost, spread_cost = :spread_cost, pnl = :pnl,
                pnl_pct = :pnl_pct, r_multiple = :r_multiple, mfe = :mfe, mae = :mae,
                duration_seconds = :duration_seconds, exit_reason = :exit_reason,
                exit_regime = :exit_regime, is_open = 0
            WHERE trade_id = :trade_id
            """,
            {**closure, "trade_id": trade_id},
        )

    def open_trades(self, experiment_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM shadow_trades WHERE experiment_id = ? AND is_open = 1", (experiment_id,)
        )
        return [dict(row) for row in rows]

    def closed_trades(
        self, *, experiment_id: str | None = None, strategy_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["is_open = 0"]
        params: list[Any] = []
        if experiment_id:
            clauses.append("experiment_id = ?")
            params.append(experiment_id)
        if strategy_id:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        rows = self.db.query(
            f"SELECT * FROM shadow_trades WHERE {' AND '.join(clauses)} ORDER BY exit_ts_utc",
            params,
        )
        return [dict(row) for row in rows]

    def count(self, experiment_id: str) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM shadow_trades WHERE experiment_id = ?",
                (experiment_id,),
                default=0,
            )
        )


# ---------------------------------------------------------------- demo orders


class DemoOrderRepository(BaseRepository):
    def reserve(self, order: dict[str, Any]) -> bool:
        """Insert an order row *before* sending it to the exchange.

        The ``UNIQUE(setup_id, intent)`` constraint is the authoritative
        duplicate-order guard: if the same setup is somehow processed twice
        (reconnect, race, restart), the second insert fails and no HTTP request
        is ever made.
        """
        try:
            self.db.execute(
                """
                INSERT INTO demo_orders (
                    client_order_id, exchange_order_id, experiment_id, signal_id, setup_id,
                    strategy_id, strategy_version, signal_ts_utc, submitted_ts_utc, symbol,
                    category, side, order_type, intent, quantity, quantity_str, price,
                    estimated_notional, stop_price, target_price, estimated_risk_pct, regime,
                    confidence, status, sizing_reasoning, pos_side, td_mode, leverage,
                    contracts, created_at, updated_at
                ) VALUES (
                    :client_order_id, NULL, :experiment_id, :signal_id, :setup_id,
                    :strategy_id, :strategy_version, :signal_ts_utc, :submitted_ts_utc, :symbol,
                    :category, :side, :order_type, :intent, :quantity, :quantity_str, :price,
                    :estimated_notional, :stop_price, :target_price, :estimated_risk_pct, :regime,
                    :confidence, 'submitted', :sizing_reasoning, :pos_side, :td_mode, :leverage,
                    :contracts, :now, :now
                )
                """,
                {
                    "pos_side": None,
                    "td_mode": None,
                    "leverage": None,
                    "contracts": None,
                    **order,
                    "now": iso(now_utc()),
                },
            )
            return True
        except DatabaseError as exc:
            if "UNIQUE constraint failed" in str(exc):
                return False
            raise

    def mark_result(
        self,
        client_order_id: str,
        *,
        status: str,
        exchange_order_id: str | None = None,
        reject_reason: str | None = None,
        raw_response: Any = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE demo_orders SET
                status = ?, exchange_order_id = COALESCE(?, exchange_order_id),
                reject_reason = ?, raw_response = ?, updated_at = ?
            WHERE client_order_id = ?
            """,
            (status, exchange_order_id, reject_reason, _json(raw_response), iso(now_utc()), client_order_id),
        )

    def get(self, client_order_id: str) -> dict[str, Any] | None:
        return row_to_dict(
            self.db.query_one("SELECT * FROM demo_orders WHERE client_order_id = ?", (client_order_id,))
        )

    def get_by_exchange_order_id(self, exchange_order_id: str) -> dict[str, Any] | None:
        """Look an order up by OKX's ``ordId``.

        Fills frequently arrive with an empty ``clOrdId`` — OKX only echoes it
        on some surfaces — so ``ordId`` is the identifier that reliably ties a
        fill back to the order that produced it.
        """
        if not exchange_order_id:
            return None
        return row_to_dict(
            self.db.query_one(
                "SELECT * FROM demo_orders WHERE exchange_order_id = ?", (exchange_order_id,)
            )
        )

    def has_open_intent(self, setup_id: str, intent: str) -> bool:
        return (
            self.db.query_one(
                "SELECT 1 FROM demo_orders WHERE setup_id = ? AND intent = ?", (setup_id, intent)
            )
            is not None
        )

    def in_flight(self) -> list[dict[str, Any]]:
        """Orders sent but not yet resolved — reconciled on startup/reconnect."""
        rows = self.db.query("SELECT * FROM demo_orders WHERE status IN ('submitted', 'accepted')")
        return [dict(row) for row in rows]

    def recent(self, limit: int = 50, *, experiment_id: str | None = None) -> list[dict[str, Any]]:
        if experiment_id:
            rows = self.db.query(
                "SELECT * FROM demo_orders WHERE experiment_id = ? ORDER BY submitted_ts_utc DESC LIMIT ?",
                (experiment_id, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM demo_orders ORDER BY submitted_ts_utc DESC LIMIT ?", (limit,)
            )
        return [dict(row) for row in rows]

    def count_since(self, experiment_id: str, since_ts_utc: str, *, intent: str = "entry") -> int:
        return int(
            self.db.scalar(
                """
                SELECT COUNT(*) FROM demo_orders
                WHERE experiment_id = ? AND submitted_ts_utc >= ? AND intent = ?
                  AND status != 'failed'
                """,
                (experiment_id, since_ts_utc, intent),
                default=0,
            )
        )

    def record_fill(self, fill: dict[str, Any]) -> bool:
        try:
            self.db.execute(
                """
                INSERT INTO demo_fills (
                    fill_id, client_order_id, exchange_order_id, experiment_id, strategy_id,
                    symbol, side, price, quantity, fee, fee_currency, is_maker, exec_ts_utc,
                    raw, created_at
                ) VALUES (
                    :fill_id, :client_order_id, :exchange_order_id, :experiment_id, :strategy_id,
                    :symbol, :side, :price, :quantity, :fee, :fee_currency, :is_maker,
                    :exec_ts_utc, :raw, :created_at
                )
                """,
                {**fill, "raw": _json(fill.get("raw")), "created_at": iso(now_utc())},
            )
            return True
        except DatabaseError as exc:
            if "UNIQUE constraint failed" in str(exc):
                return False  # exchange re-sent a known execution
            raise

    def fills_for(self, client_order_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM demo_fills WHERE client_order_id = ? ORDER BY exec_ts_utc",
            (client_order_id,),
        )
        return [dict(row) for row in rows]


# ---------------------------------------------------------------- positions


class PositionRepository(BaseRepository):
    """Master position ledger — every demo position's owner, reason, and exit plan."""

    def open(self, position: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO positions (
                position_id, experiment_id, strategy_id, strategy_version, setup_id, signal_id,
                symbol, category, direction, reason, opened_ts_utc, entry_price, quantity,
                remaining_qty, stop_price, target_price, planned_exit, entry_order_id, fees,
                entry_regime, news_state, confidence, leverage, margin_mode, contracts,
                liq_price_at_entry, is_open, created_at, updated_at
            ) VALUES (
                :position_id, :experiment_id, :strategy_id, :strategy_version, :setup_id,
                :signal_id, :symbol, :category, :direction, :reason, :opened_ts_utc, :entry_price,
                :quantity, :remaining_qty, :stop_price, :target_price, :planned_exit,
                :entry_order_id, :fees, :entry_regime, :news_state, :confidence, :leverage,
                :margin_mode, :contracts, :liq_price_at_entry, 1, :now, :now
            )
            """,
            {
                "leverage": 1.0,
                "margin_mode": "isolated",
                "contracts": None,
                "liq_price_at_entry": None,
                **position,
                "now": iso(now_utc()),
            },
        )

    def add_funding_fee(self, position_id: str, amount: float) -> None:
        """Accumulate a funding payment onto the position (signed; + = paid)."""
        self.db.execute(
            "UPDATE positions SET funding_fees = funding_fees + ?, updated_at = ? "
            "WHERE position_id = ?",
            (amount, iso(now_utc()), position_id),
        )

    def close(self, position_id: str, closure: dict[str, Any]) -> None:
        self.db.execute(
            """
            UPDATE positions SET
                closed_ts_utc = :closed_ts_utc, exit_price = :exit_price, remaining_qty = 0,
                realized_pnl = :realized_pnl, r_multiple = :r_multiple, mfe = :mfe, mae = :mae,
                exit_reason = :exit_reason, exit_order_id = :exit_order_id, fees = :fees,
                is_open = 0, updated_at = :now
            WHERE position_id = :position_id
            """,
            {**closure, "position_id": position_id, "now": iso(now_utc())},
        )

    def update_excursions(self, position_id: str, *, mfe: float, mae: float) -> None:
        self.db.execute(
            "UPDATE positions SET mfe = ?, mae = ?, updated_at = ? WHERE position_id = ?",
            (mfe, mae, iso(now_utc()), position_id),
        )

    def update_stop(self, position_id: str, stop_price: float, *, planned_exit: str | None = None) -> None:
        if planned_exit:
            self.db.execute(
                "UPDATE positions SET stop_price = ?, planned_exit = ?, updated_at = ? WHERE position_id = ?",
                (stop_price, planned_exit, iso(now_utc()), position_id),
            )
        else:
            self.db.execute(
                "UPDATE positions SET stop_price = ?, updated_at = ? WHERE position_id = ?",
                (stop_price, iso(now_utc()), position_id),
            )

    def reduce(self, position_id: str, *, remaining_qty: float) -> None:
        self.db.execute(
            "UPDATE positions SET remaining_qty = ?, updated_at = ? WHERE position_id = ?",
            (remaining_qty, iso(now_utc()), position_id),
        )

    def open_positions(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol:
            rows = self.db.query(
                "SELECT * FROM positions WHERE is_open = 1 AND symbol = ?", (symbol,)
            )
        else:
            rows = self.db.query("SELECT * FROM positions WHERE is_open = 1")
        return [dict(row) for row in rows]

    def closed_positions(self, experiment_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM positions WHERE experiment_id = ? AND is_open = 0 ORDER BY closed_ts_utc",
            (experiment_id,),
        )
        return [dict(row) for row in rows]

    def get(self, position_id: str) -> dict[str, Any] | None:
        return row_to_dict(
            self.db.query_one("SELECT * FROM positions WHERE position_id = ?", (position_id,))
        )


# ---------------------------------------------------------------- market/news


class MarketRepository(BaseRepository):
    def record_balance(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO balances (
                experiment_id, ts_utc, account_type, total_equity, available, wallet_balance,
                unrealized_pnl, coins, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["experiment_id"], record["ts_utc"], record["account_type"],
                record["total_equity"], record["available"], record["wallet_balance"],
                record.get("unrealized_pnl", 0.0), _json(record.get("coins")), record["source"],
            ),
        )

    def latest_balance(self, experiment_id: str) -> dict[str, Any] | None:
        data = row_to_dict(
            self.db.query_one(
                "SELECT * FROM balances WHERE experiment_id = ? ORDER BY ts_utc DESC LIMIT 1",
                (experiment_id,),
            )
        )
        if data:
            data["coins"] = _unjson(data.get("coins"), {})
        return data

    def record_regime(self, record: dict[str, Any]) -> bool:
        try:
            self.db.execute(
                """
                INSERT INTO market_regimes (
                    experiment_id, ts_utc, bar_open_ms, symbol, timeframe, regime, confidence,
                    adx, atr, atr_pct, realized_vol, ma_slope, trend_persistence,
                    range_percentile, volume_z, vwap_deviation, evidence
                ) VALUES (
                    :experiment_id, :ts_utc, :bar_open_ms, :symbol, :timeframe, :regime,
                    :confidence, :adx, :atr, :atr_pct, :realized_vol, :ma_slope,
                    :trend_persistence, :range_percentile, :volume_z, :vwap_deviation, :evidence
                )
                """,
                {**record, "evidence": _json(record.get("evidence"))},
            )
            return True
        except DatabaseError as exc:
            if "UNIQUE constraint failed" in str(exc):
                return False
            raise

    def regime_history(self, symbol: str, timeframe: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT * FROM market_regimes WHERE symbol = ? AND timeframe = ?
            ORDER BY bar_open_ms DESC LIMIT ?
            """,
            (symbol, timeframe, limit),
        )
        return [dict(row) for row in rows]

    def save_klines(self, symbol: str, timeframe: str, candles: list[tuple[Any, ...]]) -> None:
        """Persist candles to the local historical cache (idempotent)."""
        self.db.executemany(
            """
            INSERT INTO kline_cache (symbol, timeframe, open_ms, open, high, low, close, volume, turnover)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, open_ms) DO UPDATE SET
                open = excluded.open, high = excluded.high, low = excluded.low,
                close = excluded.close, volume = excluded.volume, turnover = excluded.turnover
            """,
            [(symbol, timeframe, *candle) for candle in candles],
        )

    def load_klines(
        self, symbol: str, timeframe: str, *, start_ms: int | None = None, end_ms: int | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["symbol = ?", "timeframe = ?"]
        params: list[Any] = [symbol, timeframe]
        if start_ms is not None:
            clauses.append("open_ms >= ?")
            params.append(start_ms)
        if end_ms is not None:
            clauses.append("open_ms <= ?")
            params.append(end_ms)
        rows = self.db.query(
            f"SELECT * FROM kline_cache WHERE {' AND '.join(clauses)} ORDER BY open_ms", params
        )
        return [dict(row) for row in rows]

    def kline_range(self, symbol: str, timeframe: str) -> tuple[int | None, int | None]:
        row = self.db.query_one(
            "SELECT MIN(open_ms) AS lo, MAX(open_ms) AS hi FROM kline_cache "
            "WHERE symbol = ? AND timeframe = ?",
            (symbol, timeframe),
        )
        if row is None:
            return (None, None)
        return (row["lo"], row["hi"])

    def save_features(self, symbol: str, timeframe: str, bar_open_ms: int, payload: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO features (ts_utc, bar_open_ms, symbol, timeframe, payload)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, bar_open_ms) DO UPDATE SET payload = excluded.payload
            """,
            (iso(now_utc()), bar_open_ms, symbol, timeframe, _json(payload)),
        )


class NewsRepository(BaseRepository):
    def record(self, event: dict[str, Any]) -> bool:
        try:
            self.db.execute(
                """
                INSERT INTO news_events (
                    event_id, provider, source, headline, url, published_ts_utc, received_ts_utc,
                    btc_relevance, category, sentiment, impact, freshness_minutes, confidence,
                    duplicate_cluster_id, raw, created_at
                ) VALUES (
                    :event_id, :provider, :source, :headline, :url, :published_ts_utc,
                    :received_ts_utc, :btc_relevance, :category, :sentiment, :impact,
                    :freshness_minutes, :confidence, :duplicate_cluster_id, :raw, :created_at
                )
                """,
                {**event, "raw": _json(event.get("raw")), "created_at": iso(now_utc())},
            )
            return True
        except DatabaseError as exc:
            if "UNIQUE constraint failed" in str(exc):
                return False
            raise

    def known_before(self, ts_utc: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """News **received** before ``ts_utc`` — the point-in-time guard.

        Filtering on ``received_ts_utc`` rather than ``published_ts_utc`` is the
        whole point: a decision may only use information the system actually had
        at the time, not information that was published earlier but arrived late.
        """
        rows = self.db.query(
            "SELECT * FROM news_events WHERE received_ts_utc < ? ORDER BY received_ts_utc DESC LIMIT ?",
            (ts_utc, limit),
        )
        return [dict(row) for row in rows]

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM news_events ORDER BY received_ts_utc DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]

    def find_cluster(self, headline_tokens: str) -> str | None:
        row = self.db.query_one(
            "SELECT duplicate_cluster_id FROM news_events WHERE duplicate_cluster_id = ? LIMIT 1",
            (headline_tokens,),
        )
        return row["duplicate_cluster_id"] if row else None

    def record_effectiveness(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO news_effectiveness (
                experiment_id, ts_utc, gated_trades, ungated_trades, gated_expectancy,
                ungated_expectancy, influence, decision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["experiment_id"], record["ts_utc"], record["gated_trades"],
                record["ungated_trades"], record["gated_expectancy"],
                record["ungated_expectancy"], record["influence"], record["decision"],
            ),
        )

    def latest_influence(self, experiment_id: str, default: float = 1.0) -> float:
        value = self.db.scalar(
            "SELECT influence FROM news_effectiveness WHERE experiment_id = ? "
            "ORDER BY ts_utc DESC LIMIT 1",
            (experiment_id,),
            default=default,
        )
        return float(value)


# ---------------------------------------------------------------- learning


class PerformanceRepository(BaseRepository):
    def snapshot(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO performance_snapshots
                (experiment_id, ts_utc, day_index, strategy_id, layer, metrics, score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id, ts_utc, strategy_id, layer) DO UPDATE SET
                metrics = excluded.metrics, score = excluded.score
            """,
            (
                record["experiment_id"], record["ts_utc"], record["day_index"],
                record["strategy_id"], record["layer"], _json(record["metrics"]),
                record.get("score"),
            ),
        )

    def latest_by_layer(self, experiment_id: str, layer: str) -> dict[str, dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT p.strategy_id, p.metrics, p.score FROM performance_snapshots p
            INNER JOIN (
                SELECT strategy_id, MAX(ts_utc) AS ts FROM performance_snapshots
                WHERE experiment_id = ? AND layer = ? GROUP BY strategy_id
            ) latest ON latest.strategy_id = p.strategy_id AND latest.ts = p.ts_utc
            WHERE p.experiment_id = ? AND p.layer = ?
            """,
            (experiment_id, layer, experiment_id, layer),
        )
        return {
            row["strategy_id"]: {"metrics": _unjson(row["metrics"], {}), "score": row["score"]}
            for row in rows
        }

    def history(self, experiment_id: str, strategy_id: str, layer: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT * FROM performance_snapshots
            WHERE experiment_id = ? AND strategy_id = ? AND layer = ? ORDER BY ts_utc
            """,
            (experiment_id, strategy_id, layer),
        )
        out = []
        for row in rows:
            data = dict(row)
            data["metrics"] = _unjson(data["metrics"], {})
            out.append(data)
        return out

    def record_backtest(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO backtest_results (
                experiment_id, strategy_id, strategy_version, run_ts_utc, segment, window_label,
                symbol, timeframe, start_ts_utc, end_ts_utc, stress_multiplier, metrics, trade_count
            ) VALUES (
                :experiment_id, :strategy_id, :strategy_version, :run_ts_utc, :segment,
                :window_label, :symbol, :timeframe, :start_ts_utc, :end_ts_utc,
                :stress_multiplier, :metrics, :trade_count
            )
            """,
            {**record, "metrics": _json(record["metrics"])},
        )

    def backtest_results(
        self, strategy_id: str, *, segment: str | None = None, stress_multiplier: float | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["strategy_id = ?"]
        params: list[Any] = [strategy_id]
        if segment:
            clauses.append("segment = ?")
            params.append(segment)
        if stress_multiplier is not None:
            clauses.append("stress_multiplier = ?")
            params.append(stress_multiplier)
        rows = self.db.query(
            f"SELECT * FROM backtest_results WHERE {' AND '.join(clauses)} ORDER BY run_ts_utc",
            params,
        )
        out = []
        for row in rows:
            data = dict(row)
            data["metrics"] = _unjson(data["metrics"], {})
            out.append(data)
        return out

    def record_calibration(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO confidence_calibration (
                experiment_id, strategy_id, ts_utc, bucket_low, bucket_high, predicted_rate,
                realized_rate, sample_size, calibration_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["experiment_id"], record["strategy_id"], record["ts_utc"],
                record["bucket_low"], record["bucket_high"], record["predicted_rate"],
                record["realized_rate"], record["sample_size"], record["calibration_error"],
            ),
        )


class CandidateRepository(BaseRepository):
    def propose(self, candidate: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO parameter_candidates (
                candidate_id, experiment_id, strategy_id, base_version, candidate_version,
                parameters, proposed_ts_utc, proposal_basis, status, stability_score, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
            """,
            (
                candidate["candidate_id"], candidate["experiment_id"], candidate["strategy_id"],
                candidate["base_version"], candidate["candidate_version"],
                _json(candidate["parameters"]), candidate["proposed_ts_utc"],
                candidate["proposal_basis"], candidate.get("stability_score"), iso(now_utc()),
            ),
        )

    def decide(
        self,
        candidate_id: str,
        *,
        status: str,
        reason: str,
        validation_metrics: dict[str, Any] | None = None,
        production_metrics: dict[str, Any] | None = None,
        stability_score: float | None = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE parameter_candidates SET
                status = ?, decision_reason = ?, validation_ts_utc = ?,
                validation_metrics = ?, production_metrics = ?,
                stability_score = COALESCE(?, stability_score)
            WHERE candidate_id = ?
            """,
            (
                status, reason, iso(now_utc()), _json(validation_metrics),
                _json(production_metrics), stability_score, candidate_id,
            ),
        )

    def pending(self, experiment_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM parameter_candidates WHERE experiment_id = ? AND status IN ('proposed','validating')",
            (experiment_id,),
        )
        out = []
        for row in rows:
            data = dict(row)
            data["parameters"] = _unjson(data["parameters"], {})
            out.append(data)
        return out

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM parameter_candidates ORDER BY proposed_ts_utc DESC LIMIT ?", (limit,)
        )
        out = []
        for row in rows:
            data = dict(row)
            data["parameters"] = _unjson(data["parameters"], {})
            data["validation_metrics"] = _unjson(data.get("validation_metrics"), {})
            out.append(data)
        return out

    def count_for_strategy(self, strategy_id: str, experiment_id: str) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM parameter_candidates WHERE strategy_id = ? AND experiment_id = ?",
                (strategy_id, experiment_id),
                default=0,
            )
        )


class AllocatorRepository(BaseRepository):
    def upsert(self, state: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO allocator_state (
                strategy_id, experiment_id, observations, demo_observations, reward_sum,
                reward_sq_sum, last_allocated_ts_utc, allocations, prior_mean, prior_strength,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id) DO UPDATE SET
                observations = excluded.observations,
                demo_observations = excluded.demo_observations,
                reward_sum = excluded.reward_sum,
                reward_sq_sum = excluded.reward_sq_sum,
                last_allocated_ts_utc = excluded.last_allocated_ts_utc,
                allocations = excluded.allocations,
                prior_mean = excluded.prior_mean,
                prior_strength = excluded.prior_strength,
                updated_at = excluded.updated_at
            """,
            (
                state["strategy_id"], state["experiment_id"], state.get("observations", 0),
                state.get("demo_observations", 0), state.get("reward_sum", 0.0),
                state.get("reward_sq_sum", 0.0), state.get("last_allocated_ts_utc"),
                state.get("allocations", 0), state.get("prior_mean", 0.0),
                state.get("prior_strength", 1.0), iso(now_utc()),
            ),
        )

    def load(self, experiment_id: str) -> dict[str, dict[str, Any]]:
        rows = self.db.query("SELECT * FROM allocator_state WHERE experiment_id = ?", (experiment_id,))
        return {row["strategy_id"]: dict(row) for row in rows}


class ChampionRepository(BaseRepository):
    def record(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO champion_history (
                experiment_id, ts_utc, previous_champion, new_champion, champion_type,
                reason, evidence, metrics, confidence, score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["experiment_id"], record["ts_utc"], record.get("previous_champion"),
                record["new_champion"], record["champion_type"], record["reason"],
                _json(record["evidence"]), _json(record["metrics"]), record["confidence"],
                record.get("score"),
            ),
        )

    def current(self, experiment_id: str | None = None) -> dict[str, Any] | None:
        if experiment_id:
            row = self.db.query_one(
                "SELECT * FROM champion_history WHERE experiment_id = ? ORDER BY ts_utc DESC LIMIT 1",
                (experiment_id,),
            )
        else:
            row = self.db.query_one("SELECT * FROM champion_history ORDER BY ts_utc DESC LIMIT 1")
        data = row_to_dict(row)
        if data:
            data["evidence"] = _unjson(data.get("evidence"), {})
            data["metrics"] = _unjson(data.get("metrics"), {})
        return data

    def history(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM champion_history ORDER BY ts_utc DESC")
        out = []
        for row in rows:
            data = dict(row)
            data["evidence"] = _unjson(data.get("evidence"), {})
            data["metrics"] = _unjson(data.get("metrics"), {})
            out.append(data)
        return out


class SystemRepository(BaseRepository):
    def event(
        self,
        category: str,
        message: str,
        *,
        level: str = "INFO",
        experiment_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO system_events (experiment_id, ts_utc, level, category, message, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (experiment_id, iso(now_utc()), level, category, message, _json(payload)),
        )

    def recent_events(self, limit: int = 100, *, category: str | None = None) -> list[dict[str, Any]]:
        if category:
            rows = self.db.query(
                "SELECT * FROM system_events WHERE category = ? ORDER BY ts_utc DESC LIMIT ?",
                (category, limit),
            )
        else:
            rows = self.db.query("SELECT * FROM system_events ORDER BY ts_utc DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def start_outage(self, experiment_id: str, component: str, reason: str) -> int:
        cursor = self.db.execute(
            """
            INSERT INTO outages (experiment_id, component, started_ts_utc, reason, recovered)
            VALUES (?, ?, ?, ?, 0)
            """,
            (experiment_id, component, iso(now_utc()), reason),
        )
        return int(cursor.lastrowid or 0)

    def end_outage(self, outage_id: int, duration_seconds: int) -> None:
        self.db.execute(
            """
            UPDATE outages SET ended_ts_utc = ?, duration_seconds = ?, recovered = 1 WHERE id = ?
            """,
            (iso(now_utc()), duration_seconds, outage_id),
        )

    def open_outage(self, experiment_id: str, component: str) -> dict[str, Any] | None:
        return row_to_dict(
            self.db.query_one(
                "SELECT * FROM outages WHERE experiment_id = ? AND component = ? AND recovered = 0 "
                "ORDER BY started_ts_utc DESC LIMIT 1",
                (experiment_id, component),
            )
        )

    def outages(self, experiment_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM outages WHERE experiment_id = ? ORDER BY started_ts_utc", (experiment_id,)
        )
        return [dict(row) for row in rows]

    def total_outage_seconds(self, experiment_id: str) -> int:
        return int(
            self.db.scalar(
                "SELECT COALESCE(SUM(duration_seconds), 0) FROM outages WHERE experiment_id = ?",
                (experiment_id,),
                default=0,
            )
        )


# ------------------------------------------------------------ okx perp entities


class LeverageDecisionRepository(BaseRepository):
    """Audit trail of every DYNAMIC_LEVERAGE_ENGINE decision."""

    def record(self, decision: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO leverage_decisions (
                experiment_id, ts_utc, setup_id, strategy_id, inst_id, direction, approved,
                leverage, confidence, volatility_pct, regime, regime_confidence, drawdown_pct,
                risk_state, stop_distance_pct, est_liq_distance_pct, liq_buffer_ratio,
                confirmed_by_exchange, reason, reasoning, adjustments
            ) VALUES (
                :experiment_id, :ts_utc, :setup_id, :strategy_id, :inst_id, :direction,
                :approved, :leverage, :confidence, :volatility_pct, :regime,
                :regime_confidence, :drawdown_pct, :risk_state, :stop_distance_pct,
                :est_liq_distance_pct, :liq_buffer_ratio, :confirmed_by_exchange,
                :reason, :reasoning, :adjustments
            )
            """,
            {
                **decision,
                "reasoning": _json(decision.get("reasoning")),
                "adjustments": _json(decision.get("adjustments")),
            },
        )

    def mark_confirmed(self, setup_id: str) -> None:
        self.db.execute(
            "UPDATE leverage_decisions SET confirmed_by_exchange = 1 WHERE setup_id = ?",
            (setup_id,),
        )

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM leverage_decisions ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]


class RejectedSignalRepository(BaseRepository):
    """Per-layer journal of every signal the decision engine refused."""

    def record(self, rejection: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO rejected_signals (
                experiment_id, ts_utc, signal_id, setup_id, strategy_id, strategy_version,
                inst_id, direction, layer_index, layer_name, reason, detail, regime, confidence
            ) VALUES (
                :experiment_id, :ts_utc, :signal_id, :setup_id, :strategy_id,
                :strategy_version, :inst_id, :direction, :layer_index, :layer_name,
                :reason, :detail, :regime, :confidence
            )
            """,
            {
                "signal_id": None,
                "setup_id": None,
                "strategy_version": None,
                "inst_id": None,
                "direction": None,
                "regime": None,
                "confidence": None,
                **rejection,
                "detail": _json(rejection.get("detail")),
            },
        )

    def recent(self, limit: int = 50, *, experiment_id: str | None = None) -> list[dict[str, Any]]:
        if experiment_id:
            rows = self.db.query(
                "SELECT * FROM rejected_signals WHERE experiment_id = ? ORDER BY id DESC LIMIT ?",
                (experiment_id, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM rejected_signals ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(row) for row in rows]

    def counts_by_layer(self, experiment_id: str) -> dict[str, int]:
        rows = self.db.query(
            "SELECT layer_name, COUNT(*) AS n FROM rejected_signals "
            "WHERE experiment_id = ? GROUP BY layer_name",
            (experiment_id,),
        )
        return {row["layer_name"]: int(row["n"]) for row in rows}


class FundingRepository(BaseRepository):
    """Realised funding payments — part of every PnL figure."""

    def record(self, event: dict[str, Any]) -> bool:
        """Insert one funding bill; returns False when already recorded."""
        try:
            self.db.execute(
                """
                INSERT INTO funding_events (
                    bill_id, experiment_id, ts_utc, inst_id, amount, currency,
                    funding_rate, position_id, strategy_id, raw
                ) VALUES (
                    :bill_id, :experiment_id, :ts_utc, :inst_id, :amount, :currency,
                    :funding_rate, :position_id, :strategy_id, :raw
                )
                """,
                {
                    "currency": None,
                    "funding_rate": None,
                    "position_id": None,
                    "strategy_id": None,
                    **event,
                    "raw": _json(event.get("raw")),
                },
            )
        except DatabaseError as exc:
            if "UNIQUE" in str(exc):
                return False
            raise
        return True

    def total_for_position(self, position_id: str) -> float:
        return float(
            self.db.scalar(
                "SELECT COALESCE(SUM(amount), 0) FROM funding_events WHERE position_id = ?",
                (position_id,),
                default=0.0,
            )
        )

    def total_for_experiment(self, experiment_id: str) -> float:
        return float(
            self.db.scalar(
                "SELECT COALESCE(SUM(amount), 0) FROM funding_events WHERE experiment_id = ?",
                (experiment_id,),
                default=0.0,
            )
        )

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM funding_events ORDER BY ts_utc DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]


class Repositories:
    """Convenience bundle passed around the engine."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.experiments = ExperimentRepository(db)
        self.strategies = StrategyRepository(db)
        self.signals = SignalRepository(db)
        self.shadow = ShadowRepository(db)
        self.demo_orders = DemoOrderRepository(db)
        self.positions = PositionRepository(db)
        self.market = MarketRepository(db)
        self.news = NewsRepository(db)
        self.performance = PerformanceRepository(db)
        self.candidates = CandidateRepository(db)
        self.allocator = AllocatorRepository(db)
        self.champion = ChampionRepository(db)
        self.system = SystemRepository(db)
        self.leverage = LeverageDecisionRepository(db)
        self.rejected = RejectedSignalRepository(db)
        self.funding = FundingRepository(db)
