"""Exchange-side position protection — the stop that survives this process.

The bug this module exists to fix
---------------------------------

The bot opened a real OKX Demo position with **no stop-loss and no take-profit
registered at the exchange**. Its stop and target lived only in Python: on
:class:`~btcbot.execution.position_ledger.LedgerPosition`, watched tick by tick
by :class:`~btcbot.execution.trade_manager.TradeManager`, which issued a market
exit when the price crossed them.

That is not protection. It evaporates if the process exits, the WebSocket drops,
the event loop stalls, the machine sleeps, or the network goes away — precisely
the moments a stop matters most. The logs said ``stop_loss`` and
``take_profit`` because the *software* had those levels; the exchange had
nothing. ``OrderRequest`` had carried ``tp_trigger_price``/``sl_trigger_price``
fields and emitted ``attachAlgoOrds`` correctly for a long time — the entry path
simply never set them.

The rule this module enforces
-----------------------------

**A position is protected only when the exchange says so.** Never because a
Python object holds a ``stop_price``. Every claim of protection here comes from
reading back ``/api/v5/trade/orders-algo-pending`` and finding a live algo order
with a stop trigger on the right instrument. Internal state is not evidence.

Two placement routes, one verification
--------------------------------------

1. **Attached** — the entry carries ``attachAlgoOrds`` so OKX creates the TP/SL
   with the fill. Best: there is no window between fill and protection.
2. **Standalone OCO** — placed immediately after the fill, sized to the
   *actually filled* quantity at the *actually filled* price.

Route 1 is preferred but cannot be trusted blindly: OKX can accept an order and
reject its attached algo. So both routes end in the same verification read, and
only that read decides.

OCO, not two separate orders, is deliberate: a stop and a target as independent
conditional orders leave an orphan behind when one fires — the position closes
on the take-profit and the stop remains, ready to open a *new* position in the
opposite direction. OCO makes the exchange cancel the sibling atomically.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..exchange.models import AlgoOrder, InstrumentSpec, PositionMode, PosSide, Side
from ..strategies.base import Direction
from ..utils.errors import ApiError, TransportError
from ..utils.logging import get_logger
from ..utils.numeric import format_qty
from ..utils.timeutil import now_utc

log = get_logger(__name__)

#: Delay before each verification attempt. OKX registers an algo order a beat
#: after accepting it, so the first read can legitimately miss. Bounded and
#: short — an unverified stop is an emergency, not something to wait out.
VERIFY_BACKOFF: tuple[float, ...] = (0.0, 0.3, 0.7, 1.5)

#: Attempts at placing the standalone OCO before giving up and closing.
PLACE_ATTEMPTS = 2


class ProtectionStatus(str, Enum):
    """What the **exchange** confirms about a position's protection."""

    #: Stop-loss and take-profit both verified live at the exchange.
    PROTECTED = "protected"
    #: Stop verified, take-profit missing. Position is safe; entries blocked.
    SL_ONLY = "sl_only"
    #: No verified stop. The position must be closed immediately.
    UNPROTECTED = "unprotected"

    @property
    def has_stop(self) -> bool:
        return self is not ProtectionStatus.UNPROTECTED


@dataclass(frozen=True, slots=True)
class ProtectionState:
    """The verified protection on one position, as the exchange reports it.

    Every field here is derived from an API read. ``verified_at`` is when that
    read happened — a caller that wants to know whether protection is *current*
    should look at it rather than assuming.
    """

    status: ProtectionStatus
    inst_id: str = ""
    algo_id: str = ""
    client_algo_id: str = ""
    sl_trigger_price: float = 0.0
    tp_trigger_price: float = 0.0
    size: float = 0.0
    mode: str = ""                       # attached | oco | none
    verified_at: datetime | None = None
    detail: str = ""

    @property
    def protected(self) -> bool:
        """Whether a verified exchange-side stop exists. The only safety gate."""
        return self.status.has_stop

    @property
    def has_take_profit(self) -> bool:
        return self.status is ProtectionStatus.PROTECTED and self.tp_trigger_price > 0

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "protected": self.protected,
            "inst_id": self.inst_id,
            "sl_order_id": self.algo_id,
            "sl_price": round(self.sl_trigger_price, 2) if self.sl_trigger_price else None,
            "tp_order_id": self.algo_id if self.has_take_profit else "",
            "tp_price": round(self.tp_trigger_price, 2) if self.tp_trigger_price else None,
            "size": self.size,
            "mode": self.mode,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "detail": self.detail,
        }

    @classmethod
    def unprotected(cls, inst_id: str, detail: str) -> ProtectionState:
        return cls(
            status=ProtectionStatus.UNPROTECTED,
            inst_id=inst_id,
            verified_at=now_utc(),
            detail=detail,
        )

    @classmethod
    def from_algo(cls, order: AlgoOrder, *, mode: str) -> ProtectionState:
        status = (
            ProtectionStatus.PROTECTED
            if order.has_stop_loss and order.has_take_profit
            else ProtectionStatus.SL_ONLY
        )
        return cls(
            status=status,
            inst_id=order.inst_id,
            algo_id=order.algo_id,
            client_algo_id=order.client_algo_id,
            sl_trigger_price=order.sl_trigger_price,
            tp_trigger_price=order.tp_trigger_price,
            size=order.size,
            mode=mode,
            verified_at=now_utc(),
            detail=order.describe(),
        )


