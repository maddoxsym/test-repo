"""Trade management for real demo positions.

Exits are managed locally rather than by resting exchange stop orders. The
reason is attribution: on spot there is no exchange-side position object, so a
resting order cannot record which strategy owns it. Managing locally keeps the
ledger authoritative — and the ledger survives restarts, so an open position is
re-attached rather than orphaned.

The trade-off is honest and worth stating: a local stop only acts while the
process is running. Data-staleness detection and SAFE_MODE exist partly to
bound that exposure, and the position is re-evaluated immediately on restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..strategies.base import Direction, ExitMechanism
from ..utils.logging import get_logger
from ..utils.numeric import safe_div
from ..utils.timeutil import now_utc
from .position_ledger import LedgerPosition, PositionLedger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExitDecision:
    should_exit: bool
    reason: str = ""
    partial_quantity: float | None = None
    stop_moved_to: float | None = None


class TradeManager:
    """Applies each position's declared exit policy to live prices."""

    def __init__(self, ledger: PositionLedger) -> None:
        self.ledger = ledger

    def evaluate(
        self,
        position: LedgerPosition,
        *,
        price: float,
        atr: float | None = None,
        opposite_signal: bool = False,
        bars_elapsed: int | None = None,
    ) -> ExitDecision:
        """Decide what, if anything, to do with ``position`` at ``price``."""
        if price <= 0:
            return ExitDecision(False)

        position.update_excursions(price)
        long = position.direction is Direction.LONG
        policy = position.exit_policy

        # --- stop (checked first, always) --------------------------------
        stop_hit = price <= position.stop_price if long else price >= position.stop_price
        if stop_hit:
            at_break_even = abs(position.stop_price - position.entry_price) < 1e-9
            moved = abs(position.stop_price - position.initial_stop) > 1e-9
            reason = (
                "break_even" if at_break_even else ("trailing_stop" if moved else "stop_loss")
            )
            return ExitDecision(True, reason)

        # --- target -------------------------------------------------------
        if position.target_price is not None:
            target_hit = price >= position.target_price if long else price <= position.target_price
            if target_hit:
                return ExitDecision(True, "take_profit")

        if policy is None:
            return ExitDecision(False)

        risk = position.risk_per_unit
        favourable = (price - position.entry_price) * position.direction.sign
        current_r = safe_div(favourable, risk)

        # --- partial exit --------------------------------------------------
        if (
            policy.supports(ExitMechanism.PARTIAL_EXIT)
            and not position.partial_taken
            and current_r >= policy.partial_at_r
        ):
            partial_qty = position.remaining_qty * policy.partial_fraction
            if partial_qty > 0:
                position.partial_taken = True
                return ExitDecision(
                    True,
                    f"partial_exit_at_{policy.partial_at_r:.1f}R",
                    partial_quantity=partial_qty,
                )

        # --- break-even ----------------------------------------------------
        if (
            policy.supports(ExitMechanism.BREAK_EVEN)
            and not position.break_even_applied
            and current_r >= policy.break_even_at_r
        ):
            position.break_even_applied = True
            self.ledger.update_stop(position, position.entry_price, note="break-even")
            log.info(
                "EXIT",
                f"{position.strategy_id} stop moved to break-even at "
                f"{position.entry_price:,.2f} ({current_r:.2f}R)",
            )
            return ExitDecision(False, "break_even_applied", stop_moved_to=position.entry_price)

        # --- trailing stop --------------------------------------------------
        if policy.supports(ExitMechanism.TRAILING_STOP):
            trail_atr = atr if atr and atr > 0 else position.atr_at_entry
            if trail_atr > 0:
                distance = trail_atr * policy.trail_atr_mult
                candidate = (price - distance) if long else (price + distance)
                improved = (
                    candidate > position.stop_price if long else candidate < position.stop_price
                )
                # Never trail past entry into a losing stop.
                valid_side = (
                    candidate < price if long else candidate > price
                )
                if improved and valid_side:
                    self.ledger.update_stop(position, candidate, note="trailing")
                    return ExitDecision(False, "trailing_stop_moved", stop_moved_to=candidate)

        # --- time stop -------------------------------------------------------
        if policy.supports(ExitMechanism.TIME_STOP) and policy.time_stop_bars > 0:
            elapsed = bars_elapsed if bars_elapsed is not None else position.bars_held
            if elapsed >= policy.time_stop_bars:
                return ExitDecision(True, "time_stop")

        # --- volatility exit --------------------------------------------------
        if (
            policy.supports(ExitMechanism.VOLATILITY_EXIT)
            and atr
            and position.atr_at_entry > 0
            and atr > position.atr_at_entry * policy.volatility_exit_mult
        ):
            return ExitDecision(True, "volatility_exit")

        # --- opposite signal --------------------------------------------------
        if policy.supports(ExitMechanism.OPPOSITE_SIGNAL) and opposite_signal:
            return ExitDecision(True, "opposite_signal")

        return ExitDecision(False)

    def finalization_exit(self, position: LedgerPosition, policy: str) -> ExitDecision:
        """Exit decision at the end of the 14-day experiment.

        ``close_at_end`` flattens immediately so the dataset is clean and frozen;
        ``manage_to_exit`` leaves the position to reach its own stop or target.
        Either way no *new* research entries are taken.
        """
        if policy == "close_at_end":
            return ExitDecision(True, "experiment_end")
        return ExitDecision(False, "managed_to_natural_exit")

    def stale_data_action(self, position: LedgerPosition) -> ExitDecision:
        """What to do with an open position while price data is stale.

        Deliberately does *not* exit: closing on stale data means trading on a
        price we do not trust. Trading pauses, the position is held, and the
        situation is logged for the operator.
        """
        log.warning(
            "SAFETY",
            f"Market data stale while {position.strategy_id} holds an open position "
            f"({position.remaining_qty:.6f} {position.symbol}) — holding, not exiting on bad data",
        )
        return ExitDecision(False, "stale_data_hold")

    def snapshot(self, price: float) -> list[dict[str, Any]]:
        rows = []
        for position in self.ledger.open_positions():
            unrealized = position.unrealized_pnl(price)
            rows.append(
                {
                    "strategy_id": position.strategy_id,
                    "direction": position.direction.value,
                    "quantity": position.remaining_qty,
                    "entry_price": position.entry_price,
                    "current_price": price,
                    "stop_price": position.stop_price,
                    "target_price": position.target_price,
                    "unrealized_pnl": round(unrealized, 2),
                    "unrealized_r": round(position.r_multiple(unrealized), 3),
                    "age_minutes": round(
                        (now_utc() - position.opened_at).total_seconds() / 60.0, 1
                    ),
                }
            )
        return rows
