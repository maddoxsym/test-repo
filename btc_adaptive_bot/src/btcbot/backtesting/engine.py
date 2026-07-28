"""Bar-replay backtester with structural look-ahead protection.

The engine walks history one bar at a time. On each step it:

1. Advances the replay cursor by one bar.
2. Builds features from *only* the bars up to and including the cursor.
3. Classifies the regime from those same features.
4. Asks the strategy for a signal.
5. Fills any resulting entry at the **next** bar's open — never at the signal
   bar's close, because that price was not tradable when the signal formed.
6. Manages open positions bar by bar using the shared position manager.

Leakage is prevented structurally, not by discipline: the strategy is handed a
:class:`~btcbot.features.engine.MultiTimeframeFeatures` built from a truncated
candle list, so future bars are not merely off-limits — they are absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..config.schema import RegimeConfig
from ..exchange.models import Candle
from ..features.engine import FeatureEngine, MultiTimeframeFeatures
from ..regime.classifier import RegimeClassifier
from ..strategies.base import Strategy, StrategyContext
from ..utils.logging import get_logger
from ..utils.numeric import safe_div
from ..utils.timeutil import hour_bucket, interval_seconds, iso, ms_to_dt, weekday_bucket
from .execution_model import (
    ExecutionCosts,
    ExecutionModel,
    ExitReason,
    PositionManager,
    SimulatedPosition,
    funding_cost,
)

log = get_logger(__name__)


@dataclass(slots=True)
class BacktestTrade:
    """One completed simulated trade."""

    strategy_id: str
    strategy_version: str
    direction: str
    symbol: str
    timeframe: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float | None
    quantity: float
    notional: float
    pnl: float
    pnl_pct: float
    r_multiple: float
    fees: float
    slippage_cost: float
    spread_cost: float
    mfe: float
    mae: float
    bars_held: int
    duration_seconds: int
    exit_reason: str
    entry_regime: str
    confidence: float
    hour_utc: int
    weekday_utc: int
    setup_key: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "direction": self.direction,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "entry_ts_utc": self.entry_ts,
            "exit_ts_utc": self.exit_ts,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "quantity": self.quantity,
            "notional": self.notional,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "r_multiple": self.r_multiple,
            "fees": self.fees,
            "slippage_cost": self.slippage_cost,
            "spread_cost": self.spread_cost,
            "mfe": self.mfe,
            "mae": self.mae,
            "bars_held": self.bars_held,
            "duration_seconds": self.duration_seconds,
            "exit_reason": self.exit_reason,
            "entry_regime": self.entry_regime,
            "regime": self.entry_regime,
            "confidence": self.confidence,
            "hour_utc": self.hour_utc,
            "weekday_utc": self.weekday_utc,
            "setup_key": self.setup_key,
        }


@dataclass(slots=True)
class BacktestResult:
    """Trades plus the equity path they produced."""

    strategy_id: str
    strategy_version: str
    symbol: str
    timeframe: str
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    initial_equity: float = 10_000.0
    bars_processed: int = 0
    signals_generated: int = 0
    signals_rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    start_ts: str = ""
    end_ts: str = ""
    stress_multiplier: float = 1.0

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1] if self.equity_curve else self.initial_equity

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    def trade_dicts(self) -> list[dict[str, Any]]:
        return [t.as_dict() for t in self.trades]


class Backtester:
    """Replays history for one strategy on one timeframe."""

    def __init__(
        self,
        *,
        costs: ExecutionCosts,
        regime_config: RegimeConfig,
        initial_equity: float = 10_000.0,
        risk_pct: float = 0.01,
        max_bars: int | None = None,
        feature_window: int = 400,
    ) -> None:
        self.costs = costs
        self.execution = ExecutionModel(costs)
        self.manager = PositionManager(self.execution)
        self.classifier = RegimeClassifier(regime_config)
        self.feature_engine = FeatureEngine()
        self.initial_equity = initial_equity
        self.risk_pct = risk_pct
        self.max_bars = max_bars
        # How much history each feature computation sees. Bounded so a multi-year
        # backtest does not recompute 200k-bar indicator arrays on every bar.
        self.feature_window = feature_window

    def run(
        self,
        strategy: Strategy,
        candles: dict[str, list[Candle]],
        *,
        symbol: str = "BTCUSDT",
        stress_multiplier: float = 1.0,
    ) -> BacktestResult:
        """Replay ``candles`` for ``strategy``.

        ``candles`` maps timeframe → chronologically-ordered closed candles and
        must contain the strategy's primary and context timeframes.
        """
        if stress_multiplier != 1.0:
            execution = ExecutionModel(self.costs.stressed(stress_multiplier))
            manager = PositionManager(execution)
        else:
            execution, manager = self.execution, self.manager

        timeframe = strategy.primary_timeframe
        primary = candles.get(timeframe, [])
        result = BacktestResult(
            strategy_id=strategy.id,
            strategy_version=strategy.version,
            symbol=symbol,
            timeframe=timeframe,
            initial_equity=self.initial_equity,
            equity_curve=[self.initial_equity],
            stress_multiplier=stress_multiplier,
        )
        if len(primary) < strategy.min_bars + 10:
            return result

        result.start_ts = iso(ms_to_dt(primary[0].open_ms))
        result.end_ts = iso(ms_to_dt(primary[-1].open_ms))

        # Pre-index context timeframes so each bar can be aligned in O(log n).
        context_series = {
            tf: candles.get(tf, [])
            for tf in strategy.context_timeframes
            if tf in candles
        }
        context_times = {
            tf: np.fromiter((c.open_ms for c in series), dtype=np.int64, count=len(series))
            for tf, series in context_series.items()
        }

        equity = self.initial_equity
        position: SimulatedPosition | None = None
        pending_entry: dict[str, Any] | None = None

        start_index = max(strategy.min_bars, self.feature_engine.MIN_BARS)
        end_index = len(primary) - 1
        if self.max_bars is not None:
            start_index = max(start_index, end_index - self.max_bars)

        for index in range(start_index, end_index):
            bar = primary[index]
            next_bar = primary[index + 1]
            result.bars_processed += 1

            # --- manage an open position on this bar ---------------------
            if position is not None:
                # Perp funding accrues while the position is held. Charged into
                # fees so every PnL figure and score includes it.
                accrued = funding_cost(
                    quantity=position.remaining_quantity,
                    price=bar.close,
                    direction_sign=position.direction.sign,
                    funding_rate_8h=execution.costs.funding_rate_8h,
                    elapsed_seconds=interval_seconds(timeframe),
                )
                if accrued != 0.0:
                    position.fees_paid += accrued
                    equity -= accrued
                window = primary[max(0, index - self.feature_window) : index + 1]
                current_atr = _atr_from(window)
                events = manager.process_bar(position, bar, current_atr=current_atr)
                for event in events:
                    pnl = manager.apply_exit(position, event)
                    equity += pnl
                    if not event.is_partial or position.remaining_quantity <= 1e-12:
                        result.trades.append(
                            _build_trade(position, event, bar, next_bar, timeframe)
                        )
                        position = None
                        break
                result.equity_curve.append(equity)

            # --- fill a pending entry at this bar's open -----------------
            if pending_entry is not None and position is None:
                position = _open_position(
                    pending_entry, bar, execution, equity, self.risk_pct, index
                )
                pending_entry = None

            if position is not None:
                continue

            # --- generate a signal from bars up to and including `index` --
            window = primary[max(0, index - self.feature_window) : index + 1]
            primary_features = self.feature_engine.compute_from_candles(symbol, timeframe, window)
            if primary_features is None:
                continue

            by_timeframe = {timeframe: primary_features}
            usable = True
            for tf, series in context_series.items():
                # Only context bars that had already CLOSED at this instant.
                times = context_times[tf]
                cutoff = int(np.searchsorted(times, bar.open_ms, side="right"))
                if cutoff < self.feature_engine.MIN_BARS:
                    usable = False
                    break
                ctx_window = series[max(0, cutoff - self.feature_window) : cutoff]
                ctx_features = self.feature_engine.compute_from_candles(symbol, tf, ctx_window)
                if ctx_features is None:
                    usable = False
                    break
                by_timeframe[tf] = ctx_features
            if not usable:
                continue

            regime = self.classifier.classify(primary_features)
            context = StrategyContext(
                symbol=symbol,
                features=MultiTimeframeFeatures(
                    symbol=symbol,
                    by_timeframe=by_timeframe,
                    # No order book in historical replay. Microstructure
                    # strategies detect this and correctly produce nothing.
                    orderbook_valid=False,
                    spread_bps=self.costs.spread_bps,
                ),
                regime=regime,
                spread_bps=self.costs.spread_bps,
                equity=equity,
            )

            signal = strategy.generate_signal(context)
            if signal is None:
                continue
            result.signals_generated += 1

            if equity <= 0:
                result.signals_rejected += 1
                result.rejection_reasons["equity_exhausted"] = (
                    result.rejection_reasons.get("equity_exhausted", 0) + 1
                )
                continue

            pending_entry = {
                "signal": signal,
                "strategy": strategy,
                "atr": primary_features.last("atr14"),
                "regime": regime.regime.value,
            }

        # Close anything still open at the end of the data. Leaving it open would
        # quietly exclude the trade from the record and flatter the strategy.
        if position is not None and primary:
            final_bar = primary[-1]
            closeout = _SimpleExit(
                ExitReason.END_OF_DATA, final_bar.close, position.remaining_quantity
            )
            equity += manager.apply_exit(position, closeout)
            result.trades.append(
                _build_trade(position, closeout, final_bar, final_bar, timeframe)
            )
            result.equity_curve.append(equity)

        return result


class _SimpleExit:
    """Minimal exit-event stand-in for the end-of-data close-out."""

    __slots__ = ("reason", "price", "quantity", "is_partial")

    def __init__(self, reason: ExitReason, price: float, quantity: float) -> None:
        self.reason = reason
        self.price = price
        self.quantity = quantity
        self.is_partial = False


def _atr_from(candles: list[Candle], period: int = 14) -> float | None:
    """ATR over the tail of ``candles`` (used for trailing stops)."""
    if len(candles) < period + 1:
        return None
    ranges = []
    for previous, current in zip(candles[-period - 1 :], candles[-period:], strict=False):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return float(np.mean(ranges)) if ranges else None


def _open_position(
    pending: dict[str, Any],
    fill_bar: Candle,
    execution: ExecutionModel,
    equity: float,
    risk_pct: float,
    index: int,
) -> SimulatedPosition | None:
    """Open a position at ``fill_bar``'s open — one bar after the signal."""
    signal = pending["signal"]
    strategy: Strategy = pending["strategy"]

    reference = fill_bar.open
    entry = execution.entry_price(signal.direction, reference)
    stop_distance = abs(entry - signal.stop_price)
    if stop_distance <= 0:
        return None

    risk_budget = equity * risk_pct
    quantity = risk_budget / stop_distance
    notional = quantity * entry
    # Cap notional at equity: this is an unleveraged simulation, matching the
    # spot reality of the live demo layer.
    if notional > equity:
        quantity = equity / entry
    if quantity <= 0:
        return None

    fill = execution.simulate_entry(signal.direction, reference, quantity)
    if fill.quantity <= 0:
        return None

    return SimulatedPosition(
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
        entry_bar_ms=fill_bar.open_ms,
        entry_index=index,
        exit_policy=signal.exit_policy,
        atr_at_entry=float(pending.get("atr") or 0.0),
        confidence=signal.confidence,
        entry_regime=pending.get("regime", "UNCERTAIN"),
        setup_key=signal.setup_key,
        fees_paid=fill.fee,
        slippage_cost=fill.slippage_cost,
        spread_cost=fill.spread_cost,
    )


