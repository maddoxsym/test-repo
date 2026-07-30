"""
Transparent confluence scoring.

Every factor is named, weighted, and recorded as passed or failed with the
evidence that decided it, so any entry (real or shadow) can be explained
after the fact from the CSV trail alone.

CORRELATION HANDLING — this is the part V4 got wrong by scoring signals
independently.  Factors are grouped, and each GROUP has a cap.  The group's
contribution is min(sum of its passed weights, cap), so confirmations that
measure the same underlying thing cannot stack into a high score:

  HTF_DIRECTION  raw 22  cap 20   H1 and M30 trend agreement
  LTF_STRUCTURE  raw 33  cap 25   M15 trend, M5 CHoCH/MSS, displacement AND
                                  the EMA stack — the EMA is deliberately in
                                  the same group as market structure, because
                                  "price above its EMAs" and "structure is
                                  bullish" are largely the same observation
  LOCATION       raw 32  cap 22   premium/discount, supply/demand zone,
                                  order block, fair-value gap — all of these
                                  say "price is in a good place"
  LIQUIDITY      raw 22  cap 20   important sweep, room to opposing liquidity
  CONTEXT        raw 15  cap 13   session, spread, momentum/volume

The caps sum to 100, so a score is directly readable as a percentage of the
available evidence.  A setup family additionally has MANDATORY sequence
gates (see setups_v5) that are not scored at all: no amount of soft
confluence can substitute for the required sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from adaptive_bot.core.models import (Direction, FairValueGap, OrderBlock,
                                      SessionName, StructureEvent,
                                      StructureEventKind, TrendState, Zone)
from .market_state_v5 import MarketState, SweptPool

# group -> cap.  Caps sum to 100.
GROUP_CAPS: Dict[str, float] = {
    "HTF_DIRECTION": 20.0,
    "LTF_STRUCTURE": 25.0,
    "LOCATION": 22.0,
    "LIQUIDITY": 20.0,
    "CONTEXT": 13.0,
}

MAX_SCORE = sum(GROUP_CAPS.values())


@dataclass
class ConfluenceFactor:
    name: str
    group: str
    weight: float
    passed: bool
    detail: str

    def line(self) -> str:
        mark = "PASS" if self.passed else "fail"
        return (f"{mark} [{self.group}] {self.name} "
                f"({self.weight:+.0f}): {self.detail}")


@dataclass
class ConfluenceResult:
    score: float
    max_score: float
    factors: List[ConfluenceFactor] = field(default_factory=list)
    groups: Dict[str, Tuple[float, float, float]] = field(default_factory=dict)

    @property
    def passed_names(self) -> List[str]:
        return [f.name for f in self.factors if f.passed]

    @property
    def failed_names(self) -> List[str]:
        return [f.name for f in self.factors if not f.passed]

    def clears(self, threshold: float) -> bool:
        return self.score >= threshold

    def summary(self) -> str:
        return (f"confluence {self.score:.0f}/{self.max_score:.0f} "
                f"[{'+'.join(self.passed_names) or 'none'}]")

    def explain(self) -> List[str]:
        out = [f"confluence score {self.score:.0f} / {self.max_score:.0f}"]
        for name, (raw, capped, cap) in self.groups.items():
            note = " (capped: correlated confirmations)" if capped < raw else ""
            out.append(f"  group {name}: {capped:.0f} of cap {cap:.0f} "
                       f"[raw {raw:.0f}]{note}")
        for f in self.factors:
            out.append("  " + f.line())
        return out

    def as_csv_field(self) -> str:
        """Compact single-cell record of what passed and what failed."""
        parts = []
        for f in self.factors:
            parts.append(("+" if f.passed else "-") + f.name)
        return "|".join(parts)


@dataclass
class ConfluenceInputs:
    """What the setup family found; the engine turns it into a score."""
    direction: Direction
    family: str
    entry_ref: float
    stop: float
    target: float
    sweep: Optional[SweptPool] = None
    structure_event: Optional[StructureEvent] = None
    zone: Optional[Zone] = None
    order_block: Optional[OrderBlock] = None
    fvg: Optional[FairValueGap] = None
    room_price: Optional[float] = None
    displacement: bool = False
    notes: List[str] = field(default_factory=list)


class ConfluenceEngine:

    def __init__(self, cfg):
        self.cfg = cfg

    def evaluate(self, state: MarketState,
                 inp: ConfluenceInputs) -> ConfluenceResult:
        cfg = self.cfg
        d = inp.direction
        factors: List[ConfluenceFactor] = []

        def add(name: str, group: str, weight: float, passed: bool,
                detail: str) -> None:
            factors.append(ConfluenceFactor(name, group, weight, bool(passed),
                                            detail))

        want_trend = TrendState.BULLISH if d == Direction.LONG \
            else TrendState.BEARISH

        # ---------------------------------------------------- HTF_DIRECTION
        add("H1_ALIGNED", "HTF_DIRECTION", 12.0,
            state.bias.h1_trend == want_trend,
            f"H1 trend {state.bias.h1_trend.value}, want {want_trend.value}")
        add("M30_ALIGNED", "HTF_DIRECTION", 10.0,
            state.bias.m30_trend == want_trend,
            f"M30 trend {state.bias.m30_trend.value}")

        # ---------------------------------------------------- LTF_STRUCTURE
        add("M15_ALIGNED", "LTF_STRUCTURE", 7.0,
            state.bias.m15_trend == want_trend,
            f"M15 trend {state.bias.m15_trend.value}")
        ev = inp.structure_event
        add("M5_CHOCH_MSS", "LTF_STRUCTURE", 12.0,
            ev is not None and ev.direction == d
            and ev.kind in (StructureEventKind.CHOCH,
                            StructureEventKind.MSS,
                            StructureEventKind.BOS),
            f"{ev.kind.value} at {ev.broken_level:.2f}" if ev is not None
            else "no qualifying structure event")
        add("DISPLACEMENT", "LTF_STRUCTURE", 11.0, bool(inp.displacement),
            "structure shift carried displacement" if inp.displacement
            else "no displacement on the shift")
        # EMA deliberately shares the structure group's capped budget
        m5 = state.m5
        ema_ok = False
        ema_detail = "M5 features unavailable"
        if m5 is not None and m5.ema20 and m5.ema50:
            if d == Direction.LONG:
                ema_ok = m5.close > m5.ema20_now and m5.ema20_now > m5.ema50_now
            else:
                ema_ok = m5.close < m5.ema20_now and m5.ema20_now < m5.ema50_now
            ema_detail = (f"close {m5.close:.2f} vs EMA20 {m5.ema20_now:.2f} / "
                          f"EMA50 {m5.ema50_now:.2f}")
        add("EMA_STACK", "LTF_STRUCTURE", 3.0, ema_ok, ema_detail)

        # ---------------------------------------------------------- LOCATION
        add("PREMIUM_DISCOUNT", "LOCATION", 7.0,
            state.location.favours(d),
            f"{state.location.label} at {state.location.position:.2f} of the "
            f"H1 dealing range")
        z = inp.zone
        add("SD_ZONE", "LOCATION", 9.0, z is not None,
            f"{z.kind.value} {z.timeframe.value} zone "
            f"{z.lower:.2f}-{z.upper:.2f} quality {z.quality():.2f}"
            if z is not None else "no supply/demand zone at the entry")
        ob = inp.order_block
        add("ORDER_BLOCK", "LOCATION", 8.0, ob is not None,
            f"{ob.timeframe.value} order block {ob.lower:.2f}-{ob.upper:.2f} "
            f"freshness {ob.freshness:.2f}" if ob is not None
            else "no order block at the entry")
        g = inp.fvg
        add("FVG", "LOCATION", 8.0, g is not None,
            f"{g.timeframe.value} FVG {g.lower:.2f}-{g.upper:.2f} "
            f"({g.state.value})" if g is not None
            else "no fair-value gap at the entry")

        # --------------------------------------------------------- LIQUIDITY
        sw = inp.sweep
        add("IMPORTANT_SWEEP", "LIQUIDITY", 12.0,
            sw is not None and sw.is_major,
            f"{sw.level.kind.value} swept at {sw.extreme:.2f} "
            f"{sw.bars_since} bars ago" if sw is not None
            else "no important liquidity swept")
        room = inp.room_price
        room_ok = False
        if room is not None and state.atr_m5 > 0:
            room_ok = room >= cfg.min_room_atr * state.atr_m5
        add("ROOM_TO_LIQUIDITY", "LIQUIDITY", 10.0, room_ok,
            f"{room:.2f} price to the next opposing pool "
            f"({(room / state.atr_m5 if state.atr_m5 > 0 else 0):.2f} ATR, "
            f"need {cfg.min_room_atr:.2f})" if room is not None
            else "no opposing pool measured")

        # ----------------------------------------------------------- CONTEXT
        add("ACTIVE_SESSION", "CONTEXT", 5.0,
            state.session in (SessionName.LONDON, SessionName.NEW_YORK,
                              SessionName.OVERLAP),
            f"session {state.session.value}")
        add("SPREAD_OK", "CONTEXT", 4.0,
            0 < state.spread_points <= cfg.normal_spread_points,
            f"spread {state.spread_points:.0f} pts "
            f"(normal <= {cfg.normal_spread_points:.0f})")
        mom_ok = False
        mom_detail = "M5 features unavailable"
        if m5 is not None:
            roc_ok = (m5.roc > 0) == (d == Direction.LONG)
            vol_ok = m5.volume_ratio >= 1.0
            mom_ok = roc_ok and vol_ok
            mom_detail = (f"M5 ROC {m5.roc:+.4f}, volume "
                          f"{m5.volume_ratio:.2f}x its 20-bar mean")
        add("MOMENTUM_VOLUME", "CONTEXT", 6.0, mom_ok, mom_detail)

        return self._aggregate(factors)

    @staticmethod
    def _aggregate(factors: List[ConfluenceFactor]) -> ConfluenceResult:
        groups: Dict[str, Tuple[float, float, float]] = {}
        total = 0.0
        for group, cap in GROUP_CAPS.items():
            raw = sum(f.weight for f in factors
                      if f.group == group and f.passed)
            capped = min(raw, cap)
            groups[group] = (raw, capped, cap)
            total += capped
        return ConfluenceResult(score=round(total, 1), max_score=MAX_SCORE,
                                factors=factors, groups=groups)
