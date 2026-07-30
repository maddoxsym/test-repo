"""
Structural stops, structural targets, and honest net reward:risk.

THE PRICE CONVENTION (this is the fix for V4's asymmetric bid/ask handling).
cTrader bars are BID bars, so every level this module produces — stops,
TP1, targets — is a BID price.  The ask is bid + spread.  Costs therefore
land like this:

  LONG    fill  = bid + spread + slippage        (you buy the ask)
          exit  at the bid, so:
              risk   = fill - stop
              reward = target - fill
  SHORT   fill  = bid - slippage                 (you sell the bid)
          exit  at the ask, so the exit costs one extra spread:
              risk   = (stop + spread) - fill
              reward = fill - (target + spread)

Commission is converted to price units and charged for the round turn.  The
same convention is used by the shadow engine, by position sizing and by the
real executor, so a shadow R and a real R mean the same thing.

Stop construction: the stop goes beyond a REAL invalidation level (the swept
extreme, the protected swing, or the far edge of the zone / order block that
produced the setup), plus an ATR buffer and a spread buffer.  It is then
pushed out — never pulled in — to satisfy a minimum volatility distance, so
GOLD M5 trades cannot get unrealistically tight stops.  A stop that ends up
absurdly wide rejects the setup instead of being trimmed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from adaptive_bot.core.models import Direction
from .market_state_v5 import MarketState


@dataclass
class Invalidation:
    """One candidate invalidation level with the reason it invalidates."""
    price: float
    label: str


@dataclass
class StopPlan:
    price: float
    reason: str
    distance: float
    rejected: bool = False
    reject_reason: str = ""
    min_stop_applied: bool = False
    structural_price: float = 0.0
    notes: List[str] = field(default_factory=list)


@dataclass
class TargetPlan:
    tp1: float
    tp1_reason: str
    target: float
    target_reason: str
    tp1_r: float
    target_r: float
    net_rr: float
    blended_rr: float
    rejected: bool = False
    reject_reason: str = ""
    clipped: bool = False
    notes: List[str] = field(default_factory=list)


def commission_price(cfg, mpu: float) -> float:
    """Round-turn commission expressed in PRICE units.

    Spread and slippage are not here: they are already inside the fill price
    and the exit side (see the convention above), so counting them again
    would double-charge them."""
    return 2.0 * cfg.commission_per_unit / mpu if mpu > 0 else 0.0


def planned_fill(direction: Direction, entry_ref: float, spread_price: float,
                 slip_price: float) -> float:
    """Where a market order is expected to fill, in absolute price terms."""
    if direction == Direction.LONG:
        return entry_ref + spread_price + slip_price
    return entry_ref - slip_price


def risk_price(direction: Direction, fill: float, stop: float,
               spread_price: float) -> float:
    """Distance from fill to stop, including the spread a short pays on exit."""
    if direction == Direction.LONG:
        return fill - stop
    return (stop + spread_price) - fill


def reward_price(direction: Direction, fill: float, target: float,
                 spread_price: float) -> float:
    """Distance from fill to target, including the spread a short pays."""
    if direction == Direction.LONG:
        return target - fill
    return fill - (target + spread_price)


def net_rr(direction: Direction, fill: float, stop: float, target: float,
           spread_price: float, comm_price: float) -> float:
    """Reward:risk after spread, slippage (already in `fill`) and commission."""
    risk = risk_price(direction, fill, stop, spread_price)
    if risk <= 0:
        return 0.0
    reward = reward_price(direction, fill, target, spread_price) - comm_price
    return reward / risk


class StopBuilder:

    def __init__(self, cfg):
        self.cfg = cfg

    def build(self, state: MarketState, direction: Direction,
              entry_ref: float, invalidations: Sequence[Invalidation],
              atr: float, point: float) -> StopPlan:
        cfg = self.cfg
        notes: List[str] = []
        if atr <= 0:
            return StopPlan(0.0, "", 0.0, True, "ATR unavailable")
        usable = [iv for iv in invalidations
                  if (iv.price < entry_ref if direction == Direction.LONG
                      else iv.price > entry_ref)]
        if not usable:
            return StopPlan(0.0, "", 0.0, True,
                            "no structural invalidation level on the correct "
                            "side of the entry")
        # The PRIMARY invalidation is the first one the family supplied: the
        # level whose break kills the specific idea being traded.  Taking the
        # furthest level across all timeframes instead would tie an M5
        # pullback entry to an M15 swing tens of dollars away, and the
        # max-ATR sanity check would then silently reject the whole family.
        # Over-tightness is handled by the ATR floor below, not by reaching
        # for a wider level.
        chosen = usable[0]
        alternatives = [f"{iv.label} {iv.price:.2f}" for iv in usable[1:]]
        buffer_price = (cfg.stop_structure_buffer_atr * atr
                        + cfg.stop_spread_buffer_mult * state.spread_price)
        sign = -1.0 if direction == Direction.LONG else 1.0
        structural = chosen.price + sign * buffer_price
        notes.append(f"structural stop beyond {chosen.label} "
                     f"{chosen.price:.2f} with "
                     f"{cfg.stop_structure_buffer_atr:.2f} ATR + "
                     f"{cfg.stop_spread_buffer_mult:.1f}x spread buffer")
        if alternatives:
            notes.append("wider invalidation levels noted but not used: "
                         + ", ".join(alternatives))

        fill = planned_fill(direction, entry_ref, state.spread_price,
                            cfg.slippage_buffer_points * point)
        dist = risk_price(direction, fill, structural, state.spread_price)

        # minimum volatility distance: push the stop FURTHER out, never in
        min_dist = max(cfg.min_stop_atr_frac * atr, cfg.min_stop_points * point)
        min_applied = False
        stop = structural
        if dist < min_dist:
            deficit = min_dist - dist
            stop = structural + sign * deficit
            dist = risk_price(direction, fill, stop, state.spread_price)
            min_applied = True
            notes.append(f"minimum stop distance applied: widened by "
                         f"{deficit:.2f} to {min_dist:.2f} "
                         f"({cfg.min_stop_atr_frac:.2f} x M5 ATR {atr:.2f}, "
                         f"floor {cfg.min_stop_points:.0f} pts) — still beyond "
                         f"structural invalidation")

        max_dist = cfg.max_stop_atr_frac * atr
        if dist > max_dist:
            return StopPlan(stop, chosen.label, dist, True,
                            f"structural stop {dist:.2f} exceeds "
                            f"{cfg.max_stop_atr_frac:.2f} x ATR "
                            f"({max_dist:.2f}) — setup rejected rather than "
                            f"tightened", min_applied, structural, notes)
        reason = chosen.label + (" + min-ATR floor" if min_applied else "")
        return StopPlan(price=stop, reason=reason, distance=dist,
                        min_stop_applied=min_applied,
                        structural_price=structural, notes=notes)


class TargetBuilder:
    """Structure-based targets. No fixed 2.5R is imposed on anything."""

    def __init__(self, cfg):
        self.cfg = cfg

    # ------------------------------------------------------------- candidates
    def candidates(self, state: MarketState, direction: Direction,
                   fill: float) -> List[Tuple[float, str]]:
        """Real structural levels in the trade direction, nearest first."""
        out: List[Tuple[float, str]] = []
        for pool in state.untouched_pools(direction, fill):
            out.append((pool.price, f"{pool.kind.value} liquidity"))
        # previous day extremes
        if direction == Direction.LONG and state.prev_day_high:
            out.append((state.prev_day_high, "previous day high"))
        if direction == Direction.SHORT and state.prev_day_low:
            out.append((state.prev_day_low, "previous day low"))
        # session extremes
        for key, price in state.session_marks.items():
            if price is None:
                continue
            if direction == Direction.LONG and key.endswith("_high") \
                    and price > fill:
                out.append((price, f"{key.replace('_', ' ')}"))
            if direction == Direction.SHORT and key.endswith("_low") \
                    and price < fill:
                out.append((price, f"{key.replace('_', ' ')}"))
        # higher-timeframe imbalance (unfilled FVG) and zone edges
        for tf_feat in (state.h1, state.m30):
            if tf_feat is None:
                continue
            for g in tf_feat.fvgs:
                edge = g.lower if direction == Direction.LONG else g.upper
                if (edge > fill) if direction == Direction.LONG \
                        else (edge < fill):
                    out.append((edge,
                                f"{g.timeframe.value} imbalance edge"))
        # confirmed swing extremes on M15
        if state.m15 is not None:
            for s in state.m15.structure.swings[-8:]:
                if direction == Direction.LONG and s.price > fill:
                    out.append((s.price, "M15 confirmed swing high"))
                if direction == Direction.SHORT and s.price < fill:
                    out.append((s.price, "M15 confirmed swing low"))
        # dedupe near-identical levels, keep the first (nearest) label
        out.sort(key=lambda t: (t[0] - fill) if direction == Direction.LONG
                 else (fill - t[0]))
        deduped: List[Tuple[float, str]] = []
        tol = max(state.atr_m5 * 0.10, state.point * 5)
        for price, label in out:
            if (price - fill) * direction.sign <= 0:
                continue
            if any(abs(price - p) <= tol for p, _ in deduped):
                continue
            deduped.append((price, label))
        return deduped

    # ----------------------------------------------------------------- build
    def build(self, state: MarketState, direction: Direction,
              entry_ref: float, stop: float, atr: float, point: float,
              mpu: float, family: str) -> TargetPlan:
        cfg = self.cfg
        notes: List[str] = []
        fill = planned_fill(direction, entry_ref, state.spread_price,
                            cfg.slippage_buffer_points * point)
        risk = risk_price(direction, fill, stop, state.spread_price)
        if risk <= 0:
            return TargetPlan(0, "", 0, "", 0, 0, 0, 0, True,
                              "non-positive risk distance")
        comm = commission_price(cfg, mpu)
        sign = direction.sign

        # major opposing structure that would block a target
        blocker = state.opposing_htf_zone(direction, fill)
        blocker_edge: Optional[float] = None
        if blocker is not None:
            blocker_edge = blocker.lower if direction == Direction.LONG \
                else blocker.upper
            clearance = cfg.target_block_buffer_atr * atr
            blocker_edge = blocker_edge - sign * clearance

        cands = self.candidates(state, direction, fill)
        chosen: Optional[Tuple[float, str]] = None
        clipped = False
        for price, label in cands:
            if blocker_edge is not None and (price - blocker_edge) * sign > 0:
                # this level sits beyond a blocking HTF zone
                continue
            rr = net_rr(direction, fill, stop, price, state.spread_price, comm)
            if rr >= cfg.min_net_rr:
                chosen = (price, label)
                break
        if chosen is None and blocker_edge is not None:
            rr_block = net_rr(direction, fill, stop, blocker_edge,
                              state.spread_price, comm)
            if rr_block >= cfg.min_net_rr:
                chosen = (blocker_edge,
                          f"clipped to {blocker.timeframe.value} "
                          f"{blocker.kind.value} zone")
                clipped = True
                notes.append("target clipped short of the opposing "
                             "higher-timeframe zone")
        if chosen is None:
            # no structural target gives the required reward:risk
            needed = fill + sign * (cfg.min_net_rr * risk + comm)
            if blocker_edge is not None and (needed - blocker_edge) * sign > 0:
                reason = (f"minimum {cfg.min_net_rr:.1f}R target "
                          f"{needed:.2f} is blocked by the "
                          f"{blocker.timeframe.value} {blocker.kind.value} "
                          f"zone at {blocker.lower:.2f}-{blocker.upper:.2f}")
            else:
                near = cands[0] if cands else None
                reason = (f"nearest structural target "
                          f"{near[0]:.2f} ({near[1]}) is only "
                          f"{net_rr(direction, fill, stop, near[0], state.spread_price, comm):.2f}R "
                          f"net, below the {cfg.min_net_rr:.1f}R floor"
                          if near else
                          "no structural target found in the trade direction")
            return TargetPlan(0, "", 0, "", 0, 0, 0, 0, True, reason,
                              notes=notes)

        target, target_label = chosen
        target_r = net_rr(direction, fill, stop, target, state.spread_price,
                          comm)
        if target_r > cfg.allow_runner_beyond_rr:
            notes.append(f"structural room is {target_r:.2f}R — kept as a "
                         f"runner target, not trimmed to a fixed R")

        # ---- TP1: first nearby liquidity/structure, else an R level --------
        tp1_lo = fill + sign * (cfg.tp1_min_r * risk + comm)
        tp1_hi = fill + sign * (cfg.tp1_max_r * risk + comm)
        tp1: Optional[float] = None
        tp1_label = ""
        if cfg.tp1_mode == "STRUCTURE_OR_R":
            for price, label in cands:
                if (price - tp1_lo) * sign >= 0 and (tp1_hi - price) * sign >= 0 \
                        and (target - price) * sign > 0:
                    tp1, tp1_label = price, f"first {label}"
                    break
        if tp1 is None:
            mid_r = 0.5 * (cfg.tp1_min_r + cfg.tp1_max_r)
            tp1 = fill + sign * (mid_r * risk + comm)
            tp1_label = f"{mid_r:.2f}R (no nearby structure in the TP1 band)"
        if (target - tp1) * sign <= 0:
            # TP1 must sit before the main target; if not, drop the partial
            tp1 = fill + sign * (cfg.tp1_min_r * risk + comm)
            tp1_label = f"{cfg.tp1_min_r:.2f}R floor"
            if (target - tp1) * sign <= 0:
                return TargetPlan(0, "", 0, "", 0, 0, 0, 0, True,
                                  "main target is inside the TP1 band — no "
                                  "room for a partial plus a runner",
                                  notes=notes)

        tp1_r = net_rr(direction, fill, stop, tp1, state.spread_price, comm)
        frac = cfg.partial_fraction if cfg.partial_enabled else 0.0
        blended = frac * tp1_r + (1.0 - frac) * target_r
        if blended < cfg.min_blended_rr:
            return TargetPlan(
                tp1, tp1_label, target, target_label, tp1_r, target_r,
                target_r, blended, True,
                f"blended reward:risk {blended:.2f} (TP1 {tp1_r:.2f}R at "
                f"{frac:.0%} + runner {target_r:.2f}R) is below the "
                f"{cfg.min_blended_rr:.2f} floor", clipped, notes)

        return TargetPlan(tp1=tp1, tp1_reason=tp1_label, target=target,
                          target_reason=target_label, tp1_r=tp1_r,
                          target_r=target_r, net_rr=target_r,
                          blended_rr=blended, clipped=clipped, notes=notes)
