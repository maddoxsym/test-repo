"""
Autonomous strategy space.

A strategy is a CONFIGURATION, not code: an archetype (one of eight exact
rule templates below) plus a timeframe plus bounded numeric parameters,
management rules, a regime whitelist and a session whitelist.  Every
strategy therefore has exact, reproducible rules for entry, stop, target,
invalidation, regime and management, and is stored with a unique id and
version.  The learning system may create bounded parameter variants
("mutations") of successful strategies and retire failing ones — it can
never invent rules outside these templates and never touches source code.

Archetypes (long and short are symmetric):
  TREND_PULLBACK   trend (structure + EMA100) + pullback to EMA + resume
  DONCHIAN_BREAK   N-bar channel breakout + range expansion + volume
  SWEEP_REVERSAL   liquidity sweep + reclaim (+ optional displacement)
  RANGE_FADE       dealing-range edge + RSI extreme + rejection candle
  MOMENTUM_CONT    consecutive displacement + micro-pause continuation
  FVG_RETEST       fresh fair-value-gap retest that holds, with trend
  SESSION_OPEN     London/NY open range break with displacement
  SD_ZONE          fresh supply/demand zone reaction with rejection close

Every signal must clear the net reward-to-risk floor (>= 2.0R after
spread/slippage/commission) and targets blocked by major opposing
structure are clipped or rejected.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from adaptive_bot.core.models import Direction, Regime, ZoneKind
from adaptive_bot.strategy.liquidity import LiquidityDetector
from adaptive_bot.strategy.supply_demand import SupplyDemandDetector
from .features import TFFeatures, highest, lowest

TREND_REGIMES = (Regime.STRONG_BULL.value, Regime.WEAK_BULL.value,
                 Regime.STRONG_BEAR.value, Regime.WEAK_BEAR.value)
ALL_TRADEABLE = TREND_REGIMES + (Regime.RANGE.value, Regime.EXPANSION.value,
                                 Regime.COMPRESSION.value,
                                 Regime.REVERSAL_ATTEMPT.value)

MGMT_MODES = ("FULL_TP", "BE_1R", "PARTIAL_RUNNER", "ATR_TRAIL",
              "STRUCT_TRAIL")

TARGET_MODES = ("RR", "LIQUIDITY", "ATR")


@dataclass
class StrategyConfig:
    sid: str
    archetype: str
    version: int
    tf: str                                   # "M5" | "M15" | "M30" | "H1"
    params: Dict[str, float]
    regimes: Tuple[str, ...]
    sessions: Tuple[str, ...]                 # ("ANY",) or session names
    mgmt: Dict[str, float]                    # mode index + thresholds
    status: str = "active"                    # active | benched | retired
    bench_until: str = ""                     # ISO date
    parent: str = ""
    mutations: int = 0
    created: str = ""

    @property
    def mgmt_mode(self) -> str:
        return MGMT_MODES[int(self.mgmt.get("mode", 0)) % len(MGMT_MODES)]

    def to_dict(self) -> dict:
        return {"sid": self.sid, "archetype": self.archetype,
                "version": self.version, "tf": self.tf,
                "params": dict(self.params), "regimes": list(self.regimes),
                "sessions": list(self.sessions), "mgmt": dict(self.mgmt),
                "status": self.status, "bench_until": self.bench_until,
                "parent": self.parent, "mutations": self.mutations,
                "created": self.created}

    @staticmethod
    def from_dict(d: dict) -> "StrategyConfig":
        return StrategyConfig(
            sid=d["sid"], archetype=d["archetype"], version=d["version"],
            tf=d["tf"], params=dict(d["params"]),
            regimes=tuple(d["regimes"]), sessions=tuple(d["sessions"]),
            mgmt=dict(d["mgmt"]), status=d.get("status", "active"),
            bench_until=d.get("bench_until", ""), parent=d.get("parent", ""),
            mutations=d.get("mutations", 0), created=d.get("created", ""))


@dataclass
class Signal:
    strategy_id: str
    tf: str
    direction: Direction
    entry_ref: float                 # decision-bar close (fills are later)
    stop: float
    target: float
    reason: str
    confluence: int
    created: datetime
    invalidation: str = "close beyond stop level before entry"

    def rr(self, entry: Optional[float] = None) -> float:
        e = entry if entry is not None else self.entry_ref
        risk = abs(e - self.stop)
        return abs(self.target - e) / risk if risk > 0 else 0.0


@dataclass
class EvalContext:
    regime: str
    session: str
    spread_points: float
    point: float
    now: datetime
    cost: float                      # price-units round-trip cost estimate
    min_net_rr: float
    asian_range: Optional[Tuple[float, float]] = None
    minutes_into_london: Optional[int] = None
    minutes_into_ny: Optional[int] = None


# ---------------------------------------------------------------------------
# parameter bounds (mutations may never leave these boxes)
# ---------------------------------------------------------------------------

PARAM_BOUNDS: Dict[str, Dict[str, Tuple[float, float]]] = {
    "TREND_PULLBACK": {"ema_sel": (0, 1), "swing_lookback": (5, 14),
                       "buffer_atr": (0.2, 0.6), "rr": (2.0, 3.5),
                       "target_mode": (0, 2), "atr_target": (2.5, 4.5),
                       "rsi_floor": (45, 55)},
    "DONCHIAN_BREAK": {"ch_len": (20, 55), "exp_mult": (1.1, 1.7),
                       "vol_mult": (1.0, 2.0), "stop_atr": (1.2, 2.0),
                       "rr": (2.0, 3.5), "target_mode": (0, 2),
                       "atr_target": (2.0, 4.0)},
    "SWEEP_REVERSAL": {"recency": (3, 10), "disp_req": (0, 1),
                       "buffer_atr": (0.2, 0.6), "rr": (2.0, 3.0),
                       "target_mode": (0, 2), "atr_target": (2.0, 3.5)},
    "RANGE_FADE": {"edge_frac": (0.10, 0.25), "rsi_os": (25, 35),
                   "wick_frac": (0.40, 0.60), "buffer_atr": (0.2, 0.6),
                   "target_sel": (0, 1), "rr": (2.0, 3.0)},
    "MOMENTUM_CONT": {"mom_count": (2, 3), "pause_frac": (0.4, 0.7),
                      "stop_atr": (1.0, 1.8), "atr_target": (2.5, 4.5),
                      "rr": (2.0, 3.5), "target_mode": (0, 2)},
    "FVG_RETEST": {"recency": (4, 15), "disp_req": (0, 1),
                   "buffer_atr": (0.2, 0.6), "rr": (2.0, 3.5),
                   "target_mode": (0, 2), "atr_target": (2.0, 4.0)},
    "SESSION_OPEN": {"open_window": (15, 60), "range_mult": (1.0, 2.0),
                     "stop_sel": (0, 1), "rr": (2.0, 3.0),
                     "pre_range_bars": (12, 36), "target_mode": (0, 2),
                     "atr_target": (2.0, 3.5)},
    "SD_ZONE": {"min_quality": (0.40, 0.70), "buffer_atr": (0.2, 0.6),
                "trend_req": (0, 1), "rr": (2.0, 3.5),
                "target_mode": (0, 2), "atr_target": (2.0, 4.0)},
}

MGMT_BOUNDS = {"mode": (0, len(MGMT_MODES) - 1), "be_r": (0.8, 2.0),
               "partial_r": (1.5, 2.0), "partial_frac": (0.4, 0.6),
               "trail_atr": (1.5, 2.5)}


def _clamp(name_bounds, key, value):
    lo, hi = name_bounds[key]
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# shared building blocks
# ---------------------------------------------------------------------------

def _swing_stop(direction: Direction, f: TFFeatures, lookback: int,
                buffer_atr: float) -> float:
    if direction == Direction.LONG:
        return lowest(f.candles, lookback, exclude_last=False) \
            - buffer_atr * f.atr_now
    return highest(f.candles, lookback, exclude_last=False) \
        + buffer_atr * f.atr_now


def _blocking_zone_edge(direction: Direction, f: TFFeatures,
                        entry: float) -> Optional[float]:
    """Nearest strong opposing zone edge in the trade direction."""
    kind = ZoneKind.SUPPLY if direction == Direction.LONG else ZoneKind.DEMAND
    best = None
    for z in SupplyDemandDetector.active_zones(f.zones, kind,
                                               min_quality=0.45):
        edge = z.lower if direction == Direction.LONG else z.upper
        if direction == Direction.LONG and edge > entry:
            best = edge if best is None else min(best, edge)
        if direction == Direction.SHORT and edge < entry:
            best = edge if best is None else max(best, edge)
    return best


def _target(direction: Direction, entry: float, stop: float, f: TFFeatures,
            mode_idx: float, rr: float, atr_mult: float, ctx: EvalContext
            ) -> Optional[Tuple[float, str]]:
    """Target by mode; rejects/clips targets blocked by opposing structure
    and rejects anything below the net-RR floor."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = direction.sign
    mode = TARGET_MODES[int(mode_idx) % len(TARGET_MODES)]
    if mode == "RR":
        tgt, why = entry + sign * rr * risk, f"{rr:.1f}R multiple"
    elif mode == "ATR":
        tgt, why = entry + sign * atr_mult * f.atr_now, \
            f"{atr_mult:.1f}x ATR projection"
    else:
        pools = LiquidityDetector.targets_beyond(f.liquidity, entry,
                                                 direction, count=3)
        tgt = None
        why = ""
        for lvl in pools:
            net = (abs(lvl.price - entry) - ctx.cost) / (risk + ctx.cost)
            if net >= ctx.min_net_rr:
                tgt, why = lvl.price, f"opposing {lvl.kind.value} liquidity"
                break
        if tgt is None:
            return None
    # opposing-structure block: clip to the zone edge; reject if too near
    block = _blocking_zone_edge(direction, f, entry)
    if block is not None and (block - tgt) * sign < 0:
        tgt, why = block, why + " (clipped at opposing zone)"
    net_rr = (abs(tgt - entry) - ctx.cost) / (risk + ctx.cost)
    if net_rr < ctx.min_net_rr:
        return None
    return tgt, why


