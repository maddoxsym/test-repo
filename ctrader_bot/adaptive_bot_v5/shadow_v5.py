"""
Shadow (virtual) portfolios — one book per strategy variant, in parallel.

Every accepted setup is also taken virtually, with the SAME management state
machine the real position uses, so shadow evidence is comparable to real
evidence rather than flattering.

WHAT V4 GOT WRONG AND WHAT IS FIXED HERE
----------------------------------------
1. Exit labels lied.  A trailed or breakeven stop was still recorded as
   "STOP_LOSS", so ranking data contained stop losses with positive R.  Labels
   now come from ManagedTrade.exit_label_for_stop(), which reports what the
   stop actually was.
2. R and MFE used different denominators (planned risk money vs the actual
   fill distance), so R could exceed the recorded MFE.  There is now exactly
   ONE denominator, risk_money = units_initial * risk_dist * mpu, and MFE/MAE
   are measured against the same risk_dist.
3. Bid/ask handling was asymmetric: longs were charged a spread on the target
   and shorts were not.  Now every level is a BID level, triggers are pure bid
   comparisons for both directions, and the spread appears where it really
   does — a long buys the ask at entry, a short buys the ask back at exit.
4. Pending setups never expired, so a signal could fill days later, hundreds
   of dollars away (observed: a "TAKE_PROFIT" worth -11.6R).  Fills are now
   rejected if the signal is stale, if the next open gapped away from the
   planned entry, if the fill is already past the stop or TP1, or if the
   reward:risk no longer holds at the actual fill.
5. Partial closes used fractional units no broker would accept.  Volume is
   now rounded with the real symbol specification, exactly like a real order.

Every closed shadow trade is checked against explicit accounting invariants.
A trade that violates one is flagged suspect, written to its own file, and
EXCLUDED from the learning system — an unexplainable result must never rank a
strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from adaptive_bot.core.models import Candle, CTraderSymbolSpec, Direction
from .management import (CLOSE, EXIT_TAKE_PROFIT, MOVE_STOP, PARTIAL,
                         ManagedTrade, ManagementAction, ManagementView,
                         TradeManager)
from .setups_v5 import SetupCandidate
from .trade_plan import commission_price, planned_fill, risk_price

INVARIANT_TOL = 1e-6


@dataclass
class VirtualTrade:
    trade_id: str
    sid: str
    family: str
    version: int
    direction: Direction
    signal_time: datetime
    entry_ref: float
    planned_stop: float
    planned_tp1: float
    planned_target: float
    confluence_score: float
    confluence_detail: str
    regime: str
    session: str
    htf_bias: str
    location: str
    stop_reason: str
    target_reason: str
    tp1_reason: str
    sweep_kind: str
    spread_points_signal: float
    atr_at_signal: float
    status: str = "pending"            # pending | open | closed | discarded
    discard_reason: str = ""
    managed: Optional[ManagedTrade] = None
    entry_time: Optional[datetime] = None
    entry: float = 0.0
    spread_at_fill: float = 0.0
    risk_pct: float = 0.0
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    exit_level: float = 0.0            # the BID level that triggered the exit
    exit_label: str = ""
    gross_money: float = 0.0
    commission_money: float = 0.0
    net_money: float = 0.0
    gross_r: float = 0.0
    net_r: float = 0.0
    partial_money: float = 0.0
    bars_open: int = 0
    suspect: bool = False
    suspect_reason: str = ""
    # stop-too-tight post-mortem
    watch_bars_left: int = 0
    watch_target_hit: bool = False

    @property
    def mfe_r(self) -> float:
        return self.managed.mfe_r if self.managed else 0.0

    @property
    def mae_r(self) -> float:
        return self.managed.mae_r if self.managed else 0.0


class ShadowEngine:
    """Virtual books for every active strategy variant."""

    def __init__(self, cfg, log: Callable[[str], None],
                 on_close: Callable[[VirtualTrade], None],
                 record_row: Callable[[VirtualTrade], None],
                 record_suspect: Optional[Callable[[VirtualTrade], None]] = None,
                 record_management: Optional[
                     Callable[[VirtualTrade, ManagementAction], None]] = None,
                 on_watch_done: Optional[Callable[[VirtualTrade], None]] = None):
        self.cfg = cfg
        self.log = log
        self.on_close = on_close
        self.record_row = record_row
        self.record_suspect = record_suspect
        self.record_management = record_management
        self.on_watch_done = on_watch_done
        self.manager = TradeManager(cfg)
        self.open: Dict[str, VirtualTrade] = {}      # sid -> trade
        self.pending: List[VirtualTrade] = []
        self.watching: List[VirtualTrade] = []
        self.equity: Dict[str, float] = {}
        self.discarded_counts: Dict[str, int] = {}
        self._serial = 0

    # ------------------------------------------------------------- intake
    def submit(self, cand: SetupCandidate, spread_points: float,
               atr: float) -> bool:
        """Queue an accepted setup for a fill at the next completed M1 open."""
        sid = cand.sid
        if sid in self.open or any(t.sid == sid for t in self.pending):
            return False
        self._serial += 1
        self.pending.append(VirtualTrade(
            trade_id=f"sh{self._serial:06d}", sid=sid, family=cand.family,
            version=cand.version, direction=cand.direction,
            signal_time=cand.created, entry_ref=cand.entry_ref,
            planned_stop=cand.stop, planned_tp1=cand.tp1,
            planned_target=cand.target,
            confluence_score=cand.confluence.score,
            confluence_detail=cand.confluence.as_csv_field(),
            regime=cand.regime, session=cand.session, htf_bias=cand.htf_bias,
            location=cand.location, stop_reason=cand.stop_reason,
            target_reason=cand.target_reason, tp1_reason=cand.tp1_reason,
            sweep_kind=cand.sweep_kind, spread_points_signal=spread_points,
            atr_at_signal=atr))
        return True

    def expire_pending(self, now: datetime) -> None:
        """Drop setups that were never filled in time (market closed, ticks
        stopped, restart).  Without this a signal can fill much later at a
        completely different price — the V4 defect."""
        keep: List[VirtualTrade] = []
        for t in self.pending:
            age = (now - t.signal_time).total_seconds() / 60.0
            if age > self.cfg.max_signal_age_minutes:
                self._discard(t, f"signal expired unfilled after "
                                 f"{age:.0f} min (limit "
                                 f"{self.cfg.max_signal_age_minutes})")
            else:
                keep.append(t)
        self.pending = keep

    def abandon_open(self, now: datetime, reason: str) -> int:
        """Called on restart: open virtual trades cannot be resumed because
        their bar-by-bar continuity is gone.  They are recorded as abandoned
        rather than silently vanishing, and are NOT fed to the learning
        system."""
        count = 0
        for sid, t in list(self.open.items()):
            t.status = "discarded"
            t.discard_reason = reason
            t.exit_time = now
            t.suspect = False
            self.record_row(t)
            del self.open[sid]
            count += 1
        for t in self.pending:
            self._discard(t, reason)
        self.pending = []
        return count

    def _discard(self, t: VirtualTrade, reason: str) -> None:
        t.status = "discarded"
        t.discard_reason = reason
        self.discarded_counts[reason.split("(")[0].strip()] = \
            self.discarded_counts.get(reason.split("(")[0].strip(), 0) + 1
        self.log(f"shadow setup discarded [{t.sid}]: {reason}")

    # -------------------------------------------------------------- M1 pass
    def on_m1(self, candle: Candle, spread_points: float, point: float,
              spec: CTraderSymbolSpec, now: datetime) -> None:
        """Advance every book by one completed M1 BID candle."""
        self._fill_pending(candle, spread_points, point, spec)
        for sid in list(self.open.keys()):
            self._resolve_exits(self.open[sid], candle, spread_points, point,
                                now)
        self._advance_watch(candle, spread_points, point)

    def _fill_pending(self, candle: Candle, spread_points: float, point: float,
                      spec: CTraderSymbolSpec) -> None:
        cfg = self.cfg
        spread_price = spread_points * point
        slip_price = cfg.slippage_buffer_points * point
        mpu = spec.money_per_price_unit_per_unit()
        comm = commission_price(cfg, mpu)
        still: List[VirtualTrade] = []
        for t in self.pending:
            if candle.time <= t.signal_time:
                still.append(t)
                continue
            age_min = (candle.time - t.signal_time).total_seconds() / 60.0
            if age_min > cfg.max_signal_age_minutes:
                self._discard(t, f"stale signal: next M1 bar is {age_min:.0f} "
                                 f"min after the setup (limit "
                                 f"{cfg.max_signal_age_minutes})")
                continue
            if t.atr_at_signal > 0:
                drift = abs(candle.open - t.entry_ref)
                if drift > cfg.max_entry_slippage_atr * t.atr_at_signal:
                    self._discard(t, f"entry gapped: next open {candle.open:.2f} "
                                     f"is {drift / t.atr_at_signal:.2f} ATR from "
                                     f"the planned {t.entry_ref:.2f} (limit "
                                     f"{cfg.max_entry_slippage_atr:.2f})")
                    continue
            fill = planned_fill(t.direction, candle.open, spread_price,
                                slip_price)
            d = t.direction.sign
            # the fill must not already be through the stop or past TP1
            if (fill - t.planned_stop) * d <= 0:
                self._discard(t, f"fill {fill:.2f} is already through the stop "
                                 f"{t.planned_stop:.2f}")
                continue
            if (fill - t.planned_tp1) * d >= 0:
                self._discard(t, f"fill {fill:.2f} is already past TP1 "
                                 f"{t.planned_tp1:.2f} — the edge is gone")
                continue
            risk_dist = risk_price(t.direction, fill, t.planned_stop,
                                   spread_price)
            if risk_dist <= 0:
                self._discard(t, "non-positive risk distance at the fill")
                continue
            rr = ((t.planned_target - fill) * d - comm
                  - (spread_price if t.direction == Direction.SHORT else 0.0)
                  ) / risk_dist
            if rr < cfg.min_net_rr * 0.9:
                self._discard(t, f"reward:risk fell to {rr:.2f} at the actual "
                                 f"fill (floor {cfg.min_net_rr * 0.9:.2f})")
                continue
            # size with the REAL broker specification, rounded down
            eq = self.equity.setdefault(t.sid, cfg.virtual_equity)
            if eq <= 0:
                self._discard(t, "virtual book is out of equity")
                continue
            target_risk = eq * cfg.virtual_risk
            per_unit = risk_dist * mpu
            if per_unit <= 0:
                self._discard(t, "degenerate cost model at the fill")
                continue
            units = spec.round_volume_down(target_risk / per_unit)
            if units <= 0:
                self._discard(t, f"broker minimum volume {spec.volume_min:g} "
                                 f"would exceed the "
                                 f"{cfg.virtual_risk:.2%} virtual risk budget")
                continue
            risk_money = units * per_unit
            t.entry = fill
            t.entry_time = candle.time
            t.spread_at_fill = spread_price
            t.risk_pct = risk_money / eq if eq > 0 else 0.0
            t.status = "open"
            t.managed = ManagedTrade(
                trade_id=t.trade_id, sid=t.sid, family=t.family,
                direction=t.direction, entry=fill,
                initial_stop=t.planned_stop, stop=t.planned_stop,
                tp1=t.planned_tp1, target=t.planned_target,
                units_initial=units, units=units, risk_dist=risk_dist,
                risk_money=risk_money, entry_time=candle.time)
            self.open[t.sid] = t
        self.pending = still

    # ------------------------------------------------------------- exit pass
    def _exit_side_high(self, t: VirtualTrade, candle: Candle,
                        spread_price: float) -> float:
        """Best price this trade could realise on this bar."""
        if t.direction == Direction.LONG:
            return candle.high
        return candle.low + spread_price          # short buys back the ask

    def _exit_side_low(self, t: VirtualTrade, candle: Candle,
                       spread_price: float) -> float:
        """Worst price this trade could realise on this bar."""
        if t.direction == Direction.LONG:
            return candle.low
        return candle.high + spread_price

    def _resolve_exits(self, t: VirtualTrade, candle: Candle,
                       spread_points: float, point: float,
                       now: datetime) -> None:
        m = t.managed
        if m is None or m.risk_dist <= 0:
            return
        spread_price = spread_points * point
        d = t.direction.sign
        m.bars_open += 1
        t.bars_open = m.bars_open

        # ---- excursions, on the trade's own exit side ----------------------
        best = self._exit_side_high(t, candle, spread_price)
        worst = self._exit_side_low(t, candle, spread_price)
        m.mfe_r = max(m.mfe_r, max(0.0, (best - m.entry) * d / m.risk_dist))
        m.mae_r = max(m.mae_r, max(0.0, (m.entry - worst) * d / m.risk_dist))

        # ---- stop / target triggers: pure BID comparisons -------------------
        # (a long's stop is a bid level; a short's stop order sits at
        #  stop + spread on the ask, so ask >= stop + spread reduces to
        #  bid >= stop — the spread cancels in the trigger and reappears in
        #  the exit price.)
        if t.direction == Direction.LONG:
            stop_hit = candle.low <= m.stop
            target_hit = candle.high >= m.target
        else:
            stop_hit = candle.high >= m.stop
            target_hit = candle.low <= m.target
        if stop_hit:
            self._close(t, m.stop, m.exit_label_for_stop(), now, spread_price,
                        point)
            return
        if target_hit:
            self._close(t, m.target, EXIT_TAKE_PROFIT, now, spread_price,
                        point)

    # ------------------------------------------------------- management pass
    def on_m5_close(self, view: ManagementView,
                    params_lookup: Dict[str, Dict[str, float]]) -> None:
        """Run the shared management state machine on every open book."""
        for sid in list(self.open.keys()):
            t = self.open.get(sid)
            if t is None or t.managed is None:
                continue
            actions = self.manager.step(t.managed, view,
                                        params_lookup.get(sid, {}))
            for act in actions:
                self._apply(t, act, view)
                if t.status == "closed":
                    break

    def _apply(self, t: VirtualTrade, act: ManagementAction,
               view: ManagementView) -> None:
        m = t.managed
        if m is None:
            return
        mpu = view.spec.money_per_price_unit_per_unit()
        if act.kind == PARTIAL:
            price = act.price
            profit = (price - m.entry) * m.d * act.units * mpu
            t.partial_money += profit
            t.gross_money += profit
            t.commission_money += self.cfg.commission_per_unit * act.units
            m.units -= act.units
            m.partial_done = True
            m.partial_units += act.units
            m.partial_price = price
            m.partial_reason = act.reason
            self.log(f"shadow partial [{t.sid}] {act.units:g} units @ "
                     f"{price:.2f}: {act.reason}")
            if self.record_management:
                self.record_management(t, act)
        elif act.kind == MOVE_STOP:
            new = m.tighten(act.price)
            if new is None:
                return
            m.stop = new
            if "trailing" in act.reason:
                m.trail_active = True
                m.trail_updates.append(f"{view.now.isoformat()} -> "
                                       f"{new:.2f}: {act.reason}")
            elif "breakeven" in act.reason:
                m.be_done = True
                m.be_reason = act.reason
            if self.record_management:
                self.record_management(t, act)
        elif act.kind == CLOSE:
            m.early_exit_reason = act.reason \
                if act.label not in (EXIT_TAKE_PROFIT,) else ""
            if self.record_management:
                self.record_management(t, act)
            self._close(t, act.price, act.label, view.now,
                        view.state.spread_price, view.state.point,
                        exit_is_market=True)

    # ----------------------------------------------------------------- close
    def _close(self, t: VirtualTrade, level: float, label: str,
               now: datetime, spread_price: float, point: float,
               exit_is_market: bool = False) -> None:
        """Close the remainder and settle the accounting.

        `level` is a BID level for a long and a BID level for a short too; the
        short pays the spread on the way out, which is applied here."""
        m = t.managed
        if m is None:
            return
        if t.direction == Direction.LONG:
            exit_price = level
        else:
            # a short buys back the ask, unless the caller already supplied a
            # market (ask-adjusted) price from ManagementView.exit_price
            exit_price = level if exit_is_market else level + spread_price
        mpu = 1.0
        # mpu is embedded in risk_money; recover it so money stays consistent
        if m.units_initial > 0 and m.risk_dist > 0 and m.risk_money > 0:
            mpu = m.risk_money / (m.units_initial * m.risk_dist)
        profit = (exit_price - m.entry) * m.d * m.units * mpu
        t.gross_money += profit
        t.commission_money += self.cfg.commission_per_unit * m.units
        t.net_money = t.gross_money - t.commission_money
        t.exit_price = exit_price
        t.exit_level = level
        t.exit_time = now
        t.exit_label = label
        t.bars_open = m.bars_open
        t.status = "closed"
        if m.risk_money > 0:
            t.gross_r = t.gross_money / m.risk_money
            t.net_r = t.net_money / m.risk_money
        m.units = 0.0

        self.equity[t.sid] = self.equity.get(
            t.sid, self.cfg.virtual_equity) + t.net_money
        self.open.pop(t.sid, None)

        self._check_invariants(t, mpu)
        self.record_row(t)
        if t.suspect:
            if self.record_suspect:
                self.record_suspect(t)
            self.log(f"SHADOW INVARIANT VIOLATION [{t.sid}] {t.trade_id}: "
                     f"{t.suspect_reason} — excluded from learning")
        else:
            self.on_close(t)

        # stop-too-tight post-mortem, only for genuine losing stop exits
        if label.startswith("STOP") and t.net_r < 0 \
                and self.cfg.shadow_post_watch_bars > 0:
            t.watch_bars_left = self.cfg.shadow_post_watch_bars
            self.watching.append(t)

    def _check_invariants(self, t: VirtualTrade, mpu: float) -> None:
        """Explicit accounting checks.  A result that cannot be explained must
        never rank a strategy."""
        m = t.managed
        problems: List[str] = []
        if m is None:
            return
        tol = 0.02          # 2% of R, covers float noise and spread drift
        if m.mfe_r < -INVARIANT_TOL:
            problems.append(f"negative MFE {m.mfe_r:.4f}")
        if m.mae_r < -INVARIANT_TOL:
            problems.append(f"negative MAE {m.mae_r:.4f}")
        if t.gross_r > m.mfe_r + tol:
            problems.append(f"gross R {t.gross_r:.3f} exceeds MFE "
                            f"{m.mfe_r:.3f}")
        if t.net_r > t.gross_r + INVARIANT_TOL:
            problems.append(f"net R {t.net_r:.3f} above gross R "
                            f"{t.gross_r:.3f}")
        comm_r = (t.commission_money / m.risk_money) if m.risk_money > 0 else 0.0
        if t.net_r < -(m.mae_r + comm_r) - tol:
            problems.append(f"net R {t.net_r:.3f} worse than MAE "
                            f"{m.mae_r:.3f} plus costs {comm_r:.3f}")
        if t.exit_label == "STOP_LOSS" and t.net_r >= 0:
            problems.append(f"labelled STOP_LOSS with net R {t.net_r:+.3f}")
        if t.exit_label == EXIT_TAKE_PROFIT and t.gross_r <= 0:
            problems.append(f"labelled TAKE_PROFIT with gross R "
                            f"{t.gross_r:+.3f}")
        # the exit LEVEL is the comparable quantity: a short's realised
        # exit price is one spread beyond the bid level it traded at
        if t.exit_label == EXIT_TAKE_PROFIT \
                and abs(t.exit_level - m.target) > INVARIANT_TOL:
            problems.append(f"TAKE_PROFIT exit level {t.exit_level:.2f} is not "
                            f"the target {m.target:.2f}")
        if problems:
            t.suspect = True
            t.suspect_reason = "; ".join(problems)

    # ------------------------------------------------------------ post-mortem
    def _advance_watch(self, candle: Candle, spread_points: float,
                       point: float) -> None:
        keep: List[VirtualTrade] = []
        for t in self.watching:
            m = t.managed
            if m is None:
                continue
            if not t.watch_target_hit:
                if t.direction == Direction.LONG:
                    hit = candle.high >= m.target
                else:
                    hit = candle.low <= m.target
                if hit:
                    t.watch_target_hit = True
            t.watch_bars_left -= 1
            if t.watch_bars_left > 0 and not t.watch_target_hit:
                keep.append(t)
            elif self.on_watch_done is not None:
                self.on_watch_done(t)
        self.watching = keep

    # -------------------------------------------------------------- state io
    def snapshot(self) -> dict:
        return {"equity": dict(self.equity), "serial": self._serial,
                "discarded": dict(self.discarded_counts)}

    def restore(self, snap: dict) -> None:
        self.equity = {str(k): float(v)
                       for k, v in dict(snap.get("equity", {})).items()}
        self._serial = int(snap.get("serial", 0))
        self.discarded_counts = {str(k): int(v) for k, v
                                 in dict(snap.get("discarded", {})).items()}
        # open/pending trades are intentionally NOT restored: their bar-by-bar
        # management cannot be verified across a restart. Books and statistics
        # survive; in-flight virtual positions do not.
