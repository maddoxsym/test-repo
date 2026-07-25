"""Master position ledger for real demo trades.

Every real position answers three questions, permanently:

* **WHY** does it exist — the strategy's rationale, recorded at entry.
* **WHICH** strategy owns it — id, version, setup, signal, experiment.
* **WHEN** should it close — the planned exit, recorded before the order is sent.

On spot, "positions" are just coin balances with no exchange-side ownership
concept. Without this ledger, strategy B selling BTC that strategy A bought would
be indistinguishable from an independent short, and attribution would be
destroyed. The ledger is therefore the authority on ownership, and the executor
enforces **one attributable strategy-controlled position at a time**.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..database.repositories import PositionRepository
from ..strategies.base import Direction, ExitPolicy, StrategySignal
from ..utils.ids import new_uuid
from ..utils.logging import get_logger
from ..utils.numeric import safe_div
from ..utils.timeutil import iso, now_utc, parse_iso

log = get_logger(__name__)


@dataclass(slots=True)
class LedgerPosition:
    """A real demo position under strategy ownership."""

    position_id: str
    experiment_id: str
    strategy_id: str
    strategy_version: str
    setup_id: str
    signal_id: str | None
    symbol: str
    category: str
    direction: Direction
    reason: str
    planned_exit: str
    opened_at: datetime
    entry_price: float
    quantity: float
    remaining_qty: float
    stop_price: float
    initial_stop: float
    target_price: float | None
    confidence: float
    entry_regime: str
    news_state: str | None = None
    entry_order_id: str | None = None
    exit_policy: ExitPolicy | None = None
    fees: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0
    bars_held: int = 0
    break_even_applied: bool = False
    partial_taken: bool = False
    atr_at_entry: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_price - self.initial_stop)

    @property
    def initial_risk(self) -> float:
        return self.risk_per_unit * self.quantity

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.direction.sign * self.remaining_qty

    def r_multiple(self, pnl: float) -> float:
        return safe_div(pnl, self.initial_risk)

    def update_excursions(self, price: float) -> None:
        excursion = (price - self.entry_price) * self.direction.sign * self.remaining_qty
        self.mfe = max(self.mfe, excursion)
        self.mae = min(self.mae, excursion)

    def describe(self) -> str:
        return (
            f"{self.strategy_id} v{self.strategy_version} {self.direction.value} "
            f"{self.remaining_qty:.6f} {self.symbol} from {self.entry_price:,.2f}, "
            f"stop {self.stop_price:,.2f}, plan: {self.planned_exit}"
        )


class PositionLedger:
    """Tracks and persists real demo positions."""

    def __init__(self, repository: PositionRepository, *, experiment_id: str) -> None:
        self.repo = repository
        self.experiment_id = experiment_id
        self._open: dict[str, LedgerPosition] = {}

    # --- lifecycle --------------------------------------------------------

    def restore(self) -> list[LedgerPosition]:
        """Reload open positions after a restart.

        Without this the bot would restart flat while the exchange still held
        coins, and the next entry would double the exposure.
        """
        restored: list[LedgerPosition] = []
        for row in self.repo.open_positions():
            if row.get("experiment_id") != self.experiment_id:
                continue
            position = LedgerPosition(
                position_id=row["position_id"],
                experiment_id=row["experiment_id"],
                strategy_id=row["strategy_id"],
                strategy_version=row["strategy_version"],
                setup_id=row["setup_id"],
                signal_id=row.get("signal_id"),
                symbol=row["symbol"],
                category=row["category"],
                direction=Direction(row["direction"]),
                reason=row["reason"],
                planned_exit=row["planned_exit"],
                opened_at=parse_iso(row["opened_ts_utc"]),
                entry_price=float(row["entry_price"]),
                quantity=float(row["quantity"]),
                remaining_qty=float(row["remaining_qty"]),
                stop_price=float(row["stop_price"]),
                initial_stop=float(row["stop_price"]),
                target_price=float(row["target_price"]) if row.get("target_price") else None,
                confidence=float(row["confidence"]),
                entry_regime=row["entry_regime"],
                news_state=row.get("news_state"),
                entry_order_id=row.get("entry_order_id"),
                fees=float(row.get("fees") or 0.0),
                mfe=float(row.get("mfe") or 0.0),
                mae=float(row.get("mae") or 0.0),
            )
            self._open[position.position_id] = position
            restored.append(position)

        if restored:
            log.info("RECOVERY", f"Restored {len(restored)} open demo position(s) from the ledger")
            for position in restored:
                log.info("RECOVERY", f"  {position.describe()}")
        return restored

    def open(
        self,
        *,
        signal: StrategySignal,
        setup_id: str,
        signal_id: str | None,
        category: str,
        entry_price: float,
        quantity: float,
        entry_order_id: str,
        news_state: str | None,
        atr: float,
    ) -> LedgerPosition:
        """Record a newly opened real position."""
        position = LedgerPosition(
            position_id=f"pos_{new_uuid()}",
            experiment_id=self.experiment_id,
            strategy_id=signal.strategy_id,
            strategy_version=signal.strategy_version,
            setup_id=setup_id,
            signal_id=signal_id,
            symbol=signal.symbol,
            category=category,
            direction=signal.direction,
            reason=signal.rationale,
            planned_exit=_planned_exit_description(signal),
            opened_at=now_utc(),
            entry_price=entry_price,
            quantity=quantity,
            remaining_qty=quantity,
            stop_price=signal.stop_price,
            initial_stop=signal.stop_price,
            target_price=signal.target_price,
            confidence=signal.confidence,
            entry_regime=signal.regime.value,
            news_state=news_state,
            entry_order_id=entry_order_id,
            exit_policy=signal.exit_policy,
            atr_at_entry=atr,
        )
        self.repo.open(
            {
                "position_id": position.position_id,
                "experiment_id": position.experiment_id,
                "strategy_id": position.strategy_id,
                "strategy_version": position.strategy_version,
                "setup_id": position.setup_id,
                "signal_id": position.signal_id,
                "symbol": position.symbol,
                "category": position.category,
                "direction": position.direction.value,
                "reason": position.reason,
                "opened_ts_utc": iso(position.opened_at),
                "entry_price": position.entry_price,
                "quantity": position.quantity,
                "remaining_qty": position.remaining_qty,
                "stop_price": position.stop_price,
                "target_price": position.target_price,
                "planned_exit": position.planned_exit,
                "entry_order_id": position.entry_order_id,
                "fees": position.fees,
                "entry_regime": position.entry_regime,
                "news_state": position.news_state,
                "confidence": position.confidence,
            }
        )
        self._open[position.position_id] = position
        return position

    def close(
        self,
        position: LedgerPosition,
        *,
        exit_price: float,
        exit_reason: str,
        exit_order_id: str | None,
        fees: float,
    ) -> dict[str, Any]:
        """Close a position and return its completed trade record."""
        gross = (exit_price - position.entry_price) * position.direction.sign * position.remaining_qty
        total_fees = position.fees + fees
        pnl = gross - total_fees
        closed_at = now_utc()

        self.repo.close(
            position.position_id,
            {
                "closed_ts_utc": iso(closed_at),
                "exit_price": exit_price,
                "realized_pnl": pnl,
                "r_multiple": position.r_multiple(pnl),
                "mfe": position.mfe,
                "mae": position.mae,
                "exit_reason": exit_reason,
                "exit_order_id": exit_order_id,
                "fees": total_fees,
            },
        )
        self._open.pop(position.position_id, None)

        from ..utils.timeutil import hour_bucket, weekday_bucket

        return {
            "position_id": position.position_id,
            "strategy_id": position.strategy_id,
            "strategy_version": position.strategy_version,
            "symbol": position.symbol,
            "timeframe": "demo",
            "direction": position.direction.value,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "quantity": position.quantity,
            "notional": position.entry_price * position.quantity,
            "pnl": pnl,
            "pnl_pct": safe_div(pnl, position.entry_price * position.quantity),
            "r_multiple": position.r_multiple(pnl),
            "mfe": position.mfe,
            "mae": position.mae,
            "fees": total_fees,
            "slippage_cost": 0.0,
            "spread_cost": 0.0,
            "duration_seconds": int((closed_at - position.opened_at).total_seconds()),
            "exit_reason": exit_reason,
            "regime": position.entry_regime,
            "entry_regime": position.entry_regime,
            "news_state": position.news_state,
            "confidence": position.confidence,
            "hour_utc": hour_bucket(position.opened_at),
            "weekday_utc": weekday_bucket(position.opened_at),
        }

    def reduce(self, position: LedgerPosition, quantity: float) -> None:
        position.remaining_qty = max(0.0, position.remaining_qty - quantity)
        self.repo.reduce(position.position_id, remaining_qty=position.remaining_qty)

    def update_stop(self, position: LedgerPosition, stop_price: float, *, note: str = "") -> None:
        position.stop_price = stop_price
        planned = f"{position.planned_exit}; stop moved to {stop_price:,.2f}" if note else None
        self.repo.update_stop(position.position_id, stop_price, planned_exit=planned)

    def sync_excursions(self, position: LedgerPosition) -> None:
        self.repo.update_excursions(position.position_id, mfe=position.mfe, mae=position.mae)

    # --- queries ----------------------------------------------------------

    @property
    def has_open_position(self) -> bool:
        return bool(self._open)

    def open_positions(self) -> list[LedgerPosition]:
        return list(self._open.values())

    def current(self) -> LedgerPosition | None:
        """The single active position, if any."""
        return next(iter(self._open.values()), None)

    def owner_of(self, symbol: str) -> str | None:
        for position in self._open.values():
            if position.symbol == symbol:
                return position.strategy_id
        return None

    def total_exposure(self, price: float) -> float:
        return sum(p.remaining_qty * price for p in self._open.values())

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "position_id": p.position_id,
                "strategy_id": p.strategy_id,
                "direction": p.direction.value,
                "quantity": p.remaining_qty,
                "entry_price": p.entry_price,
                "stop_price": p.stop_price,
                "target_price": p.target_price,
                "reason": p.reason[:160],
                "planned_exit": p.planned_exit,
                "opened_at": iso(p.opened_at),
            }
            for p in self._open.values()
        ]


def _planned_exit_description(signal: StrategySignal) -> str:
    """Human-readable exit plan, recorded before the order is sent."""
    parts = [f"stop {signal.stop_price:,.2f}"]
    if signal.target_price is not None:
        parts.append(f"target {signal.target_price:,.2f}")
    mechanisms = sorted(m.value for m in signal.exit_policy.mechanisms)
    parts.append(f"mechanisms: {', '.join(mechanisms)}")
    if signal.exit_policy.time_stop_bars:
        parts.append(f"time stop {signal.exit_policy.time_stop_bars} bars")
    return "; ".join(parts)
