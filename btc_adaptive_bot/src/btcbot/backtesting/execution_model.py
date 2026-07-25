"""Realistic execution simulation, shared by the backtester and the shadow engine.

A strategy that only works with zero fees and zero slippage must score badly, so
every simulated trade pays:

* **spread** — entry crosses the book, exit crosses it back
* **slippage** — an additional adverse offset in basis points
* **fees** — maker/taker rates on both legs
* **latency** — signals act on the *next* bar's open, never the signal bar's close
* **partial fills** — a configurable probability that only part of the size fills

Bar-level simulation cannot know the intrabar path, so where the outcome is
ambiguous this module always resolves **against** the trade: if a bar's range
contains both the stop and the target, the stop is taken. That is the only
honest choice, and it stops optimistic path assumptions from inflating results.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum

from ..exchange.models import Candle
from ..strategies.base import Direction, ExitMechanism, ExitPolicy
from ..utils.numeric import safe_div


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    BREAK_EVEN = "break_even"
    TIME_STOP = "time_stop"
    VOLATILITY_EXIT = "volatility_exit"
    OPPOSITE_SIGNAL = "opposite_signal"
    PARTIAL = "partial_exit"
    END_OF_DATA = "end_of_data"
    MANUAL = "manual"
    EXPERIMENT_END = "experiment_end"


@dataclass(frozen=True, slots=True)
class ExecutionCosts:
    """Cost assumptions. Multiplied by a stress factor for robustness testing."""

    fee_rate_taker: float = 0.00055
    fee_rate_maker: float = 0.0002
    slippage_bps: float = 2.0
    spread_bps: float = 1.0
    latency_ms: int = 250
    partial_fill_probability: float = 0.15

    def stressed(self, multiplier: float) -> ExecutionCosts:
        """Scale the *frictions* (not latency) by ``multiplier``.

        Used to check whether an edge survives worse conditions than the base
        assumptions — a strategy that dies at 2x costs is fragile.
        """
        return ExecutionCosts(
            fee_rate_taker=self.fee_rate_taker * multiplier,
            fee_rate_maker=self.fee_rate_maker * multiplier,
            slippage_bps=self.slippage_bps * multiplier,
            spread_bps=self.spread_bps * multiplier,
            latency_ms=self.latency_ms,
            partial_fill_probability=self.partial_fill_probability,
        )


@dataclass(slots=True)
class Fill:
    price: float
    quantity: float
    fee: float
    slippage_cost: float
    spread_cost: float
    is_maker: bool = False


class ExecutionModel:
    """Converts an intended price into a realistic executed price."""

    def __init__(self, costs: ExecutionCosts, *, seed: int = 20260724) -> None:
        self.costs = costs
        # Seeded so a given dataset always produces identical results; a
        # backtest whose ranking changes between runs cannot be audited.
        self._rng = random.Random(seed)

    def entry_price(self, direction: Direction, reference: float) -> float:
        """Price paid to enter, including half-spread and slippage."""
        adverse = (self.costs.spread_bps / 2.0 + self.costs.slippage_bps) / 10_000.0
        return reference * (1.0 + adverse * direction.sign)

    def exit_price(self, direction: Direction, reference: float) -> float:
        """Price received on exit — adverse in the opposite sense to entry."""
        adverse = (self.costs.spread_bps / 2.0 + self.costs.slippage_bps) / 10_000.0
        return reference * (1.0 - adverse * direction.sign)

    def fee(self, notional: float, *, is_maker: bool = False) -> float:
        rate = self.costs.fee_rate_maker if is_maker else self.costs.fee_rate_taker
        return abs(notional) * rate

    def fill_quantity(self, requested: float) -> float:
        """Apply the partial-fill model.

        A partial fill means less size than intended — never more. Filled
        fraction is drawn from [0.5, 1.0) so a partial is still a usable trade.
        """
        if self.costs.partial_fill_probability <= 0:
            return requested
        if self._rng.random() < self.costs.partial_fill_probability:
            return requested * self._rng.uniform(0.5, 1.0)
        return requested

    def simulate_entry(
        self, direction: Direction, reference: float, quantity: float, *, allow_partial: bool = True
    ) -> Fill:
        price = self.entry_price(direction, reference)
        filled = self.fill_quantity(quantity) if allow_partial else quantity
        notional = price * filled
        spread_cost = notional * (self.costs.spread_bps / 2.0) / 10_000.0
        slippage_cost = notional * self.costs.slippage_bps / 10_000.0
        return Fill(
            price=price,
            quantity=filled,
            fee=self.fee(notional),
            slippage_cost=slippage_cost,
            spread_cost=spread_cost,
        )

    def simulate_exit(self, direction: Direction, reference: float, quantity: float) -> Fill:
        price = self.exit_price(direction, reference)
        notional = price * quantity
        spread_cost = notional * (self.costs.spread_bps / 2.0) / 10_000.0
        slippage_cost = notional * self.costs.slippage_bps / 10_000.0
        return Fill(
            price=price,
            quantity=quantity,
            fee=self.fee(notional),
            slippage_cost=slippage_cost,
            spread_cost=spread_cost,
        )


@dataclass(slots=True)
class SimulatedPosition:
    """An open simulated position and its exit-management state."""

    strategy_id: str
    strategy_version: str
    direction: Direction
    symbol: str
    timeframe: str
    entry_price: float
    initial_stop: float
    stop_price: float
    target_price: float | None
    quantity: float
    remaining_quantity: float
    entry_bar_ms: int
    entry_index: int
    exit_policy: ExitPolicy
    atr_at_entry: float
    confidence: float
    entry_regime: str
    setup_key: str = ""
    signal_id: str | None = None
    news_state: str | None = None
    sizing_model: str = "risk_based"

    fees_paid: float = 0.0
    slippage_cost: float = 0.0
    spread_cost: float = 0.0
    realized_pnl: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0
    bars_held: int = 0
    break_even_applied: bool = False
    partial_taken: bool = False
    partial_pnl: float = 0.0
    metadata: dict[str, float] = field(default_factory=dict)

    @property
    def risk_per_unit(self) -> float:
        """Distance from entry to the *initial* stop — the definition of 1R.

        Always the initial stop, never the trailed one: R must stay a fixed
        yardstick or R-multiples across trades become incomparable.
        """
        return abs(self.entry_price - self.initial_stop)

    @property
    def initial_risk(self) -> float:
        return self.risk_per_unit * self.quantity

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.direction.sign * self.remaining_quantity

    def r_multiple(self, pnl: float) -> float:
        return safe_div(pnl, self.initial_risk)

    def update_excursions(self, candle: Candle) -> None:
        """Track best/worst excursion using the bar's extremes."""
        if self.direction is Direction.LONG:
            favourable = (candle.high - self.entry_price) * self.remaining_quantity
            adverse = (candle.low - self.entry_price) * self.remaining_quantity
        else:
            favourable = (self.entry_price - candle.low) * self.remaining_quantity
            adverse = (self.entry_price - candle.high) * self.remaining_quantity
        self.mfe = max(self.mfe, favourable)
        self.mae = min(self.mae, adverse)


