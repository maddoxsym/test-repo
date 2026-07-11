"""
Supply & demand zones and order blocks.

Zone = leg-in -> base -> leg-out:
  * base: 1..zone_base_max_candles consecutive candles whose bodies are
    all <= zone_base_body_atr * ATR.
  * leg-out: the first candle after the base shows displacement; its
    direction defines SUPPLY (down) or DEMAND (up).
  * leg-in direction + leg-out direction give RBD/DBR/DBD/RBR.
Boundaries: distal = extreme of the base range, proximal = the base body
edge nearest to the departure. Invalidation = close through distal.
Freshness decays with age and touches; heavily-touched zones score 0.

Order block = the LAST opposite-coloured candle immediately before a
displacement move that (a) produced BOS/CHoCH/MSS or (b) swept liquidity.
Plain opposite candles are NOT order blocks.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from ..core.config import Config
from ..core.helpers import atr_series, is_displacement
from ..core.models import (Candle, Direction, OrderBlock, SweepEvent,
                           Timeframe, Zone, ZoneKind, ZonePattern, new_id)
from .market_structure import StructureState


class SupplyDemandDetector:

    def __init__(self, cfg: Config, timeframe: Timeframe):
        self.cfg = cfg
        self.tf = timeframe

    def detect(self, candles: Sequence[Candle],
               structure: Optional[StructureState] = None) -> List[Zone]:
        cfg = self.cfg
        n = len(candles)
        if n < cfg.atr_period + 6:
            return []
        atr = atr_series(candles, cfg.atr_period)
        zones: List[Zone] = []
        i = cfg.atr_period
        while i < n - 1:
            a = atr[i]
            if a <= 0:
                i += 1
                continue
            # find base start: candle i small-bodied
            if candles[i].body > cfg.zone_base_body_atr * a:
                i += 1
                continue
            base_start = i
            base_end = i
            while (base_end + 1 < n - 1
                   and base_end - base_start + 1 < cfg.zone_base_max_candles
                   and candles[base_end + 1].body <= cfg.zone_base_body_atr * atr[base_end + 1]):
                base_end += 1
            leg_out_idx = base_end + 1
            if leg_out_idx >= n:
                break
            out_c = candles[leg_out_idx]
            a_out = atr[leg_out_idx]
            if not is_displacement(out_c, a_out, cfg.displacement_atr_mult,
                                   cfg.displacement_body_ratio):
                i = base_end + 1
                continue
            # leg-in direction: net move of the 3 candles before the base
            pre = candles[max(0, base_start - 3):base_start]
            leg_in_up = bool(pre) and pre[-1].close >= pre[0].open
            leg_out_up = out_c.bullish
            base = candles[base_start:base_end + 1]
            base_high = max(c.high for c in base)
            base_low = min(c.low for c in base)
            body_high = max(max(c.open, c.close) for c in base)
            body_low = min(min(c.open, c.close) for c in base)
            if leg_out_up:
                kind = ZoneKind.DEMAND
                pattern = ZonePattern.DBR if not leg_in_up else ZonePattern.RBR
                upper, lower = body_high, base_low
            else:
                kind = ZoneKind.SUPPLY
                pattern = ZonePattern.RBD if leg_in_up else ZonePattern.DBD
                upper, lower = base_high, body_low
            disp_score = min(1.0, out_c.body / (2.0 * a_out)) if a_out > 0 else 0.0
            zones.append(Zone(zone_id=new_id("zone"), kind=kind, pattern=pattern,
                              upper=upper, lower=lower, timeframe=self.tf,
                              created_time=candles[base_start].time,
                              created_index=base_start,
                              displacement_score=disp_score))
            i = leg_out_idx + 1

        self._update_zone_states(zones, candles, structure)
        return zones

    def _update_zone_states(self, zones: List[Zone],
                            candles: Sequence[Candle],
                            structure: Optional[StructureState]) -> None:
        cfg = self.cfg
        n = len(candles)
        bos_indices = ([ev.index for ev in structure.events]
                       if structure else [])
        for z in zones:
            # structure break caused shortly after the departure
            z.caused_bos = any(z.created_index < bi <= z.created_index + 8
                               for bi in bos_indices)
            touches = 0
            for j in range(z.created_index + 2, n):
                c = candles[j]
                if z.invalidated:
                    break
                if z.kind == ZoneKind.DEMAND:
                    if c.close < z.lower:
                        z.invalidated = True
                        break
                    if c.low <= z.upper:
                        touches += 1
                else:
                    if c.close > z.upper:
                        z.invalidated = True
                        break
                    if c.high >= z.lower:
                        touches += 1
            z.touches = touches
            age = n - 1 - z.created_index
            age_factor = max(0.0, 1.0 - age / cfg.zone_max_age_candles)
            touch_factor = max(0.0, 1.0 - touches / (cfg.zone_max_touches + 1))
            z.freshness = round(age_factor * touch_factor, 3)
            if touches > cfg.zone_max_touches:
                z.freshness = 0.0

    @staticmethod
    def active_zones(zones: Sequence[Zone], kind: Optional[ZoneKind] = None,
                     min_quality: float = 0.25) -> List[Zone]:
        out = [z for z in zones if not z.invalidated and z.quality() >= min_quality]
        if kind:
            out = [z for z in out if z.kind == kind]
        return out

    @staticmethod
    def zone_at_price(zones: Sequence[Zone], price: float,
                      kind: Optional[ZoneKind] = None,
                      tolerance: float = 0.0) -> Optional[Zone]:
        best: Optional[Zone] = None
        for z in SupplyDemandDetector.active_zones(zones, kind):
            if z.lower - tolerance <= price <= z.upper + tolerance:
                if best is None or z.quality() > best.quality():
                    best = z
        return best


class OrderBlockDetector:

    def __init__(self, cfg: Config, timeframe: Timeframe):
        self.cfg = cfg
        self.tf = timeframe

    def detect(self, candles: Sequence[Candle],
               structure: Optional[StructureState] = None,
               sweeps: Optional[Sequence[SweepEvent]] = None) -> List[OrderBlock]:
        cfg = self.cfg
        n = len(candles)
        if n < cfg.atr_period + 3:
            return []
        atr = atr_series(candles, cfg.atr_period)
        sweep_idx = {sv.index for sv in (sweeps or [])}
        blocks: List[OrderBlock] = []
        for i in range(cfg.atr_period, n):
            c = candles[i]
            a = atr[i]
            if not is_displacement(c, a, cfg.displacement_atr_mult,
                                   cfg.displacement_body_ratio):
                continue
            # structural / liquidity link within a short forward window
            linked_ev = next((ev for ev in (structure.events if structure else [])
                              if i <= ev.index <= i + 3), None)
            linked_sweep = any(i - 2 <= si <= i + 1 for si in sweep_idx)
            if cfg.ob_require_structure_link and not linked_ev and not linked_sweep:
                continue
            # walk back for the last opposite-coloured candle
            ob_idx = None
            for k in range(i - 1, max(0, i - 4) - 1, -1):
                if c.bullish and candles[k].bearish:
                    ob_idx = k
                    break
                if c.bearish and candles[k].bullish:
                    ob_idx = k
                    break
            if ob_idx is None:
                continue
            ob_c = candles[ob_idx]
            direction = Direction.LONG if c.bullish else Direction.SHORT
            blocks.append(OrderBlock(
                ob_id=new_id("ob"), direction=direction,
                upper=ob_c.high, lower=ob_c.low, timeframe=self.tf,
                created_time=ob_c.time, created_index=ob_idx,
                linked_structure=linked_ev.kind if linked_ev else None,
                linked_sweep=linked_sweep))
        # deduplicate by index (keep the last classification)
        seen: Dict[int, OrderBlock] = {}
        for b in blocks:
            seen[b.created_index] = b
        blocks = sorted(seen.values(), key=lambda b: b.created_index)
        self._update_states(blocks, candles)
        return blocks

    @staticmethod
    def _update_states(blocks: List[OrderBlock],
                       candles: Sequence[Candle]) -> None:
        n = len(candles)
        for b in blocks:
            mitigations = 0
            for j in range(b.created_index + 2, n):
                c = candles[j]
                if b.direction == Direction.LONG:
                    if c.close < b.lower:
                        b.invalidated = True
                        break
                    if c.low <= b.upper:
                        mitigations += 1
                else:
                    if c.close > b.upper:
                        b.invalidated = True
                        break
                    if c.high >= b.lower:
                        mitigations += 1
            b.mitigations = mitigations
            b.freshness = max(0.0, 1.0 - mitigations / 3.0)
            if b.invalidated:
                b.freshness = 0.0

    @staticmethod
    def usable(blocks: Sequence[OrderBlock],
               direction: Optional[Direction] = None,
               min_freshness: float = 0.3) -> List[OrderBlock]:
        out = [b for b in blocks if not b.invalidated and b.freshness >= min_freshness]
        if direction:
            out = [b for b in out if b.direction == direction]
        return out
