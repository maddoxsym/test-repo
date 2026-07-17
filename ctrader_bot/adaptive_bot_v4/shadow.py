"""
Shadow (virtual) portfolios — one per strategy, running in parallel.

Every strategy receives the same completed market data and records
hypothetical trades against its own virtual account (same starting equity
and the same fixed virtual risk fraction for every strategy, so books are
directly comparable; ranking itself is done in R units).

Realism rules:
  * a signal formed at a decision-bar close is FILLED at the OPEN of the
    next completed M1 candle, plus spread (buys) and a slippage allowance
    — never on the signal bar itself (no look-ahead);
  * exits are resolved on completed M1 candles; when a candle spans both
    stop and target, the STOP is assumed to hit first (conservative);
  * spread is charged on the fill side, commission per unit both ways;
  * MFE/MAE are tracked from M1 extremes in R units;
  * after a stop-out the engine watches the next N bars to record whether
    the original target would still have been reached ("stop too tight"
    evidence for the learning system);
  * one open virtual trade per strategy at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from adaptive_bot.core.models import Candle, Direction
from .strategy_space import Signal, StrategyConfig


@dataclass
class VirtualTrade:
    trade_id: str
    strategy_id: str
    tf: str
    direction: Direction
    signal_time: datetime
    entry_time: Optional[datetime]
    entry: float
    stop: float
    initial_stop: float
    target: float
    units: float
    risk_money: float
    risk_pct: float
    spread_points: float
    regime: str
    session: str
    reason: str
    mgmt_mode: str
    status: str = "pending"           # pending | open | closed
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    profit: float = 0.0
    r_multiple: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    be_done: bool = False
    partial_done: bool = False
    partial_profit: float = 0.0
    bars_open: int = 0
    # post-stop watch (stop-too-tight evidence)
    watch_bars_left: int = 0
    watch_target_hit: bool = False

    def risk_dist(self) -> float:
        return abs(self.entry - self.initial_stop)


class ShadowEngine:
    """Runs every strategy's virtual book against completed M1 candles."""

    def __init__(self, cfg, log: Callable[[str], None],
                 on_close: Callable[[VirtualTrade], None],
                 record_row: Callable[[VirtualTrade], None],
                 on_watch_done: Optional[Callable[[VirtualTrade], None]] = None):
        self.cfg = cfg
        self.log = log
        self.on_close = on_close          # -> learning system
        self.record_row = record_row      # -> CSV persistence
        self.on_watch_done = on_watch_done  # post-stop watch resolution
        self.open: Dict[str, VirtualTrade] = {}       # strategy_id -> trade
        self.pending: List[VirtualTrade] = []
        self.watching: List[VirtualTrade] = []
        self.equity: Dict[str, float] = {}            # strategy_id -> equity
        self._serial = 0

    # ------------------------------------------------------------ intake
    def submit(self, scfg: StrategyConfig, sig: Signal, spread_points: float,
               point: float, regime: str, session: str) -> bool:
        """Queue a signal for fill at the next completed M1 open.
        One virtual position per strategy; duplicates are refused."""
        sid = scfg.sid
        if sid in self.open or any(t.strategy_id == sid for t in self.pending):
            return False
        eq = self.equity.setdefault(sid, self.cfg.virtual_equity)
        risk_money = eq * self.cfg.virtual_risk
        risk_dist = abs(sig.entry_ref - sig.stop)
        if risk_dist <= 0 or eq <= 0:
            return False
        cost_per_unit = (spread_points + self.cfg.slippage_buffer_points) \
            * point + 2 * self.cfg.commission_per_unit
        units = risk_money / (risk_dist + cost_per_unit)
        if units <= 0:
            return False
        self._serial += 1
        self.pending.append(VirtualTrade(
            trade_id=f"sh{self._serial:06d}", strategy_id=sid, tf=sig.tf,
            direction=sig.direction, signal_time=sig.created,
            entry_time=None, entry=sig.entry_ref, stop=sig.stop,
            initial_stop=sig.stop, target=sig.target, units=units,
            risk_money=risk_money, risk_pct=self.cfg.virtual_risk,
            spread_points=spread_points, regime=regime, session=session,
            reason=sig.reason, mgmt_mode=scfg.mgmt_mode))
        return True

    # ------------------------------------------------------------ engine
    def on_m1(self, candle: Candle, spread_points: float, point: float,
              now: datetime, mgmt_lookup: Dict[str, dict],
              tf_atr: Dict[str, float]) -> None:
        """Advance every book by one completed M1 candle.
        mgmt_lookup: strategy_id -> mgmt param dict (be_r/partial_r/...).
        tf_atr: strategy tf -> current ATR (for trailing)."""
        self._fill_pending(candle, spread_points, point, now)
        for sid in list(self.open.keys()):
            self._manage(self.open[sid], candle, spread_points, point, now,
                         mgmt_lookup.get(sid, {}), tf_atr)
        self._advance_watch(candle)

    def _fill_pending(self, candle: Candle, spread_points: float,
                      point: float, now: datetime) -> None:
        still: List[VirtualTrade] = []
        for t in self.pending:
            if candle.time <= t.signal_time:
                still.append(t)           # candle not after the signal yet
                continue
            slip = self.cfg.slippage_buffer_points * point
            if t.direction == Direction.LONG:
                fill = candle.open + spread_points * point + slip
            else:
                fill = candle.open - slip
            # keep the planned risk honest: recompute from actual fill
            t.entry = fill
            t.entry_time = candle.time
            t.status = "open"
            if abs(fill - t.initial_stop) <= 0:
                t.status = "closed"
                t.exit_reason = "DEGENERATE_FILL"
                continue
            self.open[t.strategy_id] = t
        self.pending = still

    def _manage(self, t: VirtualTrade, c: Candle, spread_points: float,
                point: float, now: datetime, mgmt: dict,
                tf_atr: Dict[str, float]) -> None:
        d = 1 if t.direction == Direction.LONG else -1
        risk = t.risk_dist()
        if risk <= 0:
            return
        t.bars_open += 1
        spread_px = spread_points * point

        # excursions in R (bid-based candles; conservative on the far side)
        if d > 0:
            t.mfe_r = max(t.mfe_r, (c.high - t.entry) / risk)
            t.mae_r = max(t.mae_r, (t.entry - c.low) / risk)
        else:
            t.mfe_r = max(t.mfe_r, (t.entry - c.low) / risk)
            t.mae_r = max(t.mae_r, (c.high + spread_px - t.entry) / risk)

        # exit checks: STOP FIRST (conservative)
        stop_hit = (c.low <= t.stop) if d > 0 else (c.high + spread_px >= t.stop)
        tgt_hit = (c.high >= t.target + spread_px) if d > 0 \
            else (c.low <= t.target)
        if stop_hit:
            self._close(t, t.stop, "STOP_LOSS", now)
            return
        if tgt_hit:
            self._close(t, t.target, "TAKE_PROFIT", now)
            return

        # management by the strategy's configured mode
        r_now = (c.close - t.entry) * d / risk
        mode = t.mgmt_mode
        be_r = mgmt.get("be_r", 1.2)
        if mode in ("BE_1R", "PARTIAL_RUNNER", "ATR_TRAIL", "STRUCT_TRAIL") \
                and not t.be_done and r_now >= be_r:
            be = t.entry + d * (spread_px + self.cfg.slippage_buffer_points
                                * point)
            if (be - t.stop) * d > 0:
                t.stop = be
            t.be_done = True
        if mode == "PARTIAL_RUNNER" and not t.partial_done \
                and r_now >= mgmt.get("partial_r", 1.8):
            frac = mgmt.get("partial_frac", 0.5)
            part_units = t.units * frac
            px = c.close - d * spread_px if d < 0 else c.close
            t.partial_profit = (px - t.entry) * d * part_units \
                - self.cfg.commission_per_unit * part_units
            t.units -= part_units
            t.partial_done = True
        if mode in ("ATR_TRAIL", "STRUCT_TRAIL") and r_now >= 1.0:
            atr = tf_atr.get(t.tf, 0.0)
            if atr > 0:
                trail = c.close - d * mgmt.get("trail_atr", 2.0) * atr
                if (trail - t.stop) * d > 0:      # only ever tightens
                    t.stop = trail

    def _close(self, t: VirtualTrade, price: float, reason: str,
               now: datetime) -> None:
        d = 1 if t.direction == Direction.LONG else -1
        t.exit_price = price
        t.exit_time = now
        t.exit_reason = reason
        gross = (price - t.entry) * d * t.units
        costs = self.cfg.commission_per_unit * t.units
        t.profit = gross - costs + t.partial_profit
        t.r_multiple = t.profit / t.risk_money if t.risk_money > 0 else 0.0
        t.status = "closed"
        self.equity[t.strategy_id] = self.equity.get(
            t.strategy_id, self.cfg.virtual_equity) + t.profit
        del self.open[t.strategy_id]
        if reason == "STOP_LOSS" and self.cfg.shadow_post_watch_bars > 0:
            t.watch_bars_left = self.cfg.shadow_post_watch_bars
            self.watching.append(t)
        self.record_row(t)
        self.on_close(t)

    def _advance_watch(self, c: Candle) -> None:
        keep: List[VirtualTrade] = []
        for t in self.watching:
            if not t.watch_target_hit:
                d = 1 if t.direction == Direction.LONG else -1
                hit = (c.high >= t.target) if d > 0 else (c.low <= t.target)
                if hit:
                    t.watch_target_hit = True
            t.watch_bars_left -= 1
            if t.watch_bars_left > 0 and not t.watch_target_hit:
                keep.append(t)
            elif self.on_watch_done is not None:
                self.on_watch_done(t)      # resolved: tight-stop evidence
        self.watching = keep

    # ------------------------------------------------------------ state io
    def snapshot(self) -> dict:
        return {"equity": dict(self.equity), "serial": self._serial}

    def restore(self, snap: dict) -> None:
        self.equity = dict(snap.get("equity", {}))
        self._serial = int(snap.get("serial", 0))
        # open/pending virtual trades are intentionally NOT restored across
        # restarts: without tick continuity their management would be
        # unverifiable. They are logged as ABANDONED_RESTART by the caller.