@dataclass(slots=True)
class ExitEvent:
    reason: ExitReason
    price: float
    quantity: float
    is_partial: bool = False


class PositionManager:
    """Applies a strategy's declared exit policy to an open position.

    Written once and used by both the backtester and the live shadow engine, so
    a strategy's exits behave identically in research and in the live layers.
    """

    def __init__(self, execution: ExecutionModel) -> None:
        self.execution = execution

    def process_bar(
        self,
        position: SimulatedPosition,
        candle: Candle,
        *,
        current_atr: float | None = None,
        opposite_signal: bool = False,
    ) -> list[ExitEvent]:
        """Evaluate one bar against a position; returns any exits triggered.

        Ordering is deliberately pessimistic and leak-free:

        1. Excursions update first.
        2. The **stop as it stood at bar open** is checked before anything else.
           Trailing computed from this bar's own high cannot retroactively save a
           trade that this bar's low already stopped out.
        3. Target next — only if the stop did not trigger.
        4. Only then are trailing/break-even/partial applied, taking effect from
           the *following* bar.
        """
        events: list[ExitEvent] = []
        position.bars_held += 1
        position.update_excursions(candle)

        long = position.direction is Direction.LONG

        # 2. Stop — checked against the stop level in force at bar open.
        stop_hit = candle.low <= position.stop_price if long else candle.high >= position.stop_price
        if stop_hit:
            reason = (
                ExitReason.BREAK_EVEN
                if position.break_even_applied and _is_at_break_even(position)
                else (
                    ExitReason.TRAILING_STOP
                    if position.stop_price != position.initial_stop
                    else ExitReason.STOP_LOSS
                )
            )
            # A gap through the stop fills at the open, not at the stop price.
            fill_price = (
                min(position.stop_price, candle.open) if long
                else max(position.stop_price, candle.open)
            )
            events.append(ExitEvent(reason, fill_price, position.remaining_quantity))
            return events

        # 3. Target.
        if position.target_price is not None:
            target_hit = (
                candle.high >= position.target_price if long else candle.low <= position.target_price
            )
            if target_hit:
                fill_price = (
                    max(position.target_price, candle.open) if long
                    else min(position.target_price, candle.open)
                )
                events.append(
                    ExitEvent(ExitReason.TAKE_PROFIT, fill_price, position.remaining_quantity)
                )
                return events

        policy = position.exit_policy
        risk = position.risk_per_unit
        favourable_move = (
            (candle.close - position.entry_price) if long else (position.entry_price - candle.close)
        )
        current_r = safe_div(favourable_move, risk)

        # 4a. Partial exit.
        if (
            policy.supports(ExitMechanism.PARTIAL_EXIT)
            and not position.partial_taken
            and current_r >= policy.partial_at_r
            and position.remaining_quantity > 0
        ):
            partial_qty = position.remaining_quantity * policy.partial_fraction
            if partial_qty > 0:
                events.append(
                    ExitEvent(ExitReason.PARTIAL, candle.close, partial_qty, is_partial=True)
                )
                position.partial_taken = True

        # 4b. Break-even.
        if (
            policy.supports(ExitMechanism.BREAK_EVEN)
            and not position.break_even_applied
            and current_r >= policy.break_even_at_r
        ):
            position.stop_price = position.entry_price
            position.break_even_applied = True

        # 4c. Trailing stop — only ever moved in the favourable direction.
        if policy.supports(ExitMechanism.TRAILING_STOP):
            atr_value = current_atr if current_atr and current_atr > 0 else position.atr_at_entry
            if atr_value > 0:
                distance = atr_value * policy.trail_atr_mult
                candidate = (candle.high - distance) if long else (candle.low + distance)
                if long and candidate > position.stop_price or not long and candidate < position.stop_price:
                    position.stop_price = candidate

        # 4d. Time stop.
        if (
            policy.supports(ExitMechanism.TIME_STOP)
            and policy.time_stop_bars > 0
            and position.bars_held >= policy.time_stop_bars
        ):
            events.append(
                ExitEvent(ExitReason.TIME_STOP, candle.close, position.remaining_quantity)
            )
            return events

        # 4e. Volatility exit — conditions changed beyond the trade's premise.
        if policy.supports(ExitMechanism.VOLATILITY_EXIT) and current_atr and position.atr_at_entry > 0:
            if current_atr > position.atr_at_entry * policy.volatility_exit_mult:
                events.append(
                    ExitEvent(ExitReason.VOLATILITY_EXIT, candle.close, position.remaining_quantity)
                )
                return events

        # 4f. Opposite signal.
        if policy.supports(ExitMechanism.OPPOSITE_SIGNAL) and opposite_signal:
            events.append(
                ExitEvent(ExitReason.OPPOSITE_SIGNAL, candle.close, position.remaining_quantity)
            )

        return events

    def apply_exit(self, position: SimulatedPosition, event: ExitEvent) -> float:
        """Book an exit, update the position, and return the realised PnL."""
        fill = self.execution.simulate_exit(position.direction, event.price, event.quantity)
        gross = (fill.price - position.entry_price) * position.direction.sign * fill.quantity
        net = gross - fill.fee

        position.fees_paid += fill.fee
        position.slippage_cost += fill.slippage_cost
        position.spread_cost += fill.spread_cost
        position.realized_pnl += net
        position.remaining_quantity = max(0.0, position.remaining_quantity - fill.quantity)
        if event.is_partial:
            position.partial_pnl += net
        return net


def _is_at_break_even(position: SimulatedPosition) -> bool:
    return abs(position.stop_price - position.entry_price) < 1e-9