def _stop_sanity(direction: Direction, entry: float, stop: float,
                 f: TFFeatures) -> bool:
    dist = abs(entry - stop)
    if dist <= 0 or f.atr_now <= 0:
        return False
    return 0.30 * f.atr_now <= dist <= 4.0 * f.atr_now


def _make(scfg: "StrategyConfig", f: TFFeatures, ctx: EvalContext,
          direction: Direction, stop: float, reason: str,
          confluence: int) -> Optional[Signal]:
    entry = f.close
    p = scfg.params
    if not _stop_sanity(direction, entry, stop, f):
        return None
    t = _target(direction, entry, stop, f, p.get("target_mode", 0),
                p.get("rr", 2.5), p.get("atr_target", 3.0), ctx)
    if t is None:
        return None
    target, twhy = t
    return Signal(strategy_id=scfg.sid, tf=scfg.tf, direction=direction,
                  entry_ref=entry, stop=stop, target=target,
                  reason=f"{reason}; target {twhy}",
                  confluence=confluence, created=ctx.now)


# ---------------------------------------------------------------------------
# archetype rules (exact and reproducible; long/short symmetric)
# ---------------------------------------------------------------------------

def _eval_trend_pullback(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    ema_p = f.ema20 if int(p.get("ema_sel", 0)) == 0 else f.ema50
    last, prev = f.last, f.prev
    trend = f.structure.trend.value
    if trend == "BULLISH" and f.close > f.ema100[-1]:
        touched = prev.low <= ema_p[-2] or last.low <= ema_p[-1]
        resumed = last.bullish and last.close > ema_p[-1]
        if touched and resumed and f.rsi14[-1] >= p.get("rsi_floor", 50):
            stop = _swing_stop(Direction.LONG, f,
                               int(p.get("swing_lookback", 8)),
                               p.get("buffer_atr", 0.35))
            return _make(scfg, f, ctx, Direction.LONG, stop,
                         "bull trend pullback to EMA resumed", 3)
    if trend == "BEARISH" and f.close < f.ema100[-1]:
        touched = prev.high >= ema_p[-2] or last.high >= ema_p[-1]
        resumed = last.bearish and last.close < ema_p[-1]
        if touched and resumed and f.rsi14[-1] <= 100 - p.get("rsi_floor", 50):
            stop = _swing_stop(Direction.SHORT, f,
                               int(p.get("swing_lookback", 8)),
                               p.get("buffer_atr", 0.35))
            return _make(scfg, f, ctx, Direction.SHORT, stop,
                         "bear trend pullback to EMA resumed", 3)
    return None


def _eval_donchian_break(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    n = int(p.get("ch_len", 34))
    if len(f.candles) < n + 2 or f.atr_now <= 0:
        return None
    last = f.last
    hi, lo = highest(f.candles, n), lowest(f.candles, n)
    expanded = last.range >= p.get("exp_mult", 1.3) * f.atr_now
    vol_ok = f.vol_ma20 <= 0 or last.volume >= p.get("vol_mult", 1.2) * f.vol_ma20
    if not (expanded and vol_ok):
        return None
    stop_atr = p.get("stop_atr", 1.5)
    if last.close > hi and last.bullish:
        stop = last.close - stop_atr * f.atr_now
        return _make(scfg, f, ctx, Direction.LONG, stop,
                     f"close above {n}-bar high with expansion+volume", 3)
    if last.close < lo and last.bearish:
        stop = last.close + stop_atr * f.atr_now
        return _make(scfg, f, ctx, Direction.SHORT, stop,
                     f"close below {n}-bar low with expansion+volume", 3)
    return None


def _eval_sweep_reversal(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    recency = int(p.get("recency", 6))
    n = len(f.candles)
    for sv in reversed(f.sweeps):
        if sv.index < n - recency:
            break
        if not sv.valid:
            continue
        if int(p.get("disp_req", 0)) == 1 and not sv.displaced_away:
            continue
        if not sv.level.buy_side:            # sell-side swept -> long
            if f.last.close > sv.level.price:
                stop = sv.extreme - p.get("buffer_atr", 0.35) * f.atr_now
                return _make(scfg, f, ctx, Direction.LONG, stop,
                             f"sweep of {sv.level.kind.value} reclaimed",
                             2 + int(sv.displaced_away))
        else:                                 # buy-side swept -> short
            if f.last.close < sv.level.price:
                stop = sv.extreme + p.get("buffer_atr", 0.35) * f.atr_now
                return _make(scfg, f, ctx, Direction.SHORT, stop,
                             f"sweep of {sv.level.kind.value} reclaimed",
                             2 + int(sv.displaced_away))
    return None


def _eval_range_fade(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    rng = f.structure.dealing_range
    if rng is None or f.atr_now <= 0:
        return None
    pos = rng.position_of(f.close)
    edge = p.get("edge_frac", 0.18)
    last = f.last
    if pos <= edge and f.rsi14[-1] <= p.get("rsi_os", 30):
        rejected = last.range > 0 and last.lower_wick >= \
            p.get("wick_frac", 0.5) * last.range and last.close >= last.open
        if rejected:
            stop = rng.low - p.get("buffer_atr", 0.35) * f.atr_now
            tgt_price = rng.low + (0.5 if int(p.get("target_sel", 0)) == 0
                                   else 1.0 - edge) * (rng.high - rng.low)
            risk = abs(f.close - stop)
            net = (abs(tgt_price - f.close) - ctx.cost) / (risk + ctx.cost) \
                if risk > 0 else 0
            if net >= ctx.min_net_rr and _stop_sanity(Direction.LONG,
                                                      f.close, stop, f):
                return Signal(scfg.sid, scfg.tf, Direction.LONG, f.close,
                              stop, tgt_price,
                              "range-low fade: RSI oversold + rejection; "
                              "target range level", 3, ctx.now)
    if pos >= 1.0 - edge and f.rsi14[-1] >= 100 - p.get("rsi_os", 30):
        rejected = last.range > 0 and last.upper_wick >= \
            p.get("wick_frac", 0.5) * last.range and last.close <= last.open
        if rejected:
            stop = rng.high + p.get("buffer_atr", 0.35) * f.atr_now
            tgt_price = rng.high - (0.5 if int(p.get("target_sel", 0)) == 0
                                    else 1.0 - edge) * (rng.high - rng.low)
            risk = abs(f.close - stop)
            net = (abs(f.close - tgt_price) - ctx.cost) / (risk + ctx.cost) \
                if risk > 0 else 0
            if net >= ctx.min_net_rr and _stop_sanity(Direction.SHORT,
                                                      f.close, stop, f):
                return Signal(scfg.sid, scfg.tf, Direction.SHORT, f.close,
                              stop, tgt_price,
                              "range-high fade: RSI overbought + rejection; "
                              "target range level", 3, ctx.now)
    return None


def _eval_momentum_cont(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    need = int(p.get("mom_count", 2))
    if len(f.candles) < need + 2 or f.atr_now <= 0:
        return None
    last = f.last
    if last.body > p.get("pause_frac", 0.5) * f.atr_now:
        return None                       # need the micro-pause candle
    push = f.candles[-(need + 1):-1]
    disp = [c for c in push
            if c.body >= 1.0 * f.atr_now and c.body_ratio >= 0.5]
    if len(disp) < need:
        return None
    bullish = all(c.bullish for c in disp)
    bearish = all(c.bearish for c in disp)
    stop_atr = p.get("stop_atr", 1.3)
    if bullish:
        stop = min(last.low, f.close - stop_atr * f.atr_now)
        return _make(scfg, f, ctx, Direction.LONG, stop,
                     f"{need} displacement candles up + pause", 2)
    if bearish:
        stop = max(last.high, f.close + stop_atr * f.atr_now)
        return _make(scfg, f, ctx, Direction.SHORT, stop,
                     f"{need} displacement candles down + pause", 2)
    return None


def _eval_fvg_retest(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    recency = int(p.get("recency", 8))
    n = len(f.candles)
    last = f.last
    for g in reversed(f.fvgs):
        if g.created_index < n - recency:
            break
        if g.state.value == "MITIGATED":
            continue
        if int(p.get("disp_req", 1)) == 1 and not g.from_displacement:
            continue
        if g.direction == Direction.LONG and f.close > f.ema50[-1]:
            tapped = last.low <= g.upper
            held = last.close > g.upper and last.bullish
            if tapped and held:
                stop = g.lower - p.get("buffer_atr", 0.35) * f.atr_now
                return _make(scfg, f, ctx, Direction.LONG, stop,
                             "bullish FVG retested and held", 3)
        if g.direction == Direction.SHORT and f.close < f.ema50[-1]:
            tapped = last.high >= g.lower
            held = last.close < g.lower and last.bearish
            if tapped and held:
                stop = g.upper + p.get("buffer_atr", 0.35) * f.atr_now
                return _make(scfg, f, ctx, Direction.SHORT, stop,
                             "bearish FVG retested and held", 3)
    return None


def _eval_session_open(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    window = int(p.get("open_window", 40))
    is_london = "LONDON" in scfg.sessions
    minutes = ctx.minutes_into_london if is_london else ctx.minutes_into_ny
    if minutes is None or not (0 <= minutes <= window):
        return None
    if is_london and ctx.asian_range is not None:
        lo, hi = ctx.asian_range
    else:
        bars = int(p.get("pre_range_bars", 24))
        hi, lo = highest(f.candles, bars), lowest(f.candles, bars)
    if not (hi > lo > 0):
        return None
    last = f.last
    disp = last.body >= 1.0 * f.atr_now and last.body_ratio >= 0.5
    rng_size = hi - lo
    if not disp or rng_size <= 0:
        return None
    stop_sel = int(p.get("stop_sel", 0))
    if last.close > hi and last.bullish:
        stop = (lo if stop_sel == 0 else (hi + lo) / 2.0) \
            - 0.15 * f.atr_now
        return _make(scfg, f, ctx, Direction.LONG, stop,
                     f"session-open break above pre-range ({rng_size:.2f})", 2)
    if last.close < lo and last.bearish:
        stop = (hi if stop_sel == 0 else (hi + lo) / 2.0) \
            + 0.15 * f.atr_now
        return _make(scfg, f, ctx, Direction.SHORT, stop,
                     f"session-open break below pre-range ({rng_size:.2f})", 2)
    return None


def _eval_sd_zone(scfg, f: TFFeatures, ctx) -> Optional[Signal]:
    p = scfg.params
    q = p.get("min_quality", 0.5)
    last = f.last
    trend = f.structure.trend.value
    for z in SupplyDemandDetector.active_zones(f.zones, None, min_quality=q):
        if z.kind == ZoneKind.DEMAND:
            if int(p.get("trend_req", 0)) == 1 and trend == "BEARISH":
                continue
            tapped = last.low <= z.upper and last.high >= z.lower
            held = last.close > z.upper and last.bullish
            if tapped and held:
                stop = z.lower - p.get("buffer_atr", 0.35) * f.atr_now
                return _make(scfg, f, ctx, Direction.LONG, stop,
                             f"demand zone {z.pattern.value} reaction "
                             f"(fresh {z.freshness:.2f})", 3)
        else:
            if int(p.get("trend_req", 0)) == 1 and trend == "BULLISH":
                continue
            tapped = last.high >= z.lower and last.low <= z.upper
            held = last.close < z.lower and last.bearish
            if tapped and held:
                stop = z.upper + p.get("buffer_atr", 0.35) * f.atr_now
                return _make(scfg, f, ctx, Direction.SHORT, stop,
                             f"supply zone {z.pattern.value} reaction "
                             f"(fresh {z.freshness:.2f})", 3)
    return None


ARCHETYPE_EVAL = {
    "TREND_PULLBACK": _eval_trend_pullback,
    "DONCHIAN_BREAK": _eval_donchian_break,
    "SWEEP_REVERSAL": _eval_sweep_reversal,
    "RANGE_FADE": _eval_range_fade,
    "MOMENTUM_CONT": _eval_momentum_cont,
    "FVG_RETEST": _eval_fvg_retest,
    "SESSION_OPEN": _eval_session_open,
    "SD_ZONE": _eval_sd_zone,
}

ARCHETYPE_REGIMES = {
    "TREND_PULLBACK": TREND_REGIMES + (Regime.EXPANSION.value,),
    "DONCHIAN_BREAK": (Regime.STRONG_BULL.value, Regime.STRONG_BEAR.value,
                       Regime.EXPANSION.value, Regime.COMPRESSION.value),
    "SWEEP_REVERSAL": (Regime.RANGE.value, Regime.REVERSAL_ATTEMPT.value,
                       Regime.WEAK_BULL.value, Regime.WEAK_BEAR.value),
    "RANGE_FADE": (Regime.RANGE.value, Regime.COMPRESSION.value),
    "MOMENTUM_CONT": (Regime.STRONG_BULL.value, Regime.STRONG_BEAR.value,
                      Regime.EXPANSION.value),
    "FVG_RETEST": TREND_REGIMES + (Regime.EXPANSION.value,
                                   Regime.REVERSAL_ATTEMPT.value),
    "SESSION_OPEN": ALL_TRADEABLE,
    "SD_ZONE": TREND_REGIMES + (Regime.RANGE.value,),
}


def evaluate_strategy(scfg: StrategyConfig, f: TFFeatures,
                      ctx: EvalContext) -> Optional[Signal]:
    """Run one strategy's exact rules against fresh features.  Regime and
    session whitelists are enforced here; the caller enforces global
    safety gates (news/spread/locks) separately."""
    if scfg.status != "active":
        return None
    if ctx.regime not in scfg.regimes:
        return None
    if "ANY" not in scfg.sessions and ctx.session not in scfg.sessions:
        return None
    fn = ARCHETYPE_EVAL.get(scfg.archetype)
    if fn is None or f is None:
        return None
    return fn(scfg, f, ctx)


# ---------------------------------------------------------------------------
# population: deterministic seeding + bounded mutation
# ---------------------------------------------------------------------------

def _mgmt(mode_idx: int) -> Dict[str, float]:
    return {"mode": float(mode_idx % len(MGMT_MODES)), "be_r": 1.2,
            "partial_r": 1.8, "partial_frac": 0.5, "trail_atr": 2.0}


def seed_population(created_iso: str) -> List[StrategyConfig]:
    """Deterministic, diverse initial population across archetypes,
    timeframes, target modes and management styles."""
    seeds: List[StrategyConfig] = []
    k = 0

    def add(arch, tf, params, sessions=("ANY",), mgmt_idx=None):
        nonlocal k
        k += 1
        m = _mgmt(mgmt_idx if mgmt_idx is not None else k)
        seeds.append(StrategyConfig(
            sid=f"{arch}-{tf}-{k:02d}", archetype=arch, version=1, tf=tf,
            params=params, regimes=ARCHETYPE_REGIMES[arch],
            sessions=sessions, mgmt=m, created=created_iso))

    for tf in ("M5", "M15", "M30"):
        add("TREND_PULLBACK", tf,
            {"ema_sel": 0, "swing_lookback": 8, "buffer_atr": 0.35,
             "rr": 2.5, "target_mode": 0, "atr_target": 3.0,
             "rsi_floor": 50})
    add("TREND_PULLBACK", "M15",
        {"ema_sel": 1, "swing_lookback": 12, "buffer_atr": 0.45,
         "rr": 3.0, "target_mode": 1, "atr_target": 3.5, "rsi_floor": 48})
    for tf in ("M5", "M15", "H1"):
        add("DONCHIAN_BREAK", tf,
            {"ch_len": 34, "exp_mult": 1.3, "vol_mult": 1.2,
             "stop_atr": 1.5, "rr": 2.5, "target_mode": 2,
             "atr_target": 3.0})
    add("DONCHIAN_BREAK", "M15",
        {"ch_len": 55, "exp_mult": 1.2, "vol_mult": 1.0, "stop_atr": 1.8,
         "rr": 3.0, "target_mode": 0, "atr_target": 3.5})
    for tf in ("M5", "M15"):
        add("SWEEP_REVERSAL", tf,
            {"recency": 6, "disp_req": 0, "buffer_atr": 0.35, "rr": 2.5,
             "target_mode": 1, "atr_target": 2.5})
    add("SWEEP_REVERSAL", "M15",
        {"recency": 8, "disp_req": 1, "buffer_atr": 0.45, "rr": 2.0,
         "target_mode": 0, "atr_target": 3.0})
    for tf in ("M5", "M15"):
        add("RANGE_FADE", tf,
            {"edge_frac": 0.18, "rsi_os": 30, "wick_frac": 0.5,
             "buffer_atr": 0.35, "target_sel": 0, "rr": 2.0,
             "target_mode": 0, "atr_target": 2.5})
    for tf in ("M5", "M15"):
        add("MOMENTUM_CONT", tf,
            {"mom_count": 2, "pause_frac": 0.5, "stop_atr": 1.3,
             "atr_target": 3.0, "rr": 2.5, "target_mode": 2})
    for tf in ("M5", "M15"):
        add("FVG_RETEST", tf,
            {"recency": 8, "disp_req": 1, "buffer_atr": 0.35, "rr": 2.5,
             "target_mode": 0, "atr_target": 3.0})
    add("SESSION_OPEN", "M5",
        {"open_window": 40, "range_mult": 1.5, "stop_sel": 0, "rr": 2.0,
         "pre_range_bars": 24, "target_mode": 0, "atr_target": 2.5},
        sessions=("LONDON", "OVERLAP"))
    add("SESSION_OPEN", "M5",
        {"open_window": 40, "range_mult": 1.5, "stop_sel": 1, "rr": 2.0,
         "pre_range_bars": 24, "target_mode": 2, "atr_target": 2.5},
        sessions=("NEW_YORK", "OVERLAP"))
    for tf in ("M15", "M30"):
        add("SD_ZONE", tf,
            {"min_quality": 0.5, "buffer_atr": 0.35, "trend_req": 1,
             "rr": 2.5, "target_mode": 1, "atr_target": 3.0})
    return seeds


def mutate_strategy(parent: StrategyConfig, rng: random.Random,
                    serial: int, created_iso: str) -> StrategyConfig:
    """Bounded variant: perturb 1-2 numeric parameters (and occasionally
    the management style) inside PARAM_BOUNDS.  Never changes the
    archetype rules themselves."""
    bounds = PARAM_BOUNDS[parent.archetype]
    params = dict(parent.params)
    keys = [key for key in params if key in bounds]
    rng.shuffle(keys)
    for key in keys[:rng.randint(1, 2)]:
        lo, hi = bounds[key]
        span = hi - lo
        params[key] = _clamp(bounds, key,
                             params[key] + rng.uniform(-0.25, 0.25) * span)
        if float(params[key]).is_integer() or key in ("ch_len", "recency",
                                                      "swing_lookback",
                                                      "mom_count",
                                                      "pre_range_bars",
                                                      "open_window",
                                                      "rsi_os", "rsi_floor"):
            params[key] = float(int(round(params[key])))
    mgmt = dict(parent.mgmt)
    if rng.random() < 0.30:
        mgmt["mode"] = float(rng.randint(0, len(MGMT_MODES) - 1))
    for key in ("be_r", "partial_r", "partial_frac", "trail_atr"):
        if rng.random() < 0.20:
            lo, hi = MGMT_BOUNDS[key]
            mgmt[key] = max(lo, min(hi, mgmt.get(key, lo)
                                    + rng.uniform(-0.15, 0.15) * (hi - lo)))
    base = parent.sid.split("-m")[0]
    return StrategyConfig(
        sid=f"{base}-m{serial:02d}", archetype=parent.archetype,
        version=parent.version + 1, tf=parent.tf, params=params,
        regimes=parent.regimes, sessions=parent.sessions, mgmt=mgmt,
        parent=parent.sid, mutations=parent.mutations + 1,
        created=created_iso)