def protective_side(direction: Direction) -> Side:
    """The side that *closes* a position — protection can only ever reduce."""
    return Side.SELL if direction is Direction.LONG else Side.BUY


def stop_is_on_the_correct_side(
    direction: Direction, *, entry_price: float, stop_price: float
) -> bool:
    """A long's stop must sit below entry, a short's above.

    A stop on the wrong side would trigger instantly and turn a protective
    order into an immediate market exit at a loss.
    """
    if direction is Direction.LONG:
        return 0 < stop_price < entry_price
    return stop_price > entry_price > 0


class PositionProtector:
    """Places and verifies exchange-side protection. Verification is the point.

    The methods here never report protection from internal state. Every
    ``ProtectionState`` with ``protected=True`` is backed by an algo order read
    back from OKX within the same call.
    """

    __slots__ = ("client", "verify_backoff", "place_attempts")

    def __init__(
        self,
        client: object,
        *,
        verify_backoff: tuple[float, ...] = VERIFY_BACKOFF,
        place_attempts: int = PLACE_ATTEMPTS,
    ) -> None:
        self.client = client
        self.verify_backoff = verify_backoff
        self.place_attempts = place_attempts

    # --- verification -----------------------------------------------------

    async def verify(self, inst_id: str, *, expected_size: float = 0.0) -> ProtectionState:
        """Read the exchange and report what protection actually exists.

        Polls briefly because OKX registers an algo order slightly after
        accepting it. A read *failure* is never reported as "unprotected" — it
        raises, so the caller does not close a healthy position over a blip.
        """
        last_seen: list[AlgoOrder] = []
        for delay in self.verify_backoff:
            if delay:
                await asyncio.sleep(delay)
            orders = await self.client.get_protective_orders(inst_id)  # type: ignore[attr-defined]
            last_seen = orders
            stops = [o for o in orders if o.has_stop_loss]
            if not stops:
                continue
            # Prefer a stop that also carries a take-profit (a full OCO).
            stops.sort(key=lambda o: (o.has_take_profit, o.size), reverse=True)
            best = stops[0]
            state = ProtectionState.from_algo(best, mode=best.order_type)
            if expected_size > 0 and not _sizes_match(best.size, expected_size):
                return ProtectionState(
                    status=ProtectionStatus.SL_ONLY,
                    inst_id=inst_id,
                    algo_id=best.algo_id,
                    client_algo_id=best.client_algo_id,
                    sl_trigger_price=best.sl_trigger_price,
                    tp_trigger_price=best.tp_trigger_price,
                    size=best.size,
                    mode=best.order_type,
                    verified_at=now_utc(),
                    detail=(
                        f"protection size {best.size:g} does not match the filled "
                        f"position size {expected_size:g}"
                    ),
                )
            return state
        return ProtectionState.unprotected(
            inst_id,
            f"no live algo order with a stop trigger after {len(self.verify_backoff)} "
            f"read(s) ({len(last_seen)} pending algo order(s) seen)",
        )

    # --- placement --------------------------------------------------------

    async def protect(
        self,
        instrument: InstrumentSpec,
        *,
        direction: Direction,
        filled_size: float,
        entry_price: float,
        stop_price: float,
        target_price: float | None,
        position_mode: PositionMode = PositionMode.NET,
        client_algo_id: str | None = None,
    ) -> ProtectionState:
        """Ensure ``instrument`` has a verified exchange-side stop.

        ``entry_price`` must be the **actually filled** average price, not the
        signal's reference price — protection placed around a price the
        exchange never traded is protection at the wrong level.

        Returns the verified state. A returned ``UNPROTECTED`` means the caller
        must close the position; it never means "probably fine".
        """
        inst_id = instrument.inst_id

        # Already protected? Never place a second OCO over an existing one.
        existing = await self.verify(inst_id, expected_size=filled_size)
        if existing.protected and existing.status is ProtectionStatus.PROTECTED:
            log.info("PROTECTION", f"{inst_id} already protected — {existing.detail}")
            return existing

        if not stop_is_on_the_correct_side(
            direction, entry_price=entry_price, stop_price=stop_price
        ):
            return ProtectionState.unprotected(
                inst_id,
                f"refusing to place a stop at {stop_price:,.2f} for a "
                f"{direction.value} filled at {entry_price:,.2f} — it would trigger "
                "immediately",
            )

        sl_price = str(instrument.round_price(stop_price))
        tp_price = (
            str(instrument.round_price(target_price))
            if target_price and target_price > 0
            else None
        )
        size = format_qty(instrument.round_qty(filled_size), instrument.lot_size)
        side = protective_side(direction)
        pos_side = None if position_mode is PositionMode.NET else (
            PosSide.LONG.value if direction is Direction.LONG else PosSide.SHORT.value
        )

        last_error = ""
        for attempt in range(1, self.place_attempts + 1):
            try:
                await self.client.place_algo_order(   # type: ignore[attr-defined]
                    inst_id,
                    side=side,
                    size=size,
                    pos_side=pos_side,
                    sl_trigger_price=sl_price,
                    tp_trigger_price=tp_price,
                    reduce_only=True,
                    client_algo_id=client_algo_id,
                )
            except (ApiError, TransportError) as exc:
                last_error = str(exc)
                log.warning(
                    "PROTECTION",
                    f"{inst_id} protection placement attempt {attempt} failed: {exc}",
                )
                continue
            # Placement claiming success proves nothing. Read it back.
            state = await self.verify(inst_id, expected_size=filled_size)
            if state.protected:
                self._log_verified(state)
                return state
            last_error = state.detail

        return ProtectionState.unprotected(
            inst_id,
            f"could not place and verify protection after {self.place_attempts} "
            f"attempt(s): {last_error}",
        )

    @staticmethod
    def _log_verified(state: ProtectionState) -> None:
        """The mandated operator lines — printed only after a verified read."""
        log.info(
            "PROTECTION",
            f"SL submitted and verified — {state.inst_id} algoId={state.algo_id} "
            f"trigger {state.sl_trigger_price:,.2f} size {state.size:g}",
        )
        if state.has_take_profit:
            log.info(
                "PROTECTION",
                f"TP submitted and verified — {state.inst_id} algoId={state.algo_id} "
                f"trigger {state.tp_trigger_price:,.2f} size {state.size:g}",
            )
        else:
            log.error(
                "PROTECTION",
                f"TP NOT verified for {state.inst_id} — the stop is in place and the "
                "position is safe, but no take-profit exists at the exchange. New "
                f"entries are blocked. Detail: {state.detail}",
            )

    async def cancel(self, inst_id: str, state: ProtectionState) -> None:
        """Remove protection — only ever when the position itself is closing."""
        if not state.algo_id:
            return
        with _suppress_exchange_errors(inst_id):
            await self.client.cancel_algo_orders(   # type: ignore[attr-defined]
                inst_id, [state.algo_id], order_type=state.mode or "oco"
            )


