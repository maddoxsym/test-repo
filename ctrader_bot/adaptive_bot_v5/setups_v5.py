"""
Six genuinely different confluence setup families.

This is the module that replaces V4's standalone signals.  Each family has a
MANDATORY SEQUENCE of multi-timeframe conditions.  The sequence is not
scored and cannot be traded around: if any required step is missing the
setup does not exist, and the failure is logged with the exact reason.  Only
after the whole sequence passes is the remaining evidence scored by the
correlation-aware confluence engine, and the score must still clear the
family's threshold.

  LIQUIDITY_REVERSAL  HTF context/location -> important liquidity swept ->
                      M15/M5 CHoCH or MSS against the short-term move ->
                      displacement -> retest of the displacement's FVG /
                      order block / zone -> confirmation close
  TREND_CONTINUATION  clear H1+M30 trend -> M15 agrees -> pullback into a
                      logical area (zone / OB / FVG / discount-premium, or
                      the EMA band ONLY when structure supports it) -> M5
                      BOS or continuation displacement -> controlled retest
                      -> enough room to opposing liquidity
  SESSION_LIQUIDITY   a PREVIOUS session's or previous day's pool swept
                      inside an active session -> displacement -> CHoCH/MSS
                      -> FVG / order-block retest -> room to the next real
                      liquidity target
  BREAKOUT_RETEST     genuine compression -> displacement break that CLOSES
                      beyond structure -> price did not fail back inside ->
                      retest holds -> HTF not strongly against -> not late
  HTF_ZONE_REVERSAL   price trading inside a fresh H1/M30 zone -> location
                      agrees (discount for longs) -> M5 CHoCH/MSS with
                      displacement out of the zone -> HTF not strongly
                      against -> room
  FVG_CONTINUATION    HTF bias -> prior M5 BOS in that direction ->
                      displacement created an FVG -> price returns into the
                      unmitigated FVG -> rejection close -> room

M1 is never a setup source.  No family reads M1 to create a trade idea; M1
exists only to time the fill (next-bar open) and to update excursions.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from adaptive_bot.core.models import (Candle, Direction, FairValueGap,
                                      LiquidityKind, OrderBlock, SessionName,
                                      StructureEvent, StructureEventKind,
                                      TrendState, Zone, ZoneKind)
from adaptive_bot.strategy.fair_value_gap import FVGDetector
from adaptive_bot.strategy.supply_demand import (OrderBlockDetector,
                                                 SupplyDemandDetector)
from .confluence import ConfluenceEngine, ConfluenceInputs, ConfluenceResult
from .features_v5 import TFFeatures
from .market_state_v5 import MarketState
from .trade_plan import Invalidation, StopBuilder, TargetBuilder

FAMILIES: Tuple[str, ...] = (
    "LIQUIDITY_REVERSAL", "TREND_CONTINUATION", "SESSION_LIQUIDITY",
    "BREAKOUT_RETEST", "HTF_ZONE_REVERSAL", "FVG_CONTINUATION")

# Bounded parameter space.  The learning system may only move parameters
# inside these bounds, and only for parameters listed here.
PARAM_BOUNDS: Dict[str, Tuple[float, float]] = {
    "confluence_delta": (-2.0, 10.0),
    "sweep_max_bars": (2.0, 12.0),
    "retest_atr": (0.10, 0.80),
    "max_extension_atr": (0.60, 3.00),
    "shift_max_bars": (1.0, 8.0),
    "compression_max_atr": (0.90, 3.00),
    "min_compression_bars": (5.0, 20.0),
    "retest_max_bars": (2.0, 15.0),
    "max_late_atr": (0.30, 2.00),
    "zone_min_quality": (0.30, 0.80),
    "fvg_max_age": (4.0, 30.0),
    "partial_fraction": (0.30, 0.55),
    "be_min_r": (0.80, 1.80),
    "trail_buffer_atr": (0.15, 0.80),
    "early_exit_min_score": (2.0, 5.0),
}

DEFAULT_PARAMS: Dict[str, float] = {
    "confluence_delta": 0.0,
    "sweep_max_bars": 6.0,
    "retest_atr": 0.35,
    "max_extension_atr": 1.60,
    "shift_max_bars": 4.0,
    "compression_max_atr": 1.80,
    "min_compression_bars": 8.0,
    "retest_max_bars": 8.0,
    "max_late_atr": 0.90,
    "zone_min_quality": 0.45,
    "fvg_max_age": 12.0,
    "partial_fraction": 0.45,
    "be_min_r": 1.00,
    "trail_buffer_atr": 0.35,
    "early_exit_min_score": 3.0,
}

# Small, genuinely meaningful variants per family — NOT 21 near-duplicates.
# Each variant changes something a trader would recognise as a different
# decision, and the ranking always reports the family as well as the variant.
VARIANT_SEEDS: Dict[str, List[Tuple[str, Dict[str, float]]]] = {
    "LIQUIDITY_REVERSAL": [
        ("A", {}),
        ("B", {"confluence_delta": 6.0, "sweep_max_bars": 3.0}),
        ("C", {"retest_atr": 0.55, "partial_fraction": 0.50}),
    ],
    "TREND_CONTINUATION": [
        ("A", {}),
        ("B", {"max_extension_atr": 1.00, "retest_atr": 0.25}),
        ("C", {"confluence_delta": 5.0, "be_min_r": 1.40,
               "trail_buffer_atr": 0.55}),
    ],
    "SESSION_LIQUIDITY": [
        ("A", {}),
        ("B", {"confluence_delta": 4.0, "sweep_max_bars": 4.0}),
    ],
    "BREAKOUT_RETEST": [
        ("A", {}),
        ("B", {"compression_max_atr": 1.30, "max_late_atr": 0.60}),
        ("C", {"retest_max_bars": 12.0, "partial_fraction": 0.35}),
    ],
    "HTF_ZONE_REVERSAL": [
        ("A", {}),
        ("B", {"zone_min_quality": 0.60, "confluence_delta": 4.0}),
    ],
    "FVG_CONTINUATION": [
        ("A", {}),
        ("B", {"fvg_max_age": 6.0, "retest_atr": 0.25}),
    ],
}


@dataclass
class StrategyVariant:
    """A strategy is a stored CONFIGURATION, never code."""
    sid: str
    family: str
    label: str
    version: int
    params: Dict[str, float]
    status: str = "active"            # active | benched | retired
    bench_until: str = ""
    bench_at_n: int = 0               # trade count when a bench was served
    parent: str = ""
    mutations: int = 0
    created: str = ""

    def p(self, name: str) -> float:
        return float(self.params.get(name, DEFAULT_PARAMS.get(name, 0.0)))

    def to_dict(self) -> dict:
        return {"sid": self.sid, "family": self.family, "label": self.label,
                "version": self.version, "params": dict(self.params),
                "status": self.status, "bench_until": self.bench_until,
                "bench_at_n": self.bench_at_n, "parent": self.parent,
                "mutations": self.mutations, "created": self.created}

    @staticmethod
    def from_dict(d: dict) -> "StrategyVariant":
        return StrategyVariant(
            sid=d["sid"], family=d["family"], label=d.get("label", "A"),
            version=int(d.get("version", 1)), params=dict(d.get("params", {})),
            status=d.get("status", "active"),
            bench_until=d.get("bench_until", ""),
            bench_at_n=int(d.get("bench_at_n", 0)), parent=d.get("parent", ""),
            mutations=int(d.get("mutations", 0)), created=d.get("created", ""))


@dataclass
class SetupCandidate:
    family: str
    sid: str
    version: int
    direction: Direction
    created: datetime
    entry_ref: float
    stop: float
    stop_reason: str
    tp1: float
    tp1_reason: str
    target: float
    target_reason: str
    tp1_r: float
    target_r: float
    blended_rr: float
    confluence: ConfluenceResult
    sequence: List[str]
    invalidation: str
    regime: str
    session: str
    htf_bias: str
    location: str
    sweep_kind: str = ""
    notes: List[str] = field(default_factory=list)
    params: Dict[str, float] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        return (f"{self.family} {self.direction.value} "
                f"conf {self.confluence.score:.0f} | stop: {self.stop_reason} "
                f"| target: {self.target_reason}")


class GateLog:
    """Records each mandatory sequence step and the first failure.

    `detail` describes why the gate FAILED; `passed` describes what satisfied
    it.  Keeping the two apart is what stops the log from printing lines like
    "PASS M5_CONTINUATION: no recent M5 BOS", which would make the research
    trail worse than useless."""

    def __init__(self, family: str, direction: Direction):
        self.family = family
        self.direction = direction
        self.lines: List[str] = []
        self.failure: str = ""

    def require(self, name: str, ok: bool, detail: str,
                passed: str = "") -> bool:
        ok = bool(ok)
        text = (passed or detail) if ok else detail
        self.lines.append(f"{'PASS' if ok else 'FAIL'} {name}: {text}")
        if not ok and not self.failure:
            self.failure = f"{name}: {detail}"
        return ok

    def note(self, text: str) -> None:
        self.lines.append("note " + text)

    @property
    def ok(self) -> bool:
        return not self.failure


# ===========================================================================
# helpers shared by the families
# ===========================================================================

def _last_event(feat: TFFeatures, direction: Direction,
                kinds: Tuple[StructureEventKind, ...],
                min_index: int = 0) -> Optional[StructureEvent]:
    for ev in reversed(feat.structure.events):
        if ev.index < min_index:
            break
        if ev.direction == direction and ev.kind in kinds:
            return ev
    return None


def _zone_for(feat: Optional[TFFeatures], direction: Direction, price: float,
              tolerance: float, min_quality: float = 0.30) -> Optional[Zone]:
    if feat is None:
        return None
    want = ZoneKind.DEMAND if direction == Direction.LONG else ZoneKind.SUPPLY
    best: Optional[Zone] = None
    for z in SupplyDemandDetector.active_zones(feat.zones, want, min_quality):
        if z.lower - tolerance <= price <= z.upper + tolerance:
            if best is None or z.quality() > best.quality():
                best = z
    return best


def _ob_for(feat: Optional[TFFeatures], direction: Direction, price: float,
            tolerance: float) -> Optional[OrderBlock]:
    if feat is None:
        return None
    best: Optional[OrderBlock] = None
    for b in OrderBlockDetector.usable(feat.order_blocks, direction, 0.30):
        if b.lower - tolerance <= price <= b.upper + tolerance:
            if best is None or b.freshness > best.freshness:
                best = b
    return best


def _fvg_for(feat: Optional[TFFeatures], direction: Direction, price: float,
             tolerance: float, max_age_bars: Optional[int] = None
             ) -> Optional[FairValueGap]:
    if feat is None:
        return None
    n = len(feat.candles)
    best: Optional[FairValueGap] = None
    for g in FVGDetector.usable(feat.fvgs, direction):
        if max_age_bars is not None and (n - 1 - g.created_index) > max_age_bars:
            continue
        if g.lower - tolerance <= price <= g.upper + tolerance:
            if best is None or g.size > best.size:
                best = g
    return best


def _rejection_close(candle: Candle, direction: Direction) -> bool:
    """The confirming candle must actually close in the trade direction with
    a body that dominates the wick against the trade."""
    if direction == Direction.LONG:
        return candle.close > candle.open and candle.close > (
            candle.low + 0.5 * candle.range) if candle.range > 0 else False
    return candle.close < candle.open and candle.close < (
        candle.high - 0.5 * candle.range) if candle.range > 0 else False


def _displacement_after(feat: TFFeatures, direction: Direction,
                        from_index: int, max_bars: int) -> bool:
    n = len(feat.candles)
    end = min(n, from_index + max_bars + 1)
    for i in range(max(0, from_index), end):
        if i < len(feat.displacement_flags) and feat.displacement_flags[i]:
            c = feat.candles[i]
            if (c.close > c.open) == (direction == Direction.LONG):
                return True
    return False


# ===========================================================================
# the library
# ===========================================================================

class SetupLibrary:

    def __init__(self, cfg):
        self.cfg = cfg
        self.confluence = ConfluenceEngine(cfg)
        self.stops = StopBuilder(cfg)
        self.targets = TargetBuilder(cfg)

    # ----------------------------------------------------------------- entry
    def evaluate(self, var: StrategyVariant, state: MarketState,
                 direction: Direction, mpu: float,
                 point: float) -> Tuple[Optional[SetupCandidate], GateLog]:
        """Run one variant against one direction. Returns (candidate, log)."""
        fam = var.family
        g = GateLog(fam, direction)
        if not state.ready():
            g.require("DATA", False, "multi-timeframe features not ready")
            return None, g
        if state.regime in ("ABNORMAL_SPREAD", "NEWS_VOLATILITY", "UNSAFE"):
            g.require("REGIME_SAFE", False,
                      f"regime {state.regime} disables all setups")
            return None, g

        handler = {
            "LIQUIDITY_REVERSAL": self._liquidity_reversal,
            "TREND_CONTINUATION": self._trend_continuation,
            "SESSION_LIQUIDITY": self._session_liquidity,
            "BREAKOUT_RETEST": self._breakout_retest,
            "HTF_ZONE_REVERSAL": self._htf_zone_reversal,
            "FVG_CONTINUATION": self._fvg_continuation,
        }.get(fam)
        if handler is None:
            g.require("FAMILY", False, f"unknown family {fam}")
            return None, g

        found = handler(var, state, direction, g)
        if found is None or not g.ok:
            return None, g
        inputs, invalidations, extra_notes = found

        # ---- structural stop ------------------------------------------------
        entry_ref = inputs.entry_ref
        sp = self.stops.build(state, direction, entry_ref, invalidations,
                              state.atr_m5, point)
        if sp.rejected:
            g.require("STOP", False, sp.reject_reason)
            return None, g
        g.require("STOP", True, f"{sp.price:.2f} ({sp.reason}), distance "
                                f"{sp.distance:.2f}")
        inputs.stop = sp.price

        # ---- structural targets ---------------------------------------------
        tp = self.targets.build(state, direction, entry_ref, sp.price,
                                state.atr_m5, point, mpu, fam)
        if tp.rejected:
            g.require("TARGET", False, tp.reject_reason)
            return None, g
        g.require("TARGET", True,
                  f"TP1 {tp.tp1:.2f} ({tp.tp1_reason}) {tp.tp1_r:.2f}R, "
                  f"target {tp.target:.2f} ({tp.target_reason}) "
                  f"{tp.target_r:.2f}R net, blended {tp.blended_rr:.2f}R")
        inputs.target = tp.target
        inputs.room_price = state.room_to_next_pool(direction, entry_ref)

        # ---- confluence ------------------------------------------------------
        conf = self.confluence.evaluate(state, inputs)
        threshold = self.threshold_for(var)
        if not g.require("CONFLUENCE", conf.clears(threshold),
                         f"score {conf.score:.0f} is below the "
                         f"{threshold:.0f} threshold — missing: "
                         f"{', '.join(conf.failed_names) or 'none'}",
                         f"score {conf.score:.0f} clears the "
                         f"{threshold:.0f} threshold"
                         + (f" (missing: {', '.join(conf.failed_names)})"
                            if conf.failed_names else "")):
            return None, g

        cand = SetupCandidate(
            family=fam, sid=var.sid, version=var.version, direction=direction,
            created=state.m5.last.time, entry_ref=entry_ref, stop=sp.price,
            stop_reason=sp.reason, tp1=tp.tp1, tp1_reason=tp.tp1_reason,
            target=tp.target, target_reason=tp.target_reason, tp1_r=tp.tp1_r,
            target_r=tp.target_r, blended_rr=tp.blended_rr, confluence=conf,
            sequence=list(g.lines),
            invalidation=f"close beyond {sp.price:.2f} ({sp.reason}) "
                         f"invalidates the idea",
            regime=state.regime, session=state.session.value,
            htf_bias=(state.bias.direction.value if state.bias.direction
                      else "NONE") + f"/{state.bias.strength}",
            location=f"{state.location.label}@{state.location.position:.2f}",
            sweep_kind=(inputs.sweep.level.kind.value if inputs.sweep
                        else ""),
            notes=sp.notes + tp.notes + extra_notes + list(inputs.notes),
            params=dict(var.params))
        return cand, g

    def threshold_for(self, var: StrategyVariant) -> float:
        base = self.cfg.family_min_confluence.get(var.family,
                                                  self.cfg.min_confluence)
        return max(self.cfg.min_confluence,
                   base + var.p("confluence_delta"))

    # =================================================================== (1)
    def _liquidity_reversal(self, var, state: MarketState,
                            direction: Direction, g: GateLog):
        m5, m15 = state.m5, state.m15
        atr = state.atr_m5
        notes: List[str] = []

        # G1 — HTF context or decisive location, and never straight into a
        # strong opposing higher-timeframe trend
        ctx_ok = state.bias.supports(direction) or state.location.favours(direction)
        if not g.require("HTF_CONTEXT", ctx_ok,
                         f"bias {state.bias.reason}; location "
                         f"{state.location.label} "
                         f"{state.location.position:.2f}"):
            return None
        if not g.require("NOT_AGAINST_HTF",
                         not state.bias.strongly_against(direction),
                         f"bias {state.bias.reason}"):
            return None

        # G2 — an important pool swept against the trade's direction
        sweeps = state.sweeps_for(direction, int(var.p("sweep_max_bars")))
        if not g.require("LIQUIDITY_SWEEP", bool(sweeps),
                         f"no important pool swept within "
                         f"{int(var.p('sweep_max_bars'))} M5 bars",
                         f"{len(sweeps)} important pool(s) swept recently"):
            return None
        sweep = sweeps[0]
        if not g.require("SWEEP_VALID",
                         sweep.closed_back or sweep.displaced_away,
                         f"{sweep.level.kind.value} sweep neither closed back "
                         f"nor displaced away",
                         f"{sweep.level.kind.value} sweep was "
                         + ("reclaimed on the close"
                            if sweep.closed_back else "followed by "
                            "displacement away")):
            return None
        g.note(f"swept {sweep.level.kind.value} at {sweep.extreme:.2f}, "
               f"{sweep.bars_since} bars ago "
               f"({'major' if sweep.is_major else 'minor'} pool)")

        # G3 — CHoCH/MSS on M15 or M5 AFTER the sweep, against the short move
        n5 = len(m5.candles)
        sweep_index = max(0, n5 - 1 - sweep.bars_since)
        ev = _last_event(m5, direction,
                         (StructureEventKind.CHOCH, StructureEventKind.MSS),
                         min_index=sweep_index)
        tf_used = "M5"
        if ev is None and m15 is not None:
            ev = _last_event(m15, direction,
                             (StructureEventKind.CHOCH, StructureEventKind.MSS))
            if ev is not None and ev.time < sweep.swept_time:
                ev = None
            tf_used = "M15"
        if not g.require("STRUCTURE_SHIFT", ev is not None,
                         "no CHoCH/MSS in the trade direction after the sweep "
                         "(a sweep alone is never a setup)",
                         f"{tf_used} {ev.kind.value} after the sweep"
                         if ev is not None else ""):
            return None
        g.note(f"{tf_used} {ev.kind.value} broke {ev.broken_level:.2f}")

        # G4 — the shift must carry displacement
        disp = ev.displacement or _displacement_after(
            m5, direction, sweep_index, int(var.p("shift_max_bars")))
        if not g.require("DISPLACEMENT", disp,
                         "structure shift had no displacement",
                         "the structure shift carried displacement"):
            return None

        # G5 — a retest location produced by that displacement
        tol = var.p("retest_atr") * atr
        price = m5.close
        fvg = _fvg_for(m5, direction, price, tol, int(var.p("fvg_max_age")))
        ob = _ob_for(m5, direction, price, tol)
        zone = _zone_for(m5, direction, price, tol, var.p("zone_min_quality")) \
            or _zone_for(m15, direction, price, tol, var.p("zone_min_quality"))
        found = [label for label, obj in (("FVG", fvg), ("order block", ob),
                                          ("zone", zone)) if obj is not None]
        if not g.require("RETEST_LOCATION", bool(found),
                         "price is not retesting an FVG, order block or "
                         "supply/demand zone from the displacement",
                         "retesting " + ", ".join(found)):
            return None

        # G6 — a confirming close (M1 may refine timing but never creates this)
        if not g.require("CONFIRMATION", _rejection_close(m5.last, direction),
                         f"M5 close {m5.last.close:.2f} is not a rejection "
                         f"close in the trade direction",
                         f"M5 rejection close at {m5.last.close:.2f}"):
            return None
        notes.append("M1 is used only to time the fill; the idea comes from "
                     "M15/M5 structure after an H1/M30-contextual sweep")

        invalidations = [Invalidation(sweep.extreme, "swept "
                                     + sweep.level.kind.value)]
        ps = m5.protected_swing(direction)
        if ps is not None:
            invalidations.append(Invalidation(ps.price, "M5 protected swing"))
        if zone is not None:
            invalidations.append(Invalidation(
                zone.lower if direction == Direction.LONG else zone.upper,
                f"{zone.timeframe.value} {zone.kind.value} zone edge"))
        if ob is not None:
            invalidations.append(Invalidation(
                ob.lower if direction == Direction.LONG else ob.upper,
                "order block edge"))

        inputs = ConfluenceInputs(
            direction=direction, family="LIQUIDITY_REVERSAL",
            entry_ref=m5.close, stop=0.0, target=0.0, sweep=sweep,
            structure_event=ev, zone=zone, order_block=ob, fvg=fvg,
            displacement=True, notes=notes)
        return inputs, invalidations, []

    # =================================================================== (2)
    def _trend_continuation(self, var, state: MarketState,
                            direction: Direction, g: GateLog):
        m5, m15 = state.m5, state.m15
        atr = state.atr_m5
        notes: List[str] = []

        # G1 — the higher-timeframe trend must be CLEAR
        if not g.require("HTF_TREND_CLEAR",
                         state.bias.supports(direction)
                         and state.bias.strength in ("STRONG", "MODERATE"),
                         f"bias {state.bias.reason} "
                         f"(strength {state.bias.strength})"):
            return None
        # G2 — M15 must agree
        want = TrendState.BULLISH if direction == Direction.LONG \
            else TrendState.BEARISH
        if not g.require("M15_AGREES", state.bias.m15_trend == want,
                         f"M15 trend {state.bias.m15_trend.value}"):
            return None

        # G3 — a pullback into a LOGICAL area
        tol = var.p("retest_atr") * atr
        price = m5.close
        zone = _zone_for(m15, direction, price, tol, var.p("zone_min_quality")) \
            or _zone_for(m5, direction, price, tol, var.p("zone_min_quality"))
        ob = _ob_for(m15, direction, price, tol) or _ob_for(m5, direction,
                                                            price, tol)
        fvg = _fvg_for(m15, direction, price, tol) \
            or _fvg_for(m5, direction, price, tol, int(var.p("fvg_max_age")))
        pd_ok = state.location.favours(direction)
        ema_area = False
        if m5.ema20 and m5.ema50:
            band = max(tol, 0.25 * atr)
            ema_area = (abs(price - m5.ema20_now) <= band
                        or abs(price - m5.ema50_now) <= band)
        structural_support = state.bias.m15_trend == want
        areas: List[str] = []
        if zone is not None:
            areas.append(f"{zone.timeframe.value} {zone.kind.value} zone")
        if ob is not None:
            areas.append("order block")
        if fvg is not None:
            areas.append(f"{fvg.timeframe.value} FVG")
        if pd_ok:
            areas.append(f"{state.location.label} location")
        if ema_area and structural_support:
            areas.append("EMA band with structural support")
        elif ema_area:
            g.note("EMA band touched but ignored: structure does not support "
                   "it (an EMA alone is not a location)")
        if not g.require("PULLBACK_AREA", bool(areas),
                         "pullback is not into any logical area "
                         "(zone / order block / FVG / discount-premium, or "
                         "an EMA band backed by structure)",
                         "pullback into " + ", ".join(areas)):
            return None

        # G4 — M5 BOS or continuation displacement in the trend direction
        n5 = len(m5.candles)
        ev = _last_event(m5, direction,
                         (StructureEventKind.BOS, StructureEventKind.MSS),
                         min_index=max(0, n5 - 1 - int(var.p("retest_max_bars"))))
        disp = m5.displaced_within(int(var.p("shift_max_bars")), direction)
        if not g.require("M5_CONTINUATION", ev is not None or disp,
                         "no recent M5 BOS or continuation displacement in "
                         "the trend direction",
                         (f"M5 {ev.kind.value} at {ev.broken_level:.2f}"
                          if ev is not None else "M5 continuation "
                          "displacement in the trend direction")):
            return None

        # G5 — controlled retest, not a chase
        extension = abs(price - m5.ema20_now) / atr if atr > 0 else 99.0
        if not g.require("NOT_EXTENDED",
                         extension <= var.p("max_extension_atr"),
                         f"price is {extension:.2f} ATR from the M5 EMA20 "
                         f"(max {var.p('max_extension_atr'):.2f}) — that is "
                         f"chasing, not a controlled retest",
                         f"price is {extension:.2f} ATR from the M5 EMA20 "
                         f"(max {var.p('max_extension_atr'):.2f}) — a "
                         f"controlled retest"):
            return None
        if not g.require("CONFIRMATION", _rejection_close(m5.last, direction),
                         "no confirming close in the trend direction",
                         f"confirming close at {m5.last.close:.2f}"):
            return None

        # G6 — enough room to opposing liquidity/structure
        room = state.room_to_next_pool(direction, price)
        room_ok = room is None or room >= self.cfg.min_room_atr * atr
        if not g.require("ROOM", room_ok,
                         f"only {room:.2f} price to the next opposing pool "
                         f"({(room / atr if atr else 0):.2f} ATR, need "
                         f"{self.cfg.min_room_atr:.2f})"
                         if room is not None else "no pool measured",
                         (f"{room:.2f} price of room "
                          f"({(room / atr if atr else 0):.2f} ATR)"
                          if room is not None
                          else "no opposing pool in the way")):
            return None

        invalidations: List[Invalidation] = []
        ps = m5.protected_swing(direction)
        if ps is not None:
            invalidations.append(Invalidation(ps.price, "M5 protected swing"))
        ps15 = m15.protected_swing(direction) if m15 else None
        if ps15 is not None:
            invalidations.append(Invalidation(ps15.price,
                                              "M15 protected swing"))
        if zone is not None:
            invalidations.append(Invalidation(
                zone.lower if direction == Direction.LONG else zone.upper,
                f"{zone.timeframe.value} {zone.kind.value} zone edge"))
        if ob is not None:
            invalidations.append(Invalidation(
                ob.lower if direction == Direction.LONG else ob.upper,
                "order block edge"))

        inputs = ConfluenceInputs(
            direction=direction, family="TREND_CONTINUATION",
            entry_ref=price, stop=0.0, target=0.0,
            structure_event=ev, zone=zone, order_block=ob, fvg=fvg,
            displacement=disp or (ev.displacement if ev else False),
            notes=notes)
        return inputs, invalidations, []

    # =================================================================== (3)
    def _session_liquidity(self, var, state: MarketState,
                           direction: Direction, g: GateLog):
        m5, m15 = state.m5, state.m15
        atr = state.atr_m5

        # G1 — must be inside an active session
        if not g.require("ACTIVE_SESSION",
                         state.session in (SessionName.LONDON,
                                           SessionName.NEW_YORK,
                                           SessionName.OVERLAP),
                         f"session {state.session.value} is not an active "
                         f"session for this family",
                         f"active session {state.session.value}"):
            return None

        # G2 — a PREVIOUS session's or previous day's pool must be swept
        wanted = (LiquidityKind.SESSION_HIGH, LiquidityKind.SESSION_LOW,
                  LiquidityKind.PDH, LiquidityKind.PDL,
                  LiquidityKind.EQUAL_HIGHS, LiquidityKind.EQUAL_LOWS)
        sweeps = [s for s in state.sweeps_for(direction,
                                              int(var.p("sweep_max_bars")))
                  if s.level.kind in wanted]
        if not g.require("SESSION_POOL_SWEPT", bool(sweeps),
                         "no session / previous-day pool swept within "
                         f"{int(var.p('sweep_max_bars'))} M5 bars",
                         f"{len(sweeps)} session/previous-day pool(s) swept"):
            return None
        sweep = sweeps[0]
        if not g.require("SWEEP_VALID",
                         sweep.closed_back or sweep.displaced_away,
                         f"{sweep.level.kind.value} sweep was not reclaimed",
                         f"{sweep.level.kind.value} sweep reclaimed"):
            return None
        g.note(f"{sweep.level.kind.value} at {sweep.level.price:.2f} swept to "
               f"{sweep.extreme:.2f} during {state.session.value}")

        n5 = len(m5.candles)
        sweep_index = max(0, n5 - 1 - sweep.bars_since)

        # G3 — displacement after the sweep
        if not g.require("DISPLACEMENT",
                         _displacement_after(m5, direction, sweep_index,
                                             int(var.p("shift_max_bars"))),
                         "no displacement away from the swept level",
                         "displacement away from the swept level"):
            return None

        # G4 — CHoCH/MSS after the sweep
        ev = _last_event(m5, direction,
                         (StructureEventKind.CHOCH, StructureEventKind.MSS,
                          StructureEventKind.BOS), min_index=sweep_index)
        if not g.require("STRUCTURE_SHIFT", ev is not None,
                         "no structure shift after the session sweep",
                         f"M5 {ev.kind.value} after the sweep"
                         if ev is not None else ""):
            return None

        # G5 — FVG or order-block retest is REQUIRED for this family
        tol = var.p("retest_atr") * atr
        price = m5.close
        fvg = _fvg_for(m5, direction, price, tol, int(var.p("fvg_max_age")))
        ob = _ob_for(m5, direction, price, tol)
        if not g.require("FVG_OB_RETEST", fvg is not None or ob is not None,
                         "price is not retesting the FVG or order block left "
                         "by the displacement",
                         "retesting the " + ("FVG" if fvg is not None
                                             else "order block")):
            return None
        if not g.require("CONFIRMATION", _rejection_close(m5.last, direction),
                         "no confirming close on the retest",
                         f"confirming close at {m5.last.close:.2f}"):
            return None

        # G6 — room to the next real liquidity target
        room = state.room_to_next_pool(direction, price)
        if not g.require("ROOM_TO_TARGET",
                         room is not None
                         and room >= self.cfg.min_room_atr * atr,
                         f"insufficient room to the next real pool "
                         f"({(room / atr if (room and atr) else 0):.2f} ATR)",
                         f"{(room / atr if (room and atr) else 0):.2f} ATR of "
                         f"room to the next real pool"):
            return None

        zone = _zone_for(m15, direction, price, tol, var.p("zone_min_quality"))
        invalidations = [Invalidation(sweep.extreme,
                                      "swept " + sweep.level.kind.value)]
        ps = m5.protected_swing(direction)
        if ps is not None:
            invalidations.append(Invalidation(ps.price, "M5 protected swing"))
        if ob is not None:
            invalidations.append(Invalidation(
                ob.lower if direction == Direction.LONG else ob.upper,
                "order block edge"))

        inputs = ConfluenceInputs(
            direction=direction, family="SESSION_LIQUIDITY", entry_ref=price,
            stop=0.0, target=0.0, sweep=sweep, structure_event=ev, zone=zone,
            order_block=ob, fvg=fvg, displacement=True)
        return inputs, invalidations, []

    # =================================================================== (4)
    def _breakout_retest(self, var, state: MarketState,
                         direction: Direction, g: GateLog):
        m5 = state.m5
        atr = state.atr_m5
        candles = m5.candles
        n = len(candles)
        bars = int(var.p("min_compression_bars"))
        look = int(var.p("retest_max_bars"))
        if not g.require("DATA", n >= bars + look + 5,
                         "not enough M5 history for compression analysis",
                         f"{n} M5 bars available"):
            return None

        # G1 — genuine compression BEFORE the break
        window = candles[-(bars + look + 1):-(look + 1)]
        if not window:
            g.require("COMPRESSION", False, "empty compression window")
            return None
        hi = max(c.high for c in window)
        lo = min(c.low for c in window)
        rng = hi - lo
        comp_ratio = rng / atr if atr > 0 else 99.0
        if not g.require("COMPRESSION",
                         comp_ratio <= var.p("compression_max_atr"),
                         f"pre-break range {rng:.2f} is {comp_ratio:.2f} ATR "
                         f"(need <= {var.p('compression_max_atr'):.2f}) — no "
                         f"genuine accumulation",
                         f"genuine compression: pre-break range {rng:.2f} is "
                         f"{comp_ratio:.2f} ATR"):
            return None
        boundary = hi if direction == Direction.LONG else lo
        g.note(f"compression {lo:.2f}-{hi:.2f} ({comp_ratio:.2f} ATR) over "
               f"{len(window)} bars; boundary {boundary:.2f}")

        # G2 — a displacement break that CLOSES beyond the boundary
        break_idx = -1
        for i in range(n - look - 1, n):
            c = candles[i]
            closed_beyond = (c.close > boundary) if direction == Direction.LONG \
                else (c.close < boundary)
            if closed_beyond and i < len(m5.displacement_flags) \
                    and m5.displacement_flags[i]:
                break_idx = i
                break
        if not g.require("BREAK_WITH_DISPLACEMENT", break_idx >= 0,
                         "no displacement candle CLOSED beyond the "
                         "compression boundary",
                         "a displacement candle closed beyond the boundary"):
            return None
        g.note(f"break bar {candles[break_idx].time:%H:%M} closed "
               f"{candles[break_idx].close:.2f} beyond {boundary:.2f}")

        # G3 — the breakout must not have failed back inside the range
        failed_back = False
        for i in range(break_idx + 1, n - 1):
            c = candles[i]
            back_inside = (c.close < lo) if direction == Direction.LONG \
                else (c.close > hi)
            if back_inside:
                failed_back = True
                break
        if not g.require("NOT_FAILED_BREAK", not failed_back,
                         "price closed back inside the old range — this is a "
                         "failed breakout, not a retest",
                         "the break has not failed back inside the range"):
            return None

        # G4 — the retest must hold
        price = m5.close
        tol = var.p("retest_atr") * atr
        touched = False
        for i in range(break_idx + 1, n):
            c = candles[i]
            near = (c.low <= boundary + tol) if direction == Direction.LONG \
                else (c.high >= boundary - tol)
            if near:
                touched = True
                break
        if not g.require("RETEST_TOUCHED", touched,
                         "price has not returned to retest the broken "
                         "boundary yet",
                         f"boundary {boundary:.2f} was retested"):
            return None
        holds = (price > boundary) if direction == Direction.LONG \
            else (price < boundary)
        if not g.require("RETEST_HOLDS",
                         holds and _rejection_close(m5.last, direction),
                         f"retest did not hold with a confirming close "
                         f"(close {price:.2f} vs boundary {boundary:.2f})",
                         f"retest held with a confirming close {price:.2f}"):
            return None

        # G5 — higher timeframe not strongly against
        if not g.require("HTF_NOT_AGAINST",
                         not state.bias.strongly_against(direction),
                         f"higher timeframe is strongly against: "
                         f"{state.bias.reason}",
                         f"higher timeframe is not against it "
                         f"({state.bias.reason})"):
            return None

        # G6 — not a late entry
        late = abs(price - boundary) / atr if atr > 0 else 99.0
        if not g.require("NOT_LATE", late <= var.p("max_late_atr"),
                         f"price is {late:.2f} ATR beyond the boundary "
                         f"(max {var.p('max_late_atr'):.2f}) — late entry",
                         f"entry is only {late:.2f} ATR beyond the boundary "
                         f"— not late"):
            return None

        ev = _last_event(m5, direction, (StructureEventKind.BOS,
                                         StructureEventKind.MSS),
                         min_index=break_idx)
        fvg = _fvg_for(m5, direction, price, tol, int(var.p("fvg_max_age")))
        ob = _ob_for(m5, direction, price, tol)
        # primary: the broken boundary — if the retest fails, the idea is
        # dead.  The far side of the range is recorded as context only.
        invalidations = [
            Invalidation(boundary, "broken boundary (retest failure)"),
            Invalidation(lo if direction == Direction.LONG else hi,
                         "far side of the compression range")]
        inputs = ConfluenceInputs(
            direction=direction, family="BREAKOUT_RETEST", entry_ref=price,
            stop=0.0, target=0.0, structure_event=ev, fvg=fvg, order_block=ob,
            displacement=True,
            notes=[f"range {lo:.2f}-{hi:.2f} retained for early-exit "
                   f"invalidation"])
        return inputs, invalidations, [f"RANGE={lo:.5f}:{hi:.5f}"]

    # =================================================================== (5)
    def _htf_zone_reversal(self, var, state: MarketState,
                           direction: Direction, g: GateLog):
        m5 = state.m5
        atr = state.atr_m5
        price = m5.close
        tol = var.p("retest_atr") * atr

        # G1 — price must be trading in a fresh higher-timeframe zone
        zone = _zone_for(state.h1, direction, price, tol,
                         var.p("zone_min_quality")) \
            or _zone_for(state.m30, direction, price, tol,
                         var.p("zone_min_quality"))
        if not g.require("HTF_ZONE", zone is not None,
                         f"price {price:.2f} is not inside a fresh H1/M30 "
                         f"{'demand' if direction == Direction.LONG else 'supply'} "
                         f"zone of quality >= "
                         f"{var.p('zone_min_quality'):.2f}",
                         f"inside a {zone.timeframe.value} {zone.kind.value} "
                         f"zone" if zone is not None else ""):
            return None
        g.note(f"{zone.timeframe.value} {zone.kind.value} zone "
               f"{zone.lower:.2f}-{zone.upper:.2f} quality "
               f"{zone.quality():.2f} pattern {zone.pattern.value}")

        # G2 — location must agree (this family is location-led)
        if not g.require("LOCATION_AGREES",
                         state.location.favours(direction)
                         or state.location.label == "EQUILIBRIUM",
                         f"{state.location.label} at "
                         f"{state.location.position:.2f} is hostile to a "
                         f"{direction.value}"):
            return None

        # G3 — M5 CHoCH/MSS with displacement out of the zone
        ev = _last_event(m5, direction, (StructureEventKind.CHOCH,
                                         StructureEventKind.MSS))
        n = len(m5.candles)
        recent = ev is not None and (n - 1 - ev.index) <= int(
            var.p("retest_max_bars"))
        if not g.require("STRUCTURE_SHIFT", recent,
                         "no recent M5 CHoCH/MSS out of the zone",
                         f"M5 {ev.kind.value} out of the zone"
                         if ev is not None else ""):
            return None
        disp = ev.displacement or m5.displaced_within(
            int(var.p("shift_max_bars")), direction)
        if not g.require("DISPLACEMENT", disp,
                         "the shift out of the zone had no displacement",
                         "the shift out of the zone carried displacement"):
            return None
        if not g.require("CONFIRMATION", _rejection_close(m5.last, direction),
                         "no confirming rejection close from the zone",
                         f"rejection close at {m5.last.close:.2f}"):
            return None

        # G4 — higher timeframe not strongly against
        if not g.require("HTF_NOT_AGAINST",
                         not state.bias.strongly_against(direction),
                         f"higher timeframe is strongly against: "
                         f"{state.bias.reason}",
                         f"higher timeframe is not against it "
                         f"({state.bias.reason})"):
            return None

        # G5 — room
        room = state.room_to_next_pool(direction, price)
        if not g.require("ROOM", room is None
                         or room >= self.cfg.min_room_atr * atr,
                         f"insufficient room "
                         f"({(room / atr if (room and atr) else 0):.2f} ATR)",
                         f"{(room / atr if (room and atr) else 0):.2f} ATR of "
                         f"room" if room is not None
                         else "no opposing pool in the way"):
            return None

        fvg = _fvg_for(m5, direction, price, tol, int(var.p("fvg_max_age")))
        ob = _ob_for(m5, direction, price, tol)
        invalidations = [Invalidation(
            zone.lower if direction == Direction.LONG else zone.upper,
            f"{zone.timeframe.value} {zone.kind.value} zone far edge")]
        ps = m5.protected_swing(direction)
        if ps is not None:
            invalidations.append(Invalidation(ps.price, "M5 protected swing"))
        inputs = ConfluenceInputs(
            direction=direction, family="HTF_ZONE_REVERSAL", entry_ref=price,
            stop=0.0, target=0.0, structure_event=ev, zone=zone, fvg=fvg,
            order_block=ob, displacement=True)
        return inputs, invalidations, []

    # =================================================================== (6)
    def _fvg_continuation(self, var, state: MarketState,
                          direction: Direction, g: GateLog):
        m5 = state.m5
        atr = state.atr_m5
        price = m5.close
        tol = var.p("retest_atr") * atr

        # G1 — higher-timeframe bias must support the direction
        if not g.require("HTF_BIAS", state.bias.supports(direction),
                         f"higher-timeframe bias does not support a "
                         f"{direction.value}: {state.bias.reason}",
                         f"bias supports a {direction.value} "
                         f"({state.bias.reason})"):
            return None

        # G2 — a prior M5 BOS in that direction: this is continuation, not a
        # blind gap fill
        n = len(m5.candles)
        ev = _last_event(m5, direction, (StructureEventKind.BOS,
                                         StructureEventKind.MSS),
                         min_index=max(0, n - 1 - 40))
        if not g.require("PRIOR_BOS", ev is not None,
                         "no prior M5 BOS/MSS in the bias direction",
                         f"prior M5 {ev.kind.value} in the bias direction"
                         if ev is not None else ""):
            return None

        # G3 — an unmitigated FVG from displacement, price back inside it
        fvg = _fvg_for(m5, direction, price, tol, int(var.p("fvg_max_age")))
        if not g.require("FVG_PRESENT", fvg is not None,
                         f"no unmitigated {direction.value} FVG within "
                         f"{int(var.p('fvg_max_age'))} bars at {price:.2f}",
                         "price is inside an unmitigated FVG"
                         if fvg is not None else ""):
            return None
        if not g.require("FVG_FROM_DISPLACEMENT",
                         fvg.from_displacement
                         or m5.displaced_within(int(var.p("fvg_max_age")) + 2,
                                                direction),
                         "the FVG was not created by displacement",
                         "the FVG came from displacement"):
            return None
        g.note(f"FVG {fvg.lower:.2f}-{fvg.upper:.2f} state "
               f"{fvg.state.value}, size {fvg.size:.2f}")

        # G4 — rejection close out of the gap
        if not g.require("CONFIRMATION", _rejection_close(m5.last, direction),
                         "no rejection close out of the FVG",
                         f"rejection close at {m5.last.close:.2f}"):
            return None

        # G5 — room
        room = state.room_to_next_pool(direction, price)
        if not g.require("ROOM", room is None
                         or room >= self.cfg.min_room_atr * atr,
                         f"insufficient room "
                         f"({(room / atr if (room and atr) else 0):.2f} ATR)",
                         f"{(room / atr if (room and atr) else 0):.2f} ATR of "
                         f"room" if room is not None
                         else "no opposing pool in the way"):
            return None

        zone = _zone_for(state.m15, direction, price, tol,
                         var.p("zone_min_quality"))
        ob = _ob_for(m5, direction, price, tol)
        invalidations = [Invalidation(
            fvg.lower if direction == Direction.LONG else fvg.upper,
            "far edge of the fair-value gap")]
        ps = m5.protected_swing(direction)
        if ps is not None:
            invalidations.append(Invalidation(ps.price, "M5 protected swing"))
        inputs = ConfluenceInputs(
            direction=direction, family="FVG_CONTINUATION", entry_ref=price,
            stop=0.0, target=0.0, structure_event=ev, zone=zone,
            order_block=ob, fvg=fvg, displacement=True)
        return inputs, invalidations, []


# ===========================================================================
# population construction
# ===========================================================================

def seed_population(cfg, created: str = "") -> List[StrategyVariant]:
    """Deterministic starting population: every family, a few real variants."""
    out: List[StrategyVariant] = []
    for family in FAMILIES:
        seeds = VARIANT_SEEDS.get(family, [("A", {})])
        for label, overrides in seeds[:cfg.max_variants_per_family]:
            params = dict(DEFAULT_PARAMS)
            params.update(overrides)
            out.append(StrategyVariant(
                sid=f"{family}-{label}", family=family, label=label,
                version=1, params=params, created=created))
            if len(out) >= cfg.max_population:
                return out
    return out


def mutate_variant(parent: StrategyVariant, serial: int, rng: random.Random,
                   created: str) -> StrategyVariant:
    """A bounded parameter variant of a proven parent.

    Only parameters in PARAM_BOUNDS may move, only by a bounded step, and the
    result is clamped to the published bounds.  Nothing else about the
    strategy can change: the family's mandatory sequence is fixed in code and
    is never mutated."""
    params = dict(parent.params)
    names = [n for n in PARAM_BOUNDS if n in params]
    rng.shuffle(names)
    changed: List[str] = []
    for name in names[:2]:
        lo, hi = PARAM_BOUNDS[name]
        span = hi - lo
        step = rng.uniform(-0.22, 0.22) * span
        new = min(hi, max(lo, params[name] + step))
        if name in ("sweep_max_bars", "min_compression_bars",
                    "retest_max_bars", "fvg_max_age", "shift_max_bars",
                    "early_exit_min_score"):
            new = float(int(round(new)))
            new = min(hi, max(lo, new))
        if abs(new - params[name]) > 1e-9:
            params[name] = new
            changed.append(name)
    label = f"{parent.label}m{serial}"
    return StrategyVariant(
        sid=f"{parent.family}-{label}", family=parent.family, label=label,
        version=parent.version + 1, params=params, parent=parent.sid,
        mutations=parent.mutations + 1, created=created)
