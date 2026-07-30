"""
ONE trade-management state machine, used by shadow trades and real trades.

This is the structural answer to "shadow trades must simulate the exact same
management as real trades": there is only one implementation.  The shadow
engine applies the returned actions to a virtual book; the cBot translates
the same actions into ClosePosition / ModifyPosition calls.  Neither one has
its own management logic, so they cannot drift apart.

Management runs ONCE PER COMPLETED M5 BAR — never per tick.  Stop and target
*hits* are a separate concern (the broker owns them for real trades, and the
shadow engine resolves them on M1 candles); this module only decides
partials, breakeven moves, trailing moves and early exits.

The four behaviours the spec asks for:

  PARTIAL    Close a configurable fraction when a bar CLOSES beyond TP1.
             Volume is rounded DOWN to the broker step; if either the slice
             or the remainder would be below the broker minimum, the trade is
             managed as one full position instead of emitting an invalid
             order.

  BREAKEVEN  Never on a bare 1R touch.  r_now >= be_min_r is only a floor;
             a move also needs JUSTIFICATION — TP1 banked, a close beyond a
             named structure level, a new protected swing beyond entry, or
             continuation displacement.  The exact justification is recorded.

  TRAILING   Only in a genuinely strong trend (nine-factor reading, with H1 +
             M30 alignment and repeated BOS mandatory), only at/after
             trail_min_r, only when a NEW protected swing has been confirmed,
             only on a bar close, and only ever tightening.  Large runners
             switch to M15 swings.  A strong trend is allowed to run past any
             fixed R target because the target came from structure, not R.

  EARLY EXIT Scored structural evidence, so one noisy candle cannot trigger
             it.  Below the full-exit score the position is reduced and the
             stop tightened; at or above it the remainder is closed.  The
             evidence is recorded verbatim.

Every action carries a human-readable reason, and every reason reaches the
CSV trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from adaptive_bot.core.models import (CTraderSymbolSpec, Direction,
                                      StructureEventKind, TrendState, ZoneKind)
from adaptive_bot.strategy.supply_demand import SupplyDemandDetector
from .market_state_v5 import MarketState

# action kinds
PARTIAL = "PARTIAL"
MOVE_STOP = "MOVE_STOP"
CLOSE = "CLOSE"

# exit labels.  These are TRUTHFUL by construction: see shadow_v5._close and
# the label rules below.
EXIT_STOP_LOSS = "STOP_LOSS"                 # initial stop, no partial banked
EXIT_STOP_AFTER_PARTIAL = "STOP_AFTER_PARTIAL"
EXIT_BREAKEVEN_STOP = "BREAKEVEN_STOP"
EXIT_TRAIL_STOP = "TRAIL_STOP"
EXIT_TAKE_PROFIT = "TAKE_PROFIT"
EXIT_EARLY_STRUCTURE = "EARLY_EXIT_STRUCTURE"
EXIT_WEEKEND = "WEEKEND_FLAT"
EXIT_EMERGENCY = "EMERGENCY_STOP"
EXIT_RESEARCH_END = "RESEARCH_PERIOD_END"


@dataclass
class ManagementAction:
    kind: str
    reason: str
    price: float = 0.0          # MOVE_STOP: the new stop. CLOSE/PARTIAL: ref
    units: float = 0.0          # PARTIAL: units to close
    label: str = ""             # CLOSE: the exit label


@dataclass
class ManagedTrade:
    """Everything management needs, identical for shadow and real trades."""
    trade_id: str
    sid: str
    family: str
    direction: Direction
    entry: float                       # actual fill price
    initial_stop: float
    stop: float
    tp1: float
    target: float
    units_initial: float
    units: float                       # remaining
    risk_dist: float                   # |entry - initial_stop|, the R unit
    risk_money: float                  # units_initial * risk_dist * mpu
    entry_time: Optional[datetime] = None
    # management state
    partial_done: bool = False
    partial_skipped: bool = False
    partial_units: float = 0.0
    partial_price: float = 0.0
    partial_reason: str = ""
    be_done: bool = False
    be_reason: str = ""
    trail_active: bool = False
    trail_updates: List[str] = field(default_factory=list)
    last_trail_swing: str = ""         # ISO time of the swing already used
    early_exit_reason: str = ""
    early_exit_partial_done: bool = False
    bars_open: int = 0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    meta: Dict[str, float] = field(default_factory=dict)

    @property
    def d(self) -> int:
        return self.direction.sign

    def stop_stage(self) -> str:
        """What the stop currently represents — shown in the heartbeat."""
        if self.early_exit_reason:
            return "EARLY_EXIT"
        if self.trail_active:
            return "TRAILING"
        if self.be_done:
            return "BREAKEVEN"
        if self.partial_done:
            return "PARTIAL_TAKEN"
        return "INITIAL"

    def stop_is_initial(self, tick: float = 1e-9) -> bool:
        return abs(self.stop - self.initial_stop) <= max(tick, 1e-9)

    def exit_label_for_stop(self) -> str:
        """The truthful label for an exit at the CURRENT stop.

        V4 called every stop exit STOP_LOSS, which is how a trailed winner
        ended up recorded as a stop loss with a positive R.  Here the label
        follows the stop's actual meaning."""
        if not self.stop_is_initial():
            if self.trail_active:
                return EXIT_TRAIL_STOP
            if self.be_done:
                return EXIT_BREAKEVEN_STOP
            return EXIT_TRAIL_STOP
        if self.partial_done:
            return EXIT_STOP_AFTER_PARTIAL
        return EXIT_STOP_LOSS

    def tighten(self, candidate: float) -> Optional[float]:
        """Return `candidate` only when it genuinely tightens risk.

        This is the single place where the 'a stop never widens' rule is
        enforced. Every caller goes through it."""
        if candidate is None:
            return None
        if (candidate - self.stop) * self.d > 0:
            return candidate
        return None


