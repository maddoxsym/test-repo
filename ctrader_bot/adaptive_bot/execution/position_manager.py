"""
Open-position management: break-even, partial take-profit and structural
trailing — ALL DISABLED BY DEFAULT until tested (see config.py), plus the
always-on safety exits (time stop, pre-weekend flat).

Hard rules enforced here:
  * a stop is NEVER widened and NEVER removed;
  * break-even only with evidence (>= breakeven_r AND, when configured,
    a confirmed protected swing in the profit direction);
  * trailing follows confirmed management-TF swings, not every tick;
  * stops are kept off obvious liquidity by a small ATR offset.

This module produces ManagementAction instructions; the main cBot applies
them through the cTrader API (ModifyPosition / ClosePosition).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from ..core.config import Config
from ..core.models import (CTraderSymbolSpec, Direction, ExitReason,
                           SwingKind, Trade, TradeStatus)
from ..strategy.strategy_engine import TFAnalysis


@dataclass
class ManagementAction:
    """One instruction produced by PositionManager for the main cBot."""
    kind: str                 # "MOVE_STOP" | "PARTIAL_CLOSE" | "CLOSE"
    trade: Trade
    price: float = 0.0        # new stop or close reference
    volume_units: float = 0.0 # for partial closes
    reason: ExitReason = ExitReason.MANUAL
    note: str = ""


class PositionManager:

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def manage(self, trade: Trade, mgmt: TFAnalysis,
               now: datetime, spec: CTraderSymbolSpec,
               weekend_flat: bool = False) -> List[ManagementAction]:
        cfg = self.cfg
        actions: List[ManagementAction] = []
        if trade.status != TradeStatus.OPEN:
            return actions
        d = trade.direction
        price = mgmt.last.close
        risk_dist = abs(trade.entry_price - trade.initial_stop)
        if risk_dist <= 0:
            return actions
        r_now = (price - trade.entry_price) * d.sign / risk_dist

        # ---- protective exits first ---------------------------------------
        if weekend_flat and not cfg.weekend_hold_allowed:
            actions.append(ManagementAction("CLOSE", trade, price=price,
                                            reason=ExitReason.WEEKEND_EXIT,
                                            note="pre-weekend flat"))
            return actions
        if trade.entry_time and \
                (now - trade.entry_time) >= timedelta(hours=cfg.time_exit_hours):
            actions.append(ManagementAction("CLOSE", trade, price=price,
                                            reason=ExitReason.TIME_EXIT,
                                            note=f"open > {cfg.time_exit_hours}h"))
            return actions
        if trade.bars_open >= cfg.max_bars_no_progress and trade.mfe < 0.2:
            actions.append(ManagementAction("CLOSE", trade, price=price,
                                            reason=ExitReason.TIME_EXIT,
                                            note="no progress time stop"))
            return actions

        # ---- break-even (OFF by default) ------------------------------------
        if cfg.breakeven_enabled and not trade.breakeven_done \
                and r_now >= cfg.breakeven_r:
            structure_ok = True
            if cfg.breakeven_needs_structure:
                structure_ok = self._protected_swing_in_profit(trade, mgmt)
            if structure_ok:
                costs = (spec.spread_points + cfg.slippage_buffer_points) \
                    * spec.point
                be = trade.entry_price + d.sign * costs
                if self._tightens(trade, be):
                    actions.append(ManagementAction(
                        "MOVE_STOP", trade, price=be,
                        reason=ExitReason.BREAK_EVEN,
                        note=f"BE at +{r_now:.2f}R with structure"))
                    trade.breakeven_done = True

        # ---- partial profit (OFF by default) ---------------------------------
        if cfg.partial_tp_enabled and not trade.partial_done \
                and r_now >= cfg.partial_r \
                and trade.volume_units > spec.volume_min:
            vol = spec.round_volume_down(
                trade.initial_volume_units * cfg.partial_fraction)
            if vol >= spec.volume_min and vol < trade.volume_units:
                actions.append(ManagementAction(
                    "PARTIAL_CLOSE", trade, price=price, volume_units=vol,
                    reason=ExitReason.PARTIAL_TP,
                    note=f"partial {cfg.partial_fraction:.0%} at +{r_now:.2f}R"))
                trade.partial_done = True

        # ---- structural trailing (OFF by default) -----------------------------
        if cfg.trailing_enabled:
            trail = self._structural_trail(trade, mgmt, cfg.trail_atr_mult)
            if trail is not None and self._tightens(trade, trail):
                actions.append(ManagementAction(
                    "MOVE_STOP", trade, price=trail,
                    reason=ExitReason.TRAIL_STOP,
                    note="trail behind confirmed swing"))
        return actions

    def _protected_swing_in_profit(self, trade: Trade,
                                   mgmt: TFAnalysis) -> bool:
        """A confirmed swing (mgmt TF) beyond entry in the profit direction."""
        d = trade.direction
        for s in reversed(mgmt.structure.swings):
            if trade.entry_time and s.time <= trade.entry_time:
                break
            if d == Direction.LONG and s.kind == SwingKind.LOW \
                    and s.price > trade.entry_price:
                return True
            if d == Direction.SHORT and s.kind == SwingKind.HIGH \
                    and s.price < trade.entry_price:
                return True
        return False

    def _structural_trail(self, trade: Trade, mgmt: TFAnalysis,
                          trail_atr_mult: float) -> Optional[float]:
        """Stop behind the newest confirmed swing after entry, with an ATR
        offset so the stop is not resting ON obvious liquidity. An ATR
        channel acts as a safety net in runaway moves."""
        d = trade.direction
        atr = mgmt.atr_now
        candidate: Optional[float] = None
        for s in reversed(mgmt.structure.swings):
            if trade.entry_time and s.time <= trade.entry_time:
                break
            if d == Direction.LONG and s.kind == SwingKind.LOW:
                candidate = s.price - 0.35 * atr
                break
            if d == Direction.SHORT and s.kind == SwingKind.HIGH:
                candidate = s.price + 0.35 * atr
                break
        # ATR net only once in decent profit
        risk_dist = abs(trade.entry_price - trade.initial_stop)
        price = mgmt.last.close
        if risk_dist > 0 and (price - trade.entry_price) * d.sign / risk_dist >= 2.0:
            atr_net = price - d.sign * trail_atr_mult * atr
            if candidate is None or (atr_net - candidate) * d.sign > 0:
                candidate = atr_net
        return candidate

    @staticmethod
    def _tightens(trade: Trade, new_stop: float) -> bool:
        """True only if the new stop reduces risk (never widens)."""
        if trade.direction == Direction.LONG:
            return new_stop > trade.stop_price
        return new_stop < trade.stop_price
