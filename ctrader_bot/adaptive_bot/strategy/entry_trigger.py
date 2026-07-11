"""
1-minute entry trigger — the final gate before execution.

The M1 timeframe can NEVER create a trade by itself.  A setup must already
be fully confirmed on M15 (bias) and M5 (decision); this module then looks
at the last few completed M1 candles for precise entry evidence:

  * micro change of character / break of structure in the trade direction
  * a fresh liquidity sweep against the trade direction (stop-hunt fuel)
  * a rejection candle (dominant wick against the move, close in direction)
  * a fair-value-gap retest that held
  * an order-block mitigation that held
  * a strong displacement close in the trade direction
  * break-and-retest of the M5 confirmation level

If cfg.require_m1_trigger is False the gate always passes (logged as such);
that is the closest equivalent of the legacy MARKET_ON_CONFIRM entry mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..core.config import Config
from ..core.helpers import is_displacement
from ..core.models import Direction, StructureEventKind

# imported for typing only (TFAnalysis lives in strategy_engine)
# a structural import cycle is avoided by duck-typing the tfa argument.


@dataclass
class TriggerResult:
    fired: bool
    kind: str
    note: str


class M1TriggerDetector:

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def check(self, m1_tfa, direction: Direction,
              confirmation_level: Optional[float] = None) -> TriggerResult:
        """m1_tfa: TFAnalysis of the M1 series (completed candles only)."""
        cfg = self.cfg
        if not cfg.require_m1_trigger:
            return TriggerResult(True, "DISABLED",
                                 "M1 trigger gate disabled in config")
        if m1_tfa is None or len(m1_tfa.candles) < cfg.atr_period + 5:
            return TriggerResult(False, "NO_DATA",
                                 "not enough completed M1 candles")
        n = len(m1_tfa.candles)
        window = cfg.m1_trigger_max_age
        last = m1_tfa.candles[-1]
        atr = m1_tfa.atr_now

        # 1. micro CHoCH / BOS / MSS in direction, recent
        for ev in reversed(m1_tfa.structure.events):
            if ev.index < n - window:
                break
            if ev.direction == direction and ev.kind in (
                    StructureEventKind.CHOCH, StructureEventKind.MSS,
                    StructureEventKind.BOS):
                return TriggerResult(True, f"M1_{ev.kind.value}",
                                     f"micro {ev.kind.value} at "
                                     f"{ev.broken_level:.2f}")

        # 2. fresh opposing-side liquidity sweep (sell-side for longs)
        for sv in reversed(m1_tfa.sweeps):
            if sv.index < n - window:
                break
            if sv.valid and sv.level.buy_side == (direction == Direction.SHORT):
                return TriggerResult(True, "M1_SWEEP",
                                     f"swept {sv.level.kind.value} at "
                                     f"{sv.level.price:.2f} and reclaimed")

        # 3. rejection candle: dominant wick against direction, close with it
        if last.range > 0:
            if direction == Direction.LONG and \
                    last.lower_wick >= 0.5 * last.range and \
                    last.close >= last.open:
                return TriggerResult(True, "M1_REJECTION",
                                     f"bullish rejection wick "
                                     f"{last.lower_wick:.2f}")
            if direction == Direction.SHORT and \
                    last.upper_wick >= 0.5 * last.range and \
                    last.close <= last.open:
                return TriggerResult(True, "M1_REJECTION",
                                     f"bearish rejection wick "
                                     f"{last.upper_wick:.2f}")

        # 4. FVG retest that held: last candle tapped a usable M1 FVG in
        #    direction and closed back in the trade direction
        for g in m1_tfa.fvgs:
            if g.direction != direction or g.state.value == "MITIGATED":
                continue
            tapped = last.low <= g.upper and last.high >= g.lower
            held = (last.close > g.upper if direction == Direction.LONG
                    else last.close < g.lower)
            if tapped and held:
                return TriggerResult(True, "M1_FVG_RETEST",
                                     f"FVG {g.lower:.2f}-{g.upper:.2f} "
                                     f"retested and held")

        # 5. order-block mitigation that held
        for b in m1_tfa.order_blocks:
            if b.direction != direction or b.invalidated:
                continue
            tapped = last.low <= b.upper and last.high >= b.lower
            held = (last.close > b.upper if direction == Direction.LONG
                    else last.close < b.lower)
            if tapped and held:
                return TriggerResult(True, "M1_OB_MITIGATION",
                                     f"OB {b.lower:.2f}-{b.upper:.2f} "
                                     f"mitigated and held")

        # 6. strong displacement close in direction
        if atr > 0 and is_displacement(last, atr, cfg.displacement_atr_mult,
                                       cfg.displacement_body_ratio):
            if (direction == Direction.LONG and last.bullish) or \
                    (direction == Direction.SHORT and last.bearish):
                return TriggerResult(True, "M1_DISPLACEMENT",
                                     f"displacement close, body "
                                     f"{last.body:.2f} vs ATR {atr:.2f}")

        # 7. break-and-retest of the M5 confirmation level
        if confirmation_level is not None and atr > 0:
            tol = 0.35 * atr
            recent = m1_tfa.candles[-window:]
            touched = any(
                (c.low <= confirmation_level + tol if direction == Direction.LONG
                 else c.high >= confirmation_level - tol)
                for c in recent)
            held = (last.close > confirmation_level if direction == Direction.LONG
                    else last.close < confirmation_level)
            closed_with = last.bullish if direction == Direction.LONG else last.bearish
            if touched and held and closed_with:
                return TriggerResult(True, "M1_BREAK_RETEST",
                                     f"retest of {confirmation_level:.2f} held")

        return TriggerResult(False, "NONE",
                             "no M1 trigger within the last "
                             f"{window} completed M1 candles")