@dataclass
class ManagementView:
    """The market as management sees it, on a completed M5 bar."""
    now: datetime
    state: MarketState
    spec: CTraderSymbolSpec
    weekend_flat: bool = False
    emergency: bool = False
    research_over: bool = False

    @property
    def atr(self) -> float:
        return self.state.atr_m5

    def exit_price(self, direction: Direction) -> float:
        """The price this trade would realise right now: bid for a long, ask
        for a short."""
        close = self.state.m5.last.close if self.state.m5 else self.state.bid
        if direction == Direction.LONG:
            return close
        return close + self.state.spread_price


@dataclass
class EarlyExitEvidence:
    score: int
    items: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"score {self.score}: " + "; ".join(self.items)


class TradeManager:

    def __init__(self, cfg):
        self.cfg = cfg

    # ------------------------------------------------------------------ core
    def r_now(self, t: ManagedTrade, view: ManagementView) -> float:
        if t.risk_dist <= 0:
            return 0.0
        return (view.exit_price(t.direction) - t.entry) * t.d / t.risk_dist

    def step(self, t: ManagedTrade, view: ManagementView,
             params: Optional[Dict[str, float]] = None
             ) -> List[ManagementAction]:
        """Decide what to do with an open trade on this completed M5 bar."""
        cfg = self.cfg
        p = params or {}
        actions: List[ManagementAction] = []
        if t.units <= 0 or t.risk_dist <= 0:
            return actions

        # ---- hard overrides ------------------------------------------------
        if view.emergency:
            return [ManagementAction(CLOSE, "emergency stop file present",
                                     view.exit_price(t.direction), t.units,
                                     EXIT_EMERGENCY)]
        if view.weekend_flat:
            return [ManagementAction(CLOSE,
                                     "pre-weekend flat (no weekend holding)",
                                     view.exit_price(t.direction), t.units,
                                     EXIT_WEEKEND)]

        r = self.r_now(t, view)
        exit_px = view.exit_price(t.direction)

        # ---- 1. partial profit at TP1 --------------------------------------
        if cfg.partial_enabled and not t.partial_done and not t.partial_skipped:
            reached = (exit_px - t.tp1) * t.d >= 0
            if reached:
                frac = p.get("partial_fraction", cfg.partial_fraction)
                frac = min(max(frac, cfg.partial_min_fraction),
                           cfg.partial_max_fraction)
                slice_units, why = self._partial_units(t, frac, view.spec)
                if slice_units > 0:
                    actions.append(ManagementAction(
                        PARTIAL,
                        f"bar closed beyond TP1 {t.tp1:.2f} at {r:+.2f}R — "
                        f"banking {frac:.0%} ({slice_units:g} units), "
                        f"remainder runs to {t.target:.2f}",
                        exit_px, slice_units))
                else:
                    # Not an order — a recorded decision. The position stays
                    # whole rather than emitting an invalid volume.
                    t.partial_skipped = True
                    t.partial_reason = why

        # ---- 2. breakeven, only when justified -----------------------------
        if cfg.be_enabled and not t.be_done:
            be_floor = p.get("be_min_r", cfg.be_min_r)
            if r >= be_floor:
                just = self._be_justification(t, view)
                if just:
                    buffer_price = (cfg.be_buffer_spread_mult
                                    * view.state.spread_price)
                    comm = 0.0
                    mpu = view.spec.money_per_price_unit_per_unit()
                    if mpu > 0 and cfg.commission_per_unit > 0:
                        comm = 2.0 * cfg.commission_per_unit / mpu
                    be = t.entry + t.d * (buffer_price + comm)
                    cand = t.tighten(be)
                    if cand is not None and (exit_px - cand) * t.d > 0:
                        actions.append(ManagementAction(
                            MOVE_STOP,
                            f"breakeven+{buffer_price + comm:.2f} at "
                            f"{r:+.2f}R — justification: {just}",
                            cand))
                    else:
                        # cannot move (would widen or would close instantly)
                        t.be_reason = t.be_reason or (
                            f"breakeven skipped: {just} but the level "
                            f"{be:.2f} would not tighten the stop")
                elif cfg.debug_logging:
                    t.meta["be_waiting"] = 1.0

        # ---- 3. trailing stop, strong trends only ---------------------------
        if cfg.trail_enabled and r >= cfg.trail_min_r:
            trail = self._trail_candidate(t, view, p, r)
            if trail is not None:
                new_stop, reason, swing_key = trail
                cand = t.tighten(new_stop)
                if cand is not None and (exit_px - cand) * t.d > 0:
                    actions.append(ManagementAction(MOVE_STOP, reason, cand))
                    t.last_trail_swing = swing_key

        # ---- 4. early exit on market-structure change ----------------------
        if cfg.early_exit_enabled and t.bars_open >= cfg.early_exit_min_bars:
            ev = self.early_exit_evidence(t, view)
            threshold = int(p.get("early_exit_min_score",
                                  cfg.early_exit_min_score))
            if ev.score >= threshold:
                htf_still_valid = view.state.bias.supports(t.direction)
                full = (ev.score >= cfg.early_exit_full_score
                        or not htf_still_valid)
                if full:
                    actions.append(ManagementAction(
                        CLOSE,
                        f"early exit ({ev.summary()}); higher-timeframe bias "
                        f"{'no longer supports the trade' if not htf_still_valid else 'is secondary to the structural damage'}",
                        exit_px, t.units, EXIT_EARLY_STRUCTURE))
                elif not t.early_exit_partial_done:
                    frac = cfg.early_exit_partial_fraction
                    slice_units, why = self._partial_units(t, frac, view.spec)
                    if slice_units > 0:
                        actions.append(ManagementAction(
                            PARTIAL,
                            f"early-exit reduction ({ev.summary()}); H1 trend "
                            f"still supports the runner", exit_px,
                            slice_units))
                        t.early_exit_partial_done = True
                    # tighten behind the current bar either way
                    defensive = self._defensive_stop(t, view)
                    cand = t.tighten(defensive)
                    if cand is not None and (exit_px - cand) * t.d > 0:
                        actions.append(ManagementAction(
                            MOVE_STOP,
                            f"early-exit tightening ({ev.summary()})"
                            + ("" if slice_units > 0 else
                               f"; partial not possible: {why}"),
                            cand))
        return actions

    # ------------------------------------------------------------- partials
    def _partial_units(self, t: ManagedTrade, frac: float,
                       spec: CTraderSymbolSpec) -> Tuple[float, str]:
        """Broker-valid partial slice, or 0 with the reason it is impossible."""
        raw = t.units * frac
        slice_units = spec.round_volume_down(raw)
        if slice_units <= 0:
            return 0.0, (f"partial of {raw:g} units rounds below the broker "
                         f"minimum {spec.volume_min:g} — managing the "
                         f"position as one full position instead")
        remainder = t.units - slice_units
        if remainder < spec.volume_min - 1e-9:
            return 0.0, (f"a {slice_units:g}-unit partial would leave "
                         f"{remainder:g} units, below the broker minimum "
                         f"{spec.volume_min:g} — managing the position as one "
                         f"full position instead")
        if slice_units >= t.units:
            return 0.0, ("partial would close the whole position — managing "
                         "it as one full position instead")
        return slice_units, ""

    # ------------------------------------------------------------ breakeven
    def _be_justification(self, t: ManagedTrade,
                          view: ManagementView) -> str:
        """Why moving to breakeven is warranted right now, or ''."""
        state = view.state
        m5 = state.m5
        if m5 is None:
            return ""
        if t.partial_done:
            return f"TP1 banked at {t.partial_price:.2f}"

        n = len(m5.candles)
        last = m5.last
        # a close beyond a named structure level, in the trade direction
        for ev in reversed(m5.structure.events):
            if ev.index < n - 1:
                break
            if ev.direction == t.direction and ev.kind in (
                    StructureEventKind.BOS, StructureEventKind.MSS,
                    StructureEventKind.CHOCH):
                return (f"M5 {ev.kind.value} closed beyond "
                        f"{ev.broken_level:.2f}")
        # a NEW protected swing formed beyond entry
        ps = m5.protected_swing(t.direction)
        if ps is not None and (ps.price - t.entry) * t.d > 0:
            if t.entry_time is None or ps.time > t.entry_time:
                return (f"new protected swing at {ps.price:.2f} formed beyond "
                        f"entry")
        # continuation displacement in the trade direction on this bar
        if m5.displacement_flags and m5.displacement_flags[-1] \
                and ((last.close > last.open) == (t.direction == Direction.LONG)):
            return (f"continuation displacement close {last.close:.2f} "
                    f"({last.body / view.atr:.2f} ATR body)"
                    if view.atr > 0 else "continuation displacement")
        return ""

    # ------------------------------------------------------------- trailing
    def _trail_candidate(self, t: ManagedTrade, view: ManagementView,
                         p: Dict[str, float], r: float
                         ) -> Optional[Tuple[float, str, str]]:
        """A new trailing stop, or None. Requires a genuinely strong trend and
        a NEWLY confirmed protected swing."""
        cfg = self.cfg
        st = view.state.strong_trend
        if not st.is_strong or st.direction != t.direction:
            return None
        atr = view.atr
        if atr <= 0:
            return None
        use_m15 = (r >= cfg.trail_m15_min_r and view.state.m15 is not None)
        feat = view.state.m15 if use_m15 else view.state.m5
        if feat is None:
            return None
        swing = feat.protected_swing(t.direction)
        if swing is None:
            return None
        swing_key = f"{feat.tf.value}@{swing.time.isoformat()}"
        if swing_key == t.last_trail_swing:
            return None                       # not a NEW structure point
        if t.entry_time is not None and swing.time < t.entry_time:
            return None                       # pre-entry structure
        buf = p.get("trail_buffer_atr", cfg.trail_swing_atr_buffer) * atr
        candidate = swing.price - t.d * buf
        reason = (f"trailing behind the new {feat.tf.value} protected swing "
                  f"{swing.price:.2f} minus {buf:.2f} ({st.score}-factor "
                  f"strong trend, {st.bos_count} BOS, efficiency "
                  f"{st.efficiency:.2f}) at {r:+.2f}R")
        return candidate, reason, swing_key

    def _defensive_stop(self, t: ManagedTrade,
                        view: ManagementView) -> Optional[float]:
        """Tighten to just beyond the current bar's adverse extreme."""
        m5 = view.state.m5
        if m5 is None or view.atr <= 0:
            return None
        last = m5.last
        buf = 0.25 * view.atr
        if t.direction == Direction.LONG:
            return last.low - buf
        return last.high + buf + view.state.spread_price

    # ------------------------------------------------------------ early exit
    def early_exit_evidence(self, t: ManagedTrade,
                            view: ManagementView) -> EarlyExitEvidence:
        """Score the structural case for abandoning the trade.

        Weights are chosen so that ONE ordinary opposite candle scores zero:
        every item below requires either a confirmed structure break, a close
        beyond a protected level, a higher-timeframe flip, or a measured
        momentum collapse."""
        state = view.state
        m5, m15 = state.m5, state.m15
        items: List[str] = []
        score = 0
        if m5 is None:
            return EarlyExitEvidence(0, ["no M5 data"])
        opp = t.direction.opposite
        n = len(m5.candles)
        last = m5.last

        # (a) confirmed opposite M5 CHoCH/MSS *with displacement*
        for ev in reversed(m5.structure.events):
            if ev.index < n - 3:
                break
            if ev.direction == opp and ev.kind in (StructureEventKind.CHOCH,
                                                   StructureEventKind.MSS):
                if ev.displacement or (
                        ev.index < len(m5.displacement_flags)
                        and m5.displacement_flags[ev.index]):
                    score += 3
                    items.append(f"opposite M5 {ev.kind.value} with "
                                 f"displacement at {ev.broken_level:.2f}")
                    break

        # (b) the protected swing was lost on a CLOSE
        ps = m5.protected_swing(t.direction)
        if ps is not None and (last.close - ps.price) * t.d < 0:
            score += 2
            items.append(f"M5 close {last.close:.2f} lost the protected swing "
                         f"{ps.price:.2f}")

        # (c) M15 structure flipped against the position
        if m15 is not None:
            want_against = TrendState.BEARISH if t.direction == Direction.LONG \
                else TrendState.BULLISH
            if m15.structure.trend == want_against:
                score += 2
                items.append(f"M15 structure flipped to "
                             f"{m15.structure.trend.value}")

        # (d) strong rejection from an opposing higher-timeframe zone
        zone_hit = None
        want = ZoneKind.SUPPLY if t.direction == Direction.LONG \
            else ZoneKind.DEMAND
        for feat in (state.h1, state.m30):
            if feat is None:
                continue
            for z in SupplyDemandDetector.active_zones(feat.zones, want, 0.30):
                touched = (last.high >= z.lower) if t.direction == Direction.LONG \
                    else (last.low <= z.upper)
                if not touched:
                    continue
                rejected = ((last.close < z.lower)
                            if t.direction == Direction.LONG
                            else (last.close > z.upper))
                strong = last.range > 0 and (
                    (last.high - last.close) / last.range > 0.5
                    if t.direction == Direction.LONG
                    else (last.close - last.low) / last.range > 0.5)
                if rejected and strong:
                    zone_hit = z
                    break
            if zone_hit is not None:
                break
        if zone_hit is not None:
            score += 2
            items.append(f"strong rejection from the "
                         f"{zone_hit.timeframe.value} {zone_hit.kind.value} "
                         f"zone {zone_hit.lower:.2f}-{zone_hit.upper:.2f}")

        # (e) momentum collapse after failing to reach the target
        r = self.r_now(t, view)
        if t.mfe_r >= 1.0 and r < t.mfe_r - 0.8 \
                and m5.eff_ratio < 0.20 \
                and ((m5.roc > 0) != (t.direction == Direction.LONG)):
            score += 1
            items.append(f"momentum collapse: gave back {t.mfe_r - r:.2f}R "
                         f"from {t.mfe_r:.2f}R with efficiency "
                         f"{m5.eff_ratio:.2f}")

        # (f) a breakout setup fell back inside its old range
        if t.family == "BREAKOUT_RETEST":
            lo = t.meta.get("range_low", 0.0)
            hi = t.meta.get("range_high", 0.0)
            if lo > 0 and hi > lo:
                inside = lo < last.close < hi
                if inside:
                    score += 3
                    items.append(f"breakout failed: close {last.close:.2f} is "
                                 f"back inside the {lo:.2f}-{hi:.2f} range")
        if not items:
            items.append("no structural damage detected")
        return EarlyExitEvidence(score, items)
