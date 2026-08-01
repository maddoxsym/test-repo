"""Replay recorded signals through the eligibility filter, strict vs balanced.

Why this exists as a tool rather than a one-off script: the question "would
loosening this threshold have made money?" is answerable only against real
recorded signals, and only the machine running the experiment has them. So the
comparison is packaged here and run on that machine, against that database.

What it does
------------

For every signal in a time window it recomputes the full cost arithmetic —
stop, target, both taker legs, spread, slippage, expected net profit, net
reward:risk, target-to-cost — under **two** configurations, and reports what
each would have traded.

Where the PnL numbers come from
-------------------------------

Not from a fresh simulation. Each signal that became a shadow trade already has
a recorded outcome: the price it actually exited at, and why. This replay reuses
that exit and re-prices it at the **real** account fee rate and actual-trade
position size. So "estimated net PnL" means: this is the move the market
actually made, charged at what an actual trade would actually have cost.

That is an estimate, and it is honest about which parts are estimates:

* the exit price is **real** (recorded by the shadow engine on live data)
* the fees are **real** (the account's discovered rate)
* the spread and slippage are **modelled** (the configured assumption)
* the position size is **derived** from the risk-per-trade rule

A signal with no closed shadow trade is counted as a candidate but contributes
no PnL, and is reported separately rather than assumed to be a winner.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.schema import ActualEligibilityConfig, RiskConfig
from ..execution.eligibility import (
    FEE_SAFE_RR_100,
    FEE_SAFE_RR_120,
    ActualTradeEligibility,
    TradeCosts,
)
from ..regime.classifier import Regime
from ..strategies.base import Direction, ExitMechanism, ExitPolicy, StrategySignal
from ..utils.numeric import safe_div


@dataclass(slots=True)
class ReplayTrade:
    """One signal that a configuration would have sent as an actual order."""

    strategy_id: str
    timeframe: str
    direction: str
    entry_price: float
    stop_pct: float
    target_pct: float
    net_reward_risk: float
    target_to_cost: float
    #: None when the signal never produced a closed shadow trade.
    exit_price: float | None = None
    exit_reason: str = ""
    exit_ts: str = ""
    notional: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0

    @property
    def resolved(self) -> bool:
        return self.exit_price is not None


@dataclass(slots=True)
class ReplayResult:
    """What one configuration would have done over the window."""

    label: str
    signals_seen: int = 0
    candidates: int = 0
    shadow_only: int = 0
    unresolved: int = 0
    trades: list[ReplayTrade] = field(default_factory=list)
    shadow_only_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def sent(self) -> int:
        return len([t for t in self.trades if t.resolved])

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades if t.resolved)

    @property
    def fees(self) -> float:
        return sum(t.fees for t in self.trades if t.resolved)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades if t.resolved)

    @property
    def winners(self) -> int:
        return len([t for t in self.trades if t.resolved and t.net_pnl > 0])

    def max_drawdown(self) -> float:
        """Deepest peak-to-trough of cumulative net PnL, in currency.

        Trades are walked in exit order, because drawdown is a property of the
        sequence: the same set of trades in a different order has a different
        worst moment. Returned as a positive number (0.0 = never underwater).
        """
        resolved = sorted(
            (t for t in self.trades if t.resolved), key=lambda t: t.exit_ts or ""
        )
        equity = peak = 0.0
        worst = 0.0
        for trade in resolved:
            equity += trade.net_pnl
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
        return worst

    @property
    def profitable(self) -> bool:
        """The gate on adopting a profile: positive after every cost."""
        return self.sent > 0 and self.net_pnl > 0

    def by_timeframe(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for trade in self.trades:
            out[trade.timeframe] = out.get(trade.timeframe, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True))

    def by_strategy(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for trade in self.trades:
            out[trade.strategy_id] = out.get(trade.strategy_id, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True))

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "signals_seen": self.signals_seen,
            "actual_candidates": self.candidates,
            "shadow_only": self.shadow_only,
            "trades_sent": self.sent,
            "unresolved_candidates": self.unresolved,
            "winners": self.winners,
            "gross_pnl": round(self.gross_pnl, 2),
            "fees": round(self.fees, 2),
            "net_pnl": round(self.net_pnl, 2),
            "max_drawdown": round(self.max_drawdown(), 2),
            "profitable": self.profitable,
            "by_timeframe": self.by_timeframe(),
            "by_strategy": self.by_strategy(),
            "top_shadow_only_reasons": sorted(
                self.shadow_only_reasons.items(), key=lambda kv: kv[1], reverse=True
            )[:6],
        }


def describe_source(
    db_path: str | Path, *, hours: int, experiment_id: str | None
) -> dict[str, Any]:
    """What database this is, and how many real rows are in the window.

    Printed at the top of every report. A replay whose provenance is not shown
    is a replay you cannot check, and the whole point of this tool is that its
    conclusion can be checked.
    """
    resolved = Path(db_path).resolve()
    out: dict[str, Any] = {
        "database": str(resolved),
        "exists": resolved.exists(),
        "size_bytes": resolved.stat().st_size if resolved.exists() else 0,
        "window_hours": hours,
        "experiment_id": experiment_id or "(all)",
    }
    if not resolved.exists():
        return out
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        where = "ts_utc >= datetime('now', ?)"
        params: list[Any] = [f"-{int(hours)} hours"]
        if experiment_id:
            where += " AND experiment_id = ?"
            params.append(experiment_id)
        out["signals_in_window"] = connection.execute(
            f"SELECT COUNT(*) FROM signals WHERE {where}", params
        ).fetchone()[0]
        out["rejected_in_window"] = connection.execute(
            f"SELECT COUNT(*) FROM rejected_signals WHERE {where}", params
        ).fetchone()[0]
        trade_where = "entry_ts_utc >= datetime('now', ?)"
        trade_params: list[Any] = [f"-{int(hours)} hours"]
        if experiment_id:
            trade_where += " AND experiment_id = ?"
            trade_params.append(experiment_id)
        out["shadow_trades_in_window"] = connection.execute(
            f"SELECT COUNT(*) FROM shadow_trades WHERE {trade_where}", trade_params
        ).fetchone()[0]
        order_where = "submitted_ts_utc >= datetime('now', ?)"
        order_params: list[Any] = [f"-{int(hours)} hours"]
        if experiment_id:
            order_where += " AND experiment_id = ?"
            order_params.append(experiment_id)
        out["demo_orders_in_window"] = connection.execute(
            f"SELECT COUNT(*) FROM demo_orders WHERE {order_where}", order_params
        ).fetchone()[0]
        out["demo_orders_filled_in_window"] = connection.execute(
            f"SELECT COUNT(*) FROM demo_orders WHERE {order_where} AND status = 'filled'",
            order_params,
        ).fetchone()[0]
        out["experiments_present"] = [
            r[0] for r in connection.execute("SELECT experiment_id FROM experiments")
        ]
        out["totals"] = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("signals", "shadow_trades", "rejected_signals", "demo_orders")
        }
    finally:
        connection.close()
    return out


def _signal_from_row(row: sqlite3.Row) -> StrategySignal | None:
    """Rebuild the signal exactly as the filter would have seen it."""
    entry = float(row["entry_reference"] or 0.0)
    stop = float(row["stop_price"] or 0.0)
    if entry <= 0 or stop <= 0:
        return None
    target = row["target_price"]
    try:
        regime = Regime(row["regime"])
    except ValueError:
        regime = Regime.UNCERTAIN
    return StrategySignal(
        strategy_id=row["strategy_id"],
        strategy_version=str(row["strategy_version"] or "1.0"),
        direction=Direction.LONG if row["direction"] == "long" else Direction.SHORT,
        symbol=row["symbol"],
        timeframe=str(row["timeframe"]),
        bar_open_ms=int(row["bar_open_ms"] or 0),
        entry_reference=entry,
        stop_price=stop,
        target_price=float(target) if target is not None else None,
        confidence=float(row["confidence"] or 0.0),
        setup_key=str(row["setup_id"] or ""),
        rationale="replay",
        exit_policy=ExitPolicy(mechanisms=frozenset({ExitMechanism.FIXED_RR})),
        regime=regime,
        regime_confidence=float(row["regime_confidence"] or 0.0),
    )


def _price_the_trade(
    signal: StrategySignal,
    outcome: sqlite3.Row | None,
    *,
    costs: TradeCosts,
    equity: float,
    risk_pct: float,
) -> ReplayTrade:
    """Re-price a recorded outcome at real fees and actual-trade size."""
    stop_pct = signal.stop_distance_pct
    target_pct = safe_div(
        abs((signal.target_price or 0.0) - signal.entry_reference), signal.entry_reference
    )
    trade = ReplayTrade(
        strategy_id=signal.strategy_id,
        timeframe=signal.timeframe,
        direction=signal.direction.value,
        entry_price=signal.entry_reference,
        stop_pct=stop_pct,
        target_pct=target_pct,
        net_reward_risk=safe_div(target_pct - costs.round_trip, stop_pct + costs.round_trip),
        target_to_cost=safe_div(target_pct, costs.round_trip),
    )
    if outcome is None or outcome["exit_price"] is None:
        return trade

    exit_price = float(outcome["exit_price"])
    trade.exit_price = exit_price
    trade.exit_reason = str(outcome["exit_reason"] or "")
    trade.exit_ts = str(outcome["exit_ts_utc"] or outcome["entry_ts_utc"] or "")

    # Actual-trade sizing: risk a fixed fraction of equity across the stop.
    # This is the same rule position_sizing.py applies, reduced to its essence.
    risk_budget = equity * risk_pct
    notional = safe_div(risk_budget, stop_pct)
    trade.notional = notional

    move = safe_div(exit_price - signal.entry_reference, signal.entry_reference)
    if signal.direction is Direction.SHORT:
        move = -move
    trade.gross_pnl = move * notional
    # Both legs at the taker rate, plus the modelled spread and slippage.
    trade.fees = costs.round_trip * notional
    trade.net_pnl = trade.gross_pnl - trade.fees
    return trade


def replay(
    db_path: str | Path,
    *,
    hours: int = 24,
    taker_fee_rate: float,
    spread_bps: float = 2.0,
    equity: float = 10_000.0,
    risk: RiskConfig | None = None,
    base_config: ActualEligibilityConfig | None = None,
    experiment_id: str | None = None,
) -> dict[str, ReplayResult]:
    """Replay the window under both profiles. Read-only on the database."""
    risk = risk or RiskConfig()
    base = base_config or ActualEligibilityConfig()
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = _load_signals(connection, hours=hours, experiment_id=experiment_id)
        outcomes = _load_outcomes(connection, [r["signal_id"] for r in rows])
    finally:
        connection.close()

    # A: exactly what the live config says, unmodified. B and C hold every
    # safety gate identical and vary ONLY the net reward:risk floor, which is
    # the knob that actually controls trade frequency.
    profiles: list[tuple[str, dict[str, float] | None]] = [
        ("A_live", None),
        ("B_feesafe_rr100", FEE_SAFE_RR_100),
        ("C_feesafe_rr120", FEE_SAFE_RR_120),
    ]
    results: dict[str, ReplayResult] = {}
    for label, profile in profiles:
        config = base if profile is None else base.model_copy(update=dict(profile))
        filt = ActualTradeEligibility(config, risk)
        result = ReplayResult(label=label)
        costs = TradeCosts(
            taker_fee_rate=taker_fee_rate,
            spread_bps=spread_bps,
            slippage_bps=config.slippage_bps,
            # The replay prices against a rate supplied by the caller, which
            # came from the exchange; mark it verified so the fee gate does not
            # refuse everything and make the comparison vacuous.
            source="exchange",
        )
        for row in rows:
            signal = _signal_from_row(row)
            if signal is None:
                continue
            result.signals_seen += 1
            verdict = filt.assess(signal, costs=costs, orderbook_valid=True)
            if not verdict.eligible:
                result.shadow_only += 1
                key = verdict.reason.split("(")[0].strip()[:70]
                result.shadow_only_reasons[key] = result.shadow_only_reasons.get(key, 0) + 1
                continue
            result.candidates += 1
            trade = _price_the_trade(
                signal,
                outcomes.get(row["signal_id"]),
                costs=costs,
                equity=equity,
                risk_pct=risk.normal_risk_pct,
            )
            if not trade.resolved:
                result.unresolved += 1
            result.trades.append(trade)
        results[label] = result
    return results


def _load_signals(
    connection: sqlite3.Connection, *, hours: int, experiment_id: str | None
) -> list[sqlite3.Row]:
    query = (
        "SELECT * FROM signals WHERE ts_utc >= datetime('now', ?)"
    )
    params: list[Any] = [f"-{int(hours)} hours"]
    if experiment_id:
        query += " AND experiment_id = ?"
        params.append(experiment_id)
    query += " ORDER BY ts_utc"
    return list(connection.execute(query, params))


def _load_outcomes(
    connection: sqlite3.Connection, signal_ids: list[Any]
) -> dict[str, sqlite3.Row]:
    """Closed shadow trades keyed by the signal that produced them."""
    outcomes: dict[str, sqlite3.Row] = {}
    ids = [s for s in signal_ids if s]
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = connection.execute(
            f"SELECT * FROM shadow_trades WHERE signal_id IN ({placeholders}) "
            "AND exit_price IS NOT NULL",
            chunk,
        )
        for row in rows:
            outcomes[row["signal_id"]] = row
    return outcomes


def format_report(
    results: dict[str, ReplayResult],
    *,
    hours: int,
    costs: TradeCosts,
    source: dict[str, Any] | None = None,
) -> str:
    """The operator-facing A/B/C comparison, with its provenance on top."""
    titles = {
        "A_live": "A — CURRENT LIVE PROFILE (config as-is)",
        "B_feesafe_rr100": "B — FEE-SAFE, net reward:risk >= 1.00",
        "C_feesafe_rr120": "C — FEE-SAFE, net reward:risk >= 1.20",
    }
    lines = [f"ELIGIBILITY REPLAY — last {hours}h"]
    if source is not None:
        lines += [
            "",
            "SOURCE (prove this is the real database before reading anything below)",
            f"  database        {source.get('database')}",
            f"  exists          {source.get('exists')}  ({source.get('size_bytes', 0):,} bytes)",
            f"  experiment      {source.get('experiment_id')}",
            f"  experiments in db  {source.get('experiments_present') or 'NONE'}",
            f"  rows in window  signals={source.get('signals_in_window', 0)} "
            f"shadow_trades={source.get('shadow_trades_in_window', 0)} "
            f"rejected={source.get('rejected_in_window', 0)} "
            f"demo_orders={source.get('demo_orders_in_window', 0)} "
            f"(filled={source.get('demo_orders_filled_in_window', 0)})",
            f"  rows in db      {source.get('totals')}",
        ]
        if not source.get("signals_in_window"):
            lines += [
                "",
                "  *** NO SIGNALS IN THE WINDOW. Every number below is vacuous. ***",
                "  *** Check the database path and the experiment id.          ***",
            ]
    lines += ["", f"Cost model: {costs.describe()}", ""]

    for label in ("A_live", "B_feesafe_rr100", "C_feesafe_rr120"):
        result = results.get(label)
        if result is None:
            continue
        data = result.as_dict()
        lines += [
            f"--- {titles[label]} ---",
            f"  real signals analysed   {data['signals_seen']}",
            f"  eligible candidates     {data['actual_candidates']}",
            f"  shadow-only             {data['shadow_only']}",
            f"  orders that would send  {data['trades_sent']}"
            f" ({data['unresolved_candidates']} still open, no PnL)",
            f"  winners                 {data['winners']}",
            f"  gross PnL               ${data['gross_pnl']:,.2f}",
            f"  estimated fees          ${data['fees']:,.2f}",
            f"  NET PnL                 ${data['net_pnl']:,.2f}",
            f"  max drawdown            ${data['max_drawdown']:,.2f}",
            f"  timeframes              {data['by_timeframe'] or '-'}",
            f"  strategies              {dict(list(data['by_strategy'].items())[:6]) or '-'}",
        ]
        if data["top_shadow_only_reasons"]:
            lines.append("  why setups stayed shadow-only:")
            lines += [
                f"    {count:4d}  {reason}"
                for reason, count in data["top_shadow_only_reasons"]
            ]
        lines.append("")

    lines += ["--- VERDICT ---"]
    adoptable = [
        (label, r) for label, r in results.items() if r.profitable
    ]
    if not adoptable:
        lines.append(
            "  NO profile is net positive on this window. Adopt none of them — "
            "keep the current configuration and gather more data."
        )
    else:
        # Requirement: adopt on net PnL after costs, never on trade count.
        best_label, best = max(adoptable, key=lambda kv: kv[1].net_pnl)
        lines.append(
            f"  Most net-positive: {titles[best_label]}"
        )
        lines.append(
            f"    net ${best.net_pnl:,.2f} over {best.sent} trades, "
            f"max drawdown ${best.max_drawdown():,.2f}"
        )
        busiest = max(results.values(), key=lambda r: r.sent)
        if busiest is not best and busiest.sent > best.sent:
            lines.append(
                f"    NOTE: a different profile sent more trades ({busiest.sent} vs "
                f"{best.sent}) but less net PnL (${busiest.net_pnl:,.2f}). "
                "Trade count is not the criterion."
            )
    return "\n".join(lines)


def results_as_json(results: dict[str, ReplayResult]) -> str:
    return json.dumps({k: v.as_dict() for k, v in results.items()}, indent=2)
