"""Shadow trading engine (Layer 2).

Every enabled strategy trades continuously in its own isolated $10,000 account,
driven by live OKX market data. Because the accounts are independent, dozens
of strategies accumulate evidence simultaneously even though only one at a time
can control the real demo account.

Perp economics are modelled: funding accrues on held positions (using the
exchange's discovered rate once the stream delivers one) and flows into fees,
so shadow PnL is comparable with real demo PnL.

The engine reuses :class:`~btcbot.backtesting.execution_model.PositionManager`,
so a strategy's exits behave identically in shadow trading and in backtests —
which is what makes the Layer-1 and Layer-2 numbers comparable.
"""

from __future__ import annotations

from typing import Any

from ..backtesting.execution_model import (
    ExecutionCosts,
    ExecutionModel,
    ExitEvent,
    ExitReason,
    PositionManager,
    SimulatedPosition,
    funding_cost,
)
from ..config.schema import ShadowConfig
from ..database.repositories import ShadowRepository
from ..exchange.models import Candle
from ..strategies.base import Direction, Strategy, StrategySignal
from ..utils.ids import new_uuid
from ..utils.logging import get_logger
from ..utils.numeric import safe_div
from ..utils.timeutil import hour_bucket, interval_seconds, iso, ms_to_dt, now_utc, weekday_bucket
from .account import ShadowAccount

log = get_logger(__name__)