def _build_trade(
    position: SimulatedPosition,
    event: Any,
    bar: Candle,
    next_bar: Candle,
    timeframe: str,
) -> BacktestTrade:
    entry_dt = ms_to_dt(position.entry_bar_ms)
    exit_dt = ms_to_dt(bar.open_ms)
    notional = position.entry_price * position.quantity
    pnl = position.realized_pnl
    return BacktestTrade(
        strategy_id=position.strategy_id,
        strategy_version=position.strategy_version,
        direction=position.direction.value,
        symbol=position.symbol,
        timeframe=timeframe,
        entry_ts=iso(entry_dt),
        exit_ts=iso(exit_dt),
        entry_price=position.entry_price,
        exit_price=float(event.price),
        stop_price=position.initial_stop,
        target_price=position.target_price,
        quantity=position.quantity,
        notional=notional,
        pnl=pnl,
        pnl_pct=safe_div(pnl, notional),
        r_multiple=position.r_multiple(pnl),
        fees=position.fees_paid,
        slippage_cost=position.slippage_cost,
        spread_cost=position.spread_cost,
        mfe=position.mfe,
        mae=position.mae,
        bars_held=position.bars_held,
        duration_seconds=int((exit_dt - entry_dt).total_seconds()),
        exit_reason=event.reason.value if hasattr(event.reason, "value") else str(event.reason),
        entry_regime=position.entry_regime,
        confidence=position.confidence,
        hour_utc=hour_bucket(entry_dt),
        weekday_utc=weekday_bucket(entry_dt),
        setup_key=position.setup_key,
    )