def _sizes_match(actual: float, expected: float, *, tolerance: float = 1e-9) -> bool:
    """Whether a protective order covers the whole position.

    Under-covering is the dangerous direction: an OCO for half the position
    leaves the other half naked. Over-covering is rejected too, because a
    reduce-only order larger than the position signals stale state.
    """
    return abs(actual - expected) <= max(tolerance, expected * 1e-6)


class _suppress_exchange_errors:
    """Context manager: log and swallow exchange errors during cleanup."""

    __slots__ = ("inst_id",)

    def __init__(self, inst_id: str) -> None:
        self.inst_id = inst_id

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None and issubclass(exc_type, ApiError | TransportError):
            log.warning("PROTECTION", f"cancelling protection on {self.inst_id} failed: {exc}")
            return True
        return False


@dataclass(slots=True)
class ProtectionRegistry:
    """The verified protection state per instrument, for display and gating.

    Deliberately not a source of truth about the exchange: it caches the last
    verified read so the dashboard and the entry gate can see it without an API
    call. Anything that *acts* on protection re-verifies first.
    """

    states: dict[str, ProtectionState] = field(default_factory=dict)

    def record(self, state: ProtectionState) -> None:
        if state.inst_id:
            self.states[state.inst_id] = state

    def forget(self, inst_id: str) -> None:
        self.states.pop(inst_id, None)

    def get(self, inst_id: str) -> ProtectionState | None:
        return self.states.get(inst_id)

    def all_protected(self) -> bool:
        return all(state.protected for state in self.states.values())

    def snapshot(self) -> dict[str, object]:
        if not self.states:
            return {"tracked": 0, "all_protected": True, "positions": []}
        return {
            "tracked": len(self.states),
            "all_protected": self.all_protected(),
            "positions": [state.as_dict() for state in self.states.values()],
        }