class ShadowEngine:
    """Runs isolated per-strategy accounts against live market data."""

    def __init__(
        self,
        config: ShadowConfig,
        repository: ShadowRepository,
        *,
        experiment_id: str,
        symbol: str,
    ) -> None:
        self.config = config
        self.repo = repository
        self.experiment_id = experiment_id
        self.symbol = symbol
        self.accounts: dict[str, ShadowAccount] = {}
        costs = ExecutionCosts(
            fee_rate_taker=config.fee_rate_taker,
            fee_rate_maker=config.fee_rate_maker,
            slippage_bps=config.slippage_bps,
            spread_bps=0.0,  # live spread is measured, not assumed
            funding_rate_8h=config.funding_rate_8h,
        )
        self.execution = ExecutionModel(costs)
        self.manager = PositionManager(self.execution)
        self._trade_ids: dict[str, str] = {}
        # Live funding: starts from the configured model rate; overwritten with
        # the exchange's *discovered* rate the moment the stream delivers one.
        self._funding_rate_8h = config.funding_rate_8h
        self.funding_charged_total = 0.0

    def set_funding_rate(self, rate_8h: float) -> None:
        """Adopt the exchange's current funding rate for shadow accrual."""
        self._funding_rate_8h = rate_8h

    # --- lifecycle --------------------------------------------------------

    def initialise(self, strategy_ids: list[str]) -> None:
        """Create or restore an account per strategy.

        Existing accounts are restored from the database so a restart does not
        reset anyone's equity — the 14-day record must be continuous.
        """
        stored = self.repo.load_accounts(self.experiment_id)
        restored = resumed_positions = 0

        for strategy_id in strategy_ids:
            row = stored.get(strategy_id)
            if row is None:
                account = ShadowAccount.create(
                    strategy_id, self.experiment_id, initial_equity=self.config.initial_equity
                )
                self.repo.upsert_account(account.to_row())
            else:
                account = ShadowAccount(
                    strategy_id=strategy_id,
                    experiment_id=self.experiment_id,
                    initial_equity=float(row["initial_equity"]),
                    equity=float(row["equity"]),
                    available=float(row["available"]),
                    realized_pnl=float(row["realized_pnl"]),
                    unrealized_pnl=float(row["unrealized_pnl"]),
                    fees_paid=float(row["fees_paid"]),
                    slippage_cost=float(row["slippage_cost"]),
                    peak_equity=float(row["peak_equity"]),
                    max_drawdown=float(row["max_drawdown"]),
                )
                restored += 1
                if row.get("open_position"):
                    resumed_positions += 1
                    account.metadata["pending_restore"] = row["open_position"]
            self.accounts[strategy_id] = account

        log.info(
            "SHADOW",
            f"Shadow engine ready: {len(self.accounts)} accounts "
            f"({restored} restored, {resumed_positions} with open positions)",
        )

    def persist(self) -> None:
        for account in self.accounts.values():
            self.repo.upsert_account(account.to_row())

    # --- trading ----------------------------------------------------------

    def on_signal(
        self,
        strategy: Strategy,
        signal: StrategySignal,
        *,
        atr: float,
        news_state: str | None = None,
        volatility_state: str | None = None,
    ) -> bool:
        """Open a shadow position from a signal. Returns True when opened."""
        account = self.accounts.get(strategy.id)
        if account is None:
            return False
        if account.has_position:
            return False  # one position per strategy at a time
        if account.equity <= 0:
            return False

        risk_budget = account.risk_budget(self.config.risk_pct)
        stop_distance = abs(signal.entry_reference - signal.stop_price)
        if stop_distance <= 0 or risk_budget <= 0:
            return False

        quantity = risk_budget / stop_distance
        notional = quantity * signal.entry_reference
        # Shadow accounts cap notional at 1x equity. Leverage changes margin,
        # not stop-out risk, so unleveraged shadow PnL remains directly
        # comparable with the leveraged demo account's risk-based PnL.
        if notional > account.equity:
            quantity = account.equity / signal.entry_reference
            notional = account.equity
        if quantity <= 0:
            return False

        fill = self.execution.simulate_entry(
            signal.direction, signal.entry_reference, quantity, allow_partial=False
        )
        if fill.quantity <= 0:
            return False

        position = SimulatedPosition(
            strategy_id=strategy.id,
            strategy_version=strategy.version,
            direction=signal.direction,
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            entry_price=fill.price,
            initial_stop=signal.stop_price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            quantity=fill.quantity,
            remaining_quantity=fill.quantity,
            entry_bar_ms=signal.bar_open_ms,
            entry_index=0,
            exit_policy=signal.exit_policy,
            atr_at_entry=atr,
            confidence=signal.confidence,
            entry_regime=signal.regime.value,
            setup_key=signal.setup_key,
            news_state=news_state,
            fees_paid=fill.fee,
            slippage_cost=fill.slippage_cost,
            spread_cost=fill.spread_cost,
        )

        account.open_position = position
        account.reserve(min(notional, account.available))

        trade_id = f"sh_{new_uuid()}"
        account.open_trade_id = trade_id
        self._trade_ids[strategy.id] = trade_id

        entry_dt = ms_to_dt(signal.bar_open_ms)
        self.repo.open_trade(
            {
                "trade_id": trade_id,
                "experiment_id": self.experiment_id,
                "signal_id": None,
                "setup_id": signal.setup_key,
                "strategy_id": strategy.id,
                "strategy_version": strategy.version,
                "symbol": signal.symbol,
                "timeframe": signal.timeframe,
                "direction": signal.direction.value,
                "sizing_model": "risk_based",
                "entry_ts_utc": iso(now_utc()),
                "entry_price": fill.price,
                "stop_price": signal.stop_price,
                "target_price": signal.target_price,
                "quantity": fill.quantity,
                "notional": fill.price * fill.quantity,
                "fees": fill.fee,
                "slippage_cost": fill.slippage_cost,
                "spread_cost": fill.spread_cost,
                "entry_regime": signal.regime.value,
                "news_state": news_state,
                "confidence": signal.confidence,
                "hour_utc": hour_bucket(entry_dt),
                "weekday_utc": weekday_bucket(entry_dt),
                "volatility_state": volatility_state,
            }
        )
        log.debug(
            "SHADOW",
            f"{strategy.id} entered {signal.direction.value} "
            f"{fill.quantity:.6f} @ {fill.price:,.2f}",
        )
        return True

    def on_bar(
        self,
        candle: Candle,
        *,
        atr_by_timeframe: dict[str, float] | None = None,
        opposite_signals: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Advance every position whose timeframe matches ``candle``.

        Returns the trades that closed on this bar, so the caller can update the
        allocator and learning layers with fresh observations.
        """
        closed: list[dict[str, Any]] = []
        atr_map = atr_by_timeframe or {}
        opposites = opposite_signals or set()

        for strategy_id, account in self.accounts.items():
            position = account.open_position
            if position is None or position.timeframe != candle.timeframe:
                continue

            # Perp funding accrues while the position is held. Charged into the
            # position's fees so every shadow PnL figure and score includes it.
            accrued = funding_cost(
                quantity=position.remaining_quantity,
                price=candle.close,
                direction_sign=position.direction.sign,
                funding_rate_8h=self._funding_rate_8h,
                elapsed_seconds=interval_seconds(candle.timeframe),
            )
            if accrued != 0.0:
                position.fees_paid += accrued
                self.funding_charged_total += accrued

            events = self.manager.process_bar(
                position,
                candle,
                current_atr=atr_map.get(candle.timeframe),
                opposite_signal=strategy_id in opposites,
            )
            for event in events:
                record = self._apply_exit(account, position, event, candle)
                if record is not None:
                    closed.append(record)
                    break

        return closed

    def mark_to_market(self, price: float) -> None:
        for account in self.accounts.values():
            account.mark_to_market(price)

    def force_close_all(self, price: float, reason: ExitReason = ExitReason.EXPERIMENT_END) -> list[dict[str, Any]]:
        """Close every open shadow position — used at experiment finalisation."""
        closed: list[dict[str, Any]] = []
        for account in self.accounts.values():
            position = account.open_position
            if position is None:
                continue
            event = ExitEvent(reason, price, position.remaining_quantity)
            record = self._apply_exit(account, position, event, None)
            if record is not None:
                closed.append(record)
        return closed

    def _apply_exit(
        self,
        account: ShadowAccount,
        position: SimulatedPosition,
        event: ExitEvent,
        candle: Candle | None,
    ) -> dict[str, Any] | None:
        """Book an exit and persist the trade when it fully closes."""
        pnl = self.manager.apply_exit(position, event)

        if event.is_partial and position.remaining_quantity > 1e-12:
            # A partial keeps the position open; equity updates but no trade closes.
            account.realized_pnl += pnl
            account.mark_to_market(event.price)
            return None

        account.book_trade(pnl, position.fees_paid, position.slippage_cost + position.spread_cost)
        # `exit_reason` names the leg that *closed* the trade; `pnl` and
        # `r_multiple` are the *whole trade*, summed over every leg net of
        # costs. Those are two different scopes, which is how a trade can be
        # labelled `take_profit` and still be recorded at negative R: an
        # earlier partial exited at a loss, or costs outweighed a thin final
        # leg. The decomposition below makes that arithmetic explicit rather
        # than leaving the reader to infer it.
        total_pnl = position.realized_pnl
        final_leg_pnl = pnl
        partials_pnl = position.partial_pnl
        costs = position.fees_paid + position.slippage_cost + position.spread_cost
        notional = position.entry_price * position.quantity
        entry_dt = ms_to_dt(position.entry_bar_ms)
        exit_dt = ms_to_dt(candle.open_ms) if candle is not None else now_utc()

        trade_id = account.open_trade_id or self._trade_ids.get(account.strategy_id)
        record: dict[str, Any] = {
            "trade_id": trade_id,
            "strategy_id": account.strategy_id,
            "strategy_version": position.strategy_version,
            "timeframe": position.timeframe,
            "direction": position.direction.value,
            "entry_price": position.entry_price,
            "exit_price": event.price,
            "quantity": position.quantity,
            "notional": notional,
            "pnl": total_pnl,
            "pnl_pct": safe_div(total_pnl, notional),
            "r_multiple": position.r_multiple(total_pnl),
            "mfe": position.mfe,
            "mae": position.mae,
            "duration_seconds": int((exit_dt - entry_dt).total_seconds()),
            "exit_reason": event.reason.value,
            # Why the whole-trade R can disagree with the exit label.
            "final_leg_pnl": final_leg_pnl,
            "partials_pnl": partials_pnl,
            "costs": costs,
            "regime": position.entry_regime,
            "entry_regime": position.entry_regime,
            "news_state": position.news_state,
            "confidence": position.confidence,
            "fees": position.fees_paid,
            "slippage_cost": position.slippage_cost,
            "spread_cost": position.spread_cost,
            "hour_utc": hour_bucket(entry_dt),
            "weekday_utc": weekday_bucket(entry_dt),
        }

        if trade_id:
            self.repo.close_trade(
                trade_id,
                {
                    "exit_ts_utc": iso(exit_dt),
                    "exit_price": event.price,
                    "fees": position.fees_paid,
                    "slippage_cost": position.slippage_cost,
                    "spread_cost": position.spread_cost,
                    "pnl": total_pnl,
                    "pnl_pct": record["pnl_pct"],
                    "r_multiple": record["r_multiple"],
                    "mfe": position.mfe,
                    "mae": position.mae,
                    "duration_seconds": record["duration_seconds"],
                    "exit_reason": event.reason.value,
                    "exit_regime": position.entry_regime,
                },
            )

        account.open_position = None
        account.open_trade_id = None
        self._trade_ids.pop(account.strategy_id, None)
        self.repo.upsert_account(account.to_row())

        log.debug(
            "SHADOW",
            f"{account.strategy_id} exited {event.reason.value} "
            f"{record['r_multiple']:+.2f}R (equity ${account.equity:,.2f})"
            f"{_exit_discrepancy(position, record)}",
        )
        return record

    # --- reporting --------------------------------------------------------

    def leaderboard(self, limit: int = 10) -> list[dict[str, Any]]:
        ranked = sorted(
            self.accounts.values(), key=lambda a: a.equity, reverse=True
        )
        return [a.summary() for a in ranked[:limit]]

    def open_position_count(self) -> int:
        return sum(1 for a in self.accounts.values() if a.has_position)

    def total_trades(self) -> int:
        return sum(a.trade_count for a in self.accounts.values())

    def snapshot(self) -> dict[str, Any]:
        equities = [a.equity for a in self.accounts.values()]
        return {
            "accounts": len(self.accounts),
            "open_positions": self.open_position_count(),
            "total_trades": self.total_trades(),
            "best_equity": round(max(equities), 2) if equities else 0.0,
            "worst_equity": round(min(equities), 2) if equities else 0.0,
            "median_equity": round(sorted(equities)[len(equities) // 2], 2) if equities else 0.0,
            "winners": sum(1 for a in self.accounts.values() if a.equity > a.initial_equity),
            "losers": sum(1 for a in self.accounts.values() if a.equity < a.initial_equity),
        }

    def account_for(self, strategy_id: str) -> ShadowAccount | None:
        return self.accounts.get(strategy_id)

    def directions_open(self) -> dict[str, Direction]:
        return {
            sid: acc.open_position.direction
            for sid, acc in self.accounts.items()
            if acc.open_position is not None
        }


#: Exit reasons whose *name* promises a gain. When one of these closes a trade
#: at negative R the line must show its working, because the label alone reads
#: as a contradiction.
PROFIT_LABELLED_EXITS = frozenset({ExitReason.TAKE_PROFIT.value, ExitReason.PARTIAL.value})


def _exit_discrepancy(position: SimulatedPosition, record: dict[str, Any]) -> str:
    """Explain a whole-trade R that the exit label does not account for.

    ``exit_reason`` describes the leg that closed the trade; ``r_multiple``
    describes the trade end to end. When a ``take_profit`` trade lands at
    negative R the arithmetic is one of two things — an earlier partial exited
    at a loss, or costs outweighed a thin final leg — and it should be stated,
    not left for the reader to reconstruct.

    Returns an empty string when label and number agree, so ordinary exits
    stay terse.
    """
    final_leg = record["final_leg_pnl"]
    total = record["pnl"]
    legs_disagree = final_leg != 0 and (final_leg > 0) != (total > 0)
    label_oversells = record["exit_reason"] in PROFIT_LABELLED_EXITS and total < 0
    if not (legs_disagree or label_oversells):
        return ""

    risk = position.initial_risk
    parts = [f"final leg {safe_div(final_leg, risk):+.2f}R"]
    if abs(record["partials_pnl"]) > 1e-12:
        parts.append(f"earlier partials {safe_div(record['partials_pnl'], risk):+.2f}R")
    parts.append(f"costs ${record['costs']:,.2f}")
    return f" — whole trade differs from the exit leg: {', '.join(parts)}"
