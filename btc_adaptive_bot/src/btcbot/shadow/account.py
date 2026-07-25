"""Independent shadow accounts (Layer 2).

Each strategy gets its own :class:`ShadowAccount` starting at exactly $10,000.
The isolation is structural — an account only ever mutates its own fields, and
nothing in the engine can move value between accounts — so Strategy A's
drawdown can never affect Strategy B's sizing. That is what makes the
cross-strategy comparison at Day 14 fair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..backtesting.execution_model import SimulatedPosition
from ..utils.numeric import safe_div


@dataclass(slots=True)
class ShadowAccount:
    """One strategy's isolated virtual account."""

    strategy_id: str
    experiment_id: str
    initial_equity: float = 10_000.0
    equity: float = 10_000.0
    available: float = 10_000.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fees_paid: float = 0.0
    slippage_cost: float = 0.0
    peak_equity: float = 10_000.0
    max_drawdown: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    open_position: SimulatedPosition | None = None
    open_trade_id: str | None = None
    consecutive_losses: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls, strategy_id: str, experiment_id: str, *, initial_equity: float = 10_000.0
    ) -> ShadowAccount:
        return cls(
            strategy_id=strategy_id,
            experiment_id=experiment_id,
            initial_equity=initial_equity,
            equity=initial_equity,
            available=initial_equity,
            peak_equity=initial_equity,
        )

    @property
    def has_position(self) -> bool:
        return self.open_position is not None

    @property
    def return_pct(self) -> float:
        return safe_div(self.equity - self.initial_equity, self.initial_equity)

    @property
    def drawdown_pct(self) -> float:
        return safe_div(self.peak_equity - self.equity, self.peak_equity)

    @property
    def win_rate(self) -> float:
        return safe_div(self.win_count, self.trade_count)

    def mark_to_market(self, price: float) -> None:
        """Revalue the open position at ``price``.

        Equity here is *mark-to-market* — realised plus unrealised — so the
        drawdown figure reflects pain actually experienced, not only closed losses.
        """
        if self.open_position is None:
            self.unrealized_pnl = 0.0
        else:
            self.unrealized_pnl = self.open_position.unrealized_pnl(price)
        self.equity = self.initial_equity + self.realized_pnl + self.unrealized_pnl
        self.peak_equity = max(self.peak_equity, self.equity)
        self.max_drawdown = max(self.max_drawdown, self.peak_equity - self.equity)

    def reserve(self, notional: float) -> bool:
        """Reserve buying power for a new position."""
        if notional > self.available:
            return False
        self.available -= notional
        return True

    def release(self, notional: float) -> None:
        self.available = min(self.equity, self.available + notional)

    def book_trade(self, pnl: float, fees: float, slippage: float) -> None:
        """Record a completed trade's outcome."""
        self.realized_pnl += pnl
        self.fees_paid += fees
        self.slippage_cost += slippage
        self.trade_count += 1
        if pnl > 0:
            self.win_count += 1
            self.consecutive_losses = 0
        elif pnl < 0:
            self.loss_count += 1
            self.consecutive_losses += 1
        self.equity = self.initial_equity + self.realized_pnl
        self.available = self.equity
        self.peak_equity = max(self.peak_equity, self.equity)
        self.max_drawdown = max(self.max_drawdown, self.peak_equity - self.equity)

    def risk_budget(self, risk_pct: float) -> float:
        """Currency amount to risk on the next trade.

        Uses current equity, so a drawdown automatically reduces position size —
        the same compounding-in-reverse behaviour a real account has.
        """
        return max(0.0, self.equity * risk_pct)

    def to_row(self) -> dict[str, Any]:
        position_payload = None
        if self.open_position is not None:
            position = self.open_position
            position_payload = {
                "direction": position.direction.value,
                "entry_price": position.entry_price,
                "stop_price": position.stop_price,
                "initial_stop": position.initial_stop,
                "target_price": position.target_price,
                "quantity": position.quantity,
                "remaining_quantity": position.remaining_quantity,
                "entry_bar_ms": position.entry_bar_ms,
                "bars_held": position.bars_held,
                "atr_at_entry": position.atr_at_entry,
                "confidence": position.confidence,
                "entry_regime": position.entry_regime,
                "setup_key": position.setup_key,
                "trade_id": self.open_trade_id,
                "break_even_applied": position.break_even_applied,
                "partial_taken": position.partial_taken,
                "mfe": position.mfe,
                "mae": position.mae,
                "fees_paid": position.fees_paid,
            }
        return {
            "strategy_id": self.strategy_id,
            "experiment_id": self.experiment_id,
            "initial_equity": self.initial_equity,
            "equity": self.equity,
            "available": self.available,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "fees_paid": self.fees_paid,
            "slippage_cost": self.slippage_cost,
            "peak_equity": self.peak_equity,
            "max_drawdown": self.max_drawdown,
            "open_position": position_payload,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "equity": round(self.equity, 2),
            "return_pct": round(self.return_pct * 100, 2),
            "trades": self.trade_count,
            "win_rate": round(self.win_rate * 100, 1),
            "drawdown_pct": round(self.drawdown_pct * 100, 2),
            "has_position": self.has_position,
        }
