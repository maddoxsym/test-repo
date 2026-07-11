"""
Market structure: fractal swings, trend (HH/HL vs LH/LL), BOS / CHoCH / MSS
events, dealing range and premium/discount classification.

Everything works on COMPLETED candles and is confirmation-delayed so nothing
ever repaints: a swing only exists once `swing_right` candles have closed
after it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..core.config import Config
from ..core.helpers import atr_series, is_displacement
from ..core.models import (Candle, DealingRange, Direction, StructureEvent,
                           StructureEventKind, SwingKind, SwingPoint,
                           TrendState)


class SwingDetector:
    """Objective fractal swings.

    A swing HIGH at index i requires:  high[i] > high[i-k] for k=1..left AND
    high[i] >= high[i+k] with at least one strict > for k=1..right.
    The swing is only CONFIRMED once candle i+right has closed, so a swing
    can never repaint: detection at candle index j only reports swings with
    confirmed_index <= j."""

    def __init__(self, left: int, right: int):
        self.left = left
        self.right = right

    def detect(self, candles: Sequence[Candle]) -> List[SwingPoint]:
        n = len(candles)
        out: List[SwingPoint] = []
        if n < self.left + self.right + 1:
            return out
        for i in range(self.left, n - self.right):
            c = candles[i]
            is_high = all(c.high > candles[i - k].high for k in range(1, self.left + 1)) \
                and all(c.high >= candles[i + k].high for k in range(1, self.right + 1)) \
                and any(c.high > candles[i + k].high for k in range(1, self.right + 1))
            is_low = all(c.low < candles[i - k].low for k in range(1, self.left + 1)) \
                and all(c.low <= candles[i + k].low for k in range(1, self.right + 1)) \
                and any(c.low < candles[i + k].low for k in range(1, self.right + 1))
            if is_high:
                out.append(SwingPoint(i, c.time, c.high, SwingKind.HIGH,
                                      confirmed_index=i + self.right))
            if is_low:
                out.append(SwingPoint(i, c.time, c.low, SwingKind.LOW,
                                      confirmed_index=i + self.right))
        return out

    @staticmethod
    def alternating(swings: Sequence[SwingPoint]) -> List[SwingPoint]:
        """Reduce to a strictly alternating high/low sequence, keeping the
        more extreme point when two of the same kind are adjacent."""
        out: List[SwingPoint] = []
        for s in sorted(swings, key=lambda x: x.index):
            if not out or out[-1].kind != s.kind:
                out.append(s)
            else:
                last = out[-1]
                if s.kind == SwingKind.HIGH and s.price >= last.price:
                    out[-1] = s
                elif s.kind == SwingKind.LOW and s.price <= last.price:
                    out[-1] = s
        return out


@dataclass
class StructureState:
    trend: TrendState
    swings: List[SwingPoint]
    events: List[StructureEvent]
    dealing_range: Optional[DealingRange]
    last_confirmed_high: Optional[SwingPoint]
    last_confirmed_low: Optional[SwingPoint]
    protected_low: Optional[SwingPoint]     # last HL that must hold (bull)
    protected_high: Optional[SwingPoint]    # last LH that must hold (bear)


class StructureAnalyzer:
    """Objective market-structure engine.

    Definitions (all on COMPLETED candles, close-based by default):
      * trend BULLISH  : last two alternating swing highs form HH and last
                         two swing lows form HL.
      * trend BEARISH  : mirrored (LH + LL).
      * BOS            : close beyond the most recent confirmed swing extreme
                         IN the direction of the current trend (continuation).
      * CHoCH          : first close beyond the protected opposing swing
                         AGAINST the current trend.
      * MSS            : a CHoCH whose breaking candle shows displacement —
                         a graded, stronger shift.
    """

    def __init__(self, cfg: Config, left: Optional[int] = None,
                 right: Optional[int] = None):
        self.cfg = cfg
        self.detector = SwingDetector(left or cfg.swing_left,
                                      right or cfg.swing_right)

    def analyze(self, candles: Sequence[Candle],
                atr: Optional[List[float]] = None) -> StructureState:
        cfg = self.cfg
        n = len(candles)
        atr = atr or atr_series(candles, cfg.atr_period)
        raw = self.detector.detect(candles)
        swings = SwingDetector.alternating(raw)
        events: List[StructureEvent] = []
        trend = TrendState.UNDEFINED
        last_high: Optional[SwingPoint] = None
        last_low: Optional[SwingPoint] = None
        protected_low: Optional[SwingPoint] = None
        protected_high: Optional[SwingPoint] = None
        pending_break_high: Optional[SwingPoint] = None  # level to watch above
        pending_break_low: Optional[SwingPoint] = None
        swing_iter = 0

        for i in range(n):
            c = candles[i]
            # 1. absorb any swings that confirm at this candle
            while swing_iter < len(swings) and swings[swing_iter].confirmed_index <= i:
                s = swings[swing_iter]
                if s.kind == SwingKind.HIGH:
                    last_high = s
                    pending_break_high = s
                else:
                    last_low = s
                    pending_break_low = s
                trend = self._classify_trend(swings[:swing_iter + 1], trend)
                if trend == TrendState.BULLISH and s.kind == SwingKind.LOW:
                    protected_low = s
                if trend == TrendState.BEARISH and s.kind == SwingKind.HIGH:
                    protected_high = s
                swing_iter += 1

            # 2. break detection on this completed candle
            ref_price_up = c.close if cfg.bos_use_close else c.high
            ref_price_dn = c.close if cfg.bos_use_close else c.low
            a = atr[i] if i < len(atr) else 0.0
            disp = is_displacement(c, a, cfg.displacement_atr_mult,
                                   cfg.displacement_body_ratio)

            if pending_break_high and ref_price_up > pending_break_high.price:
                kind = self._event_kind(Direction.LONG, trend)
                ev = StructureEvent(kind, Direction.LONG, i, c.time,
                                    pending_break_high.price,
                                    pending_break_high, displacement=disp)
                if kind == StructureEventKind.CHOCH and disp:
                    ev.kind = StructureEventKind.MSS
                events.append(ev)
                if kind != StructureEventKind.BOS:
                    trend = TrendState.BULLISH
                    protected_low = last_low
                pending_break_high = None
            if pending_break_low and ref_price_dn < pending_break_low.price:
                kind = self._event_kind(Direction.SHORT, trend)
                ev = StructureEvent(kind, Direction.SHORT, i, c.time,
                                    pending_break_low.price,
                                    pending_break_low, displacement=disp)
                if kind == StructureEventKind.CHOCH and disp:
                    ev.kind = StructureEventKind.MSS
                events.append(ev)
                if kind != StructureEventKind.BOS:
                    trend = TrendState.BEARISH
                    protected_high = last_high
                pending_break_low = None

        dealing = self._dealing_range(swings)
        return StructureState(trend=trend, swings=swings, events=events,
                              dealing_range=dealing,
                              last_confirmed_high=last_high,
                              last_confirmed_low=last_low,
                              protected_low=protected_low,
                              protected_high=protected_high)

    @staticmethod
    def _event_kind(direction: Direction, trend: TrendState) -> StructureEventKind:
        if trend in (TrendState.UNDEFINED, TrendState.RANGING):
            return StructureEventKind.BOS
        if direction == Direction.LONG:
            return (StructureEventKind.BOS if trend == TrendState.BULLISH
                    else StructureEventKind.CHOCH)
        return (StructureEventKind.BOS if trend == TrendState.BEARISH
                else StructureEventKind.CHOCH)

    @staticmethod
    def _classify_trend(swings: Sequence[SwingPoint],
                        current: TrendState) -> TrendState:
        highs = [s for s in swings if s.kind == SwingKind.HIGH][-2:]
        lows = [s for s in swings if s.kind == SwingKind.LOW][-2:]
        if len(highs) < 2 or len(lows) < 2:
            return current if current != TrendState.UNDEFINED else TrendState.UNDEFINED
        hh = highs[1].price > highs[0].price
        hl = lows[1].price > lows[0].price
        lh = highs[1].price < highs[0].price
        ll = lows[1].price < lows[0].price
        if hh and hl:
            return TrendState.BULLISH
        if lh and ll:
            return TrendState.BEARISH
        return TrendState.RANGING

    @staticmethod
    def _dealing_range(swings: Sequence[SwingPoint]) -> Optional[DealingRange]:
        """Dealing range = most recent significant swing low <-> swing high
        (last 10 alternating swings, take extremes)."""
        recent = swings[-10:]
        highs = [s for s in recent if s.kind == SwingKind.HIGH]
        lows = [s for s in recent if s.kind == SwingKind.LOW]
        if not highs or not lows:
            return None
        hi = max(highs, key=lambda s: s.price)
        lo = min(lows, key=lambda s: s.price)
        if hi.price <= lo.price:
            return None
        return DealingRange(low=lo.price, high=hi.price,
                            low_time=lo.time, high_time=hi.time)

    def premium_discount(self, state: StructureState,
                         price: float) -> Tuple[str, float]:
        """Return ('PREMIUM'|'DISCOUNT'|'EQUILIBRIUM', position 0..1)."""
        if not state.dealing_range:
            return "EQUILIBRIUM", 0.5
        pos = state.dealing_range.position_of(price)
        buf = self.cfg.premium_discount_buffer
        if pos > 0.5 + buf:
            return "PREMIUM", pos
        if pos < 0.5 - buf:
            return "DISCOUNT", pos
        return "EQUILIBRIUM", pos

    @staticmethod
    def last_event(state: StructureState,
                   kinds: Tuple[StructureEventKind, ...],
                   direction: Optional[Direction] = None,
                   since_index: int = 0) -> Optional[StructureEvent]:
        for ev in reversed(state.events):
            if ev.index < since_index:
                return None
            if ev.kind in kinds and (direction is None or ev.direction == direction):
                return ev
        return None
