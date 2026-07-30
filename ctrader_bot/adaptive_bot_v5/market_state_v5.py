"""
One multi-timeframe market picture, rebuilt when a decision bar closes.

Everything here is computed from COMPLETED candles only.  The V4 feature
builder is reused unchanged for the per-timeframe work (structure, zones,
FVGs, order blocks, liquidity, sweeps, ATR/EMA/RSI); this module adds the
cross-timeframe reasoning V5 needs and that V4 never had:

  * htf_bias      H1 + M30 agreement, with an explicit strength and reason
  * location      premium/discount of the H1 dealing range
  * pools         the important liquidity levels (previous day high/low,
                  Asia/London/session highs and lows, equal highs/lows,
                  confirmed swings) with their swept state
  * strong_trend  a nine-factor reading that decides whether a trailing
                  stop is allowed at all
  * opposing_zone the nearest higher-timeframe zone against a direction,
                  used both for target clipping and for early exits

MarketState is deliberately a plain data object: the setup families read it,
they never recompute it, so every family sees exactly the same market.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from adaptive_bot.core.models import (Candle, Direction, LiquidityKind,
                                      LiquidityLevel, LiquidityState,
                                      SessionName, StructureEventKind,
                                      Timeframe, TrendState, Zone, ZoneKind)
from adaptive_bot.strategy.liquidity import LiquidityDetector
from adaptive_bot.strategy.market_structure import StructureAnalyzer
from adaptive_bot.strategy.supply_demand import SupplyDemandDetector
from .features_v5 import TFFeatures

# Liquidity kinds that count as "important" for a sweep-based setup.  A sweep
# of a random intrabar wick is NOT important; these are levels where stops
# genuinely rest.
IMPORTANT_POOLS: Tuple[LiquidityKind, ...] = (
    LiquidityKind.PDH, LiquidityKind.PDL,
    LiquidityKind.PWH, LiquidityKind.PWL,
    LiquidityKind.SESSION_HIGH, LiquidityKind.SESSION_LOW,
    LiquidityKind.EQUAL_HIGHS, LiquidityKind.EQUAL_LOWS,
    LiquidityKind.SWING_HIGH, LiquidityKind.SWING_LOW,
    LiquidityKind.RANGE_HIGH, LiquidityKind.RANGE_LOW)

# Pools that on their own justify calling a sweep "major" (session/day/week
# levels and equal highs/lows).  A single swing high is minor by comparison.
MAJOR_POOLS: Tuple[LiquidityKind, ...] = (
    LiquidityKind.PDH, LiquidityKind.PDL,
    LiquidityKind.PWH, LiquidityKind.PWL,
    LiquidityKind.SESSION_HIGH, LiquidityKind.SESSION_LOW,
    LiquidityKind.EQUAL_HIGHS, LiquidityKind.EQUAL_LOWS)


@dataclass
class BiasReading:
    """Higher-timeframe directional context (H1 leading, M30 confirming)."""
    direction: Optional[Direction]     # None = no usable bias
    strength: str                      # STRONG | MODERATE | WEAK | NONE
    h1_trend: TrendState
    m30_trend: TrendState
    m15_trend: TrendState
    reason: str

    @property
    def has_bias(self) -> bool:
        return self.direction is not None

    def supports(self, direction: Direction) -> bool:
        return self.direction is not None and self.direction == direction

    def strongly_against(self, direction: Direction) -> bool:
        return (self.direction is not None and self.direction != direction
                and self.strength in ("STRONG", "MODERATE"))


@dataclass
class LocationReading:
    """Premium / discount of the H1 dealing range."""
    label: str                         # PREMIUM | DISCOUNT | EQUILIBRIUM
    position: float                    # 0 = range low, 1 = range high
    range_low: float = 0.0
    range_high: float = 0.0

    def favours(self, direction: Direction) -> bool:
        """Longs want discount, shorts want premium."""
        if direction == Direction.LONG:
            return self.label == "DISCOUNT"
        return self.label == "PREMIUM"

    def is_hostile(self, direction: Direction) -> bool:
        if direction == Direction.LONG:
            return self.label == "PREMIUM"
        return self.label == "DISCOUNT"


@dataclass
class StrongTrendReading:
    """Nine-factor strong-trend test. Gates the trailing stop."""
    direction: Optional[Direction]
    score: int
    factors: List[str] = field(default_factory=list)
    is_strong: bool = False
    bos_count: int = 0
    efficiency: float = 0.0
    atr_percentile: float = 0.5

    def explain(self) -> str:
        return f"strong-trend {self.score} factors: " + "; ".join(self.factors)


@dataclass
class SweptPool:
    level: LiquidityLevel
    swept_time: datetime
    extreme: float
    closed_back: bool
    displaced_away: bool
    bars_since: int

    @property
    def is_major(self) -> bool:
        return self.level.kind in MAJOR_POOLS


@dataclass
class MarketState:
    now: datetime
    bid: float
    ask: float
    spread_points: float
    spread_price: float
    point: float
    regime: str
    session: SessionName
    features: Dict[Timeframe, TFFeatures]
    bias: BiasReading
    location: LocationReading
    pools: List[LiquidityLevel]
    recent_sweeps: List[SweptPool]
    session_marks: Dict[str, Optional[float]]
    prev_day_high: Optional[float] = None
    prev_day_low: Optional[float] = None
    atr_m5: float = 0.0
    atr_m15: float = 0.0
    strong_trend: StrongTrendReading = field(
        default_factory=lambda: StrongTrendReading(None, 0, [], False))

    # ------------------------------------------------------------- accessors
    def f(self, tf: Timeframe) -> Optional[TFFeatures]:
        return self.features.get(tf)

    @property
    def m5(self) -> Optional[TFFeatures]:
        return self.features.get(Timeframe.M5)

    @property
    def m15(self) -> Optional[TFFeatures]:
        return self.features.get(Timeframe.M15)

    @property
    def m30(self) -> Optional[TFFeatures]:
        return self.features.get(Timeframe.M30)

    @property
    def h1(self) -> Optional[TFFeatures]:
        return self.features.get(Timeframe.H1)

    def ready(self) -> bool:
        return all(self.features.get(tf) is not None
                   for tf in (Timeframe.M5, Timeframe.M15, Timeframe.M30,
                              Timeframe.H1))

    # --------------------------------------------------------- liquidity ops
    def untouched_pools(self, direction: Direction,
                        beyond: float) -> List[LiquidityLevel]:
        """Untouched pools in the trade direction, nearest first."""
        if direction == Direction.LONG:
            cands = [p for p in self.pools
                     if p.buy_side and p.price > beyond
                     and p.state == LiquidityState.UNTOUCHED]
            return sorted(cands, key=lambda p: p.price)
        cands = [p for p in self.pools
                 if not p.buy_side and p.price < beyond
                 and p.state == LiquidityState.UNTOUCHED]
        return sorted(cands, key=lambda p: -p.price)

    def opposing_pools(self, direction: Direction,
                       beyond: float) -> List[LiquidityLevel]:
        """Pools that sit AGAINST the trade (behind the entry)."""
        return self.untouched_pools(
            Direction.SHORT if direction == Direction.LONG else Direction.LONG,
            beyond)

    def room_to_next_pool(self, direction: Direction,
                          price: float) -> Optional[float]:
        pools = self.untouched_pools(direction, price)
        if not pools:
            return None
        return abs(pools[0].price - price)

    def opposing_htf_zone(self, direction: Direction,
                          price: float) -> Optional[Zone]:
        """Nearest H1/M30 zone standing in the way of the trade."""
        want = ZoneKind.SUPPLY if direction == Direction.LONG else ZoneKind.DEMAND
        best: Optional[Zone] = None
        best_dist = float("inf")
        for tf in (Timeframe.H1, Timeframe.M30):
            feat = self.features.get(tf)
            if feat is None:
                continue
            for z in SupplyDemandDetector.active_zones(feat.zones, want, 0.30):
                edge = z.lower if direction == Direction.LONG else z.upper
                dist = (edge - price) if direction == Direction.LONG \
                    else (price - edge)
                if dist <= 0:
                    continue                      # already passed / behind us
                if dist < best_dist:
                    best, best_dist = z, dist
        return best

    def sweeps_for(self, direction: Direction,
                   max_bars: int = 6) -> List[SweptPool]:
        """Recent sweeps that would support a trade in `direction`.

        A LONG wants SELL-side liquidity swept below (a low taken out and
        reclaimed); a SHORT wants BUY-side liquidity swept above."""
        want_buy_side = direction == Direction.SHORT
        return [s for s in self.recent_sweeps
                if s.level.buy_side == want_buy_side
                and s.bars_since <= max_bars]


class MarketStateBuilder:
    """Assembles MarketState from per-timeframe features."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._analyzer = StructureAnalyzer(cfg)

    # ------------------------------------------------------------------ bias
    def bias(self, feats: Dict[Timeframe, TFFeatures]) -> BiasReading:
        h1 = feats.get(Timeframe.H1)
        m30 = feats.get(Timeframe.M30)
        m15 = feats.get(Timeframe.M15)
        h1_trend = h1.structure.trend if h1 else TrendState.UNDEFINED
        m30_trend = m30.structure.trend if m30 else TrendState.UNDEFINED
        m15_trend = m15.structure.trend if m15 else TrendState.UNDEFINED

        def as_dir(t: TrendState) -> Optional[Direction]:
            if t == TrendState.BULLISH:
                return Direction.LONG
            if t == TrendState.BEARISH:
                return Direction.SHORT
            return None

        h1_dir, m30_dir, m15_dir = (as_dir(h1_trend), as_dir(m30_trend),
                                    as_dir(m15_trend))
        # H1 leads. M30 confirms. M15 adds strength but cannot create bias.
        if h1_dir is not None and m30_dir == h1_dir:
            strength = "STRONG" if m15_dir == h1_dir else "MODERATE"
            reason = (f"H1 {h1_trend.value} + M30 {m30_trend.value}"
                      + (f" + M15 {m15_trend.value}" if m15_dir == h1_dir
                         else f" (M15 {m15_trend.value})"))
            return BiasReading(h1_dir, strength, h1_trend, m30_trend,
                               m15_trend, reason)
        if h1_dir is not None and m30_dir is None:
            return BiasReading(h1_dir, "WEAK", h1_trend, m30_trend, m15_trend,
                               f"H1 {h1_trend.value}, M30 not directional")
        if h1_dir is not None and m30_dir is not None and m30_dir != h1_dir:
            return BiasReading(None, "NONE", h1_trend, m30_trend, m15_trend,
                               f"H1 {h1_trend.value} conflicts with M30 "
                               f"{m30_trend.value}")
        if h1_dir is None and m30_dir is not None and m15_dir == m30_dir:
            return BiasReading(m30_dir, "WEAK", h1_trend, m30_trend, m15_trend,
                               f"H1 undirectional; M30+M15 {m30_trend.value}")
        return BiasReading(None, "NONE", h1_trend, m30_trend, m15_trend,
                           "no higher-timeframe direction")

    # -------------------------------------------------------------- location
    def location(self, feats: Dict[Timeframe, TFFeatures],
                 price: float) -> LocationReading:
        h1 = feats.get(Timeframe.H1)
        if h1 is None or h1.structure.dealing_range is None:
            return LocationReading("EQUILIBRIUM", 0.5)
        label, pos = self._analyzer.premium_discount(h1.structure, price)
        dr = h1.structure.dealing_range
        return LocationReading(label, pos, dr.low, dr.high)

    # ----------------------------------------------------------- strong trend
    def strong_trend(self, feats: Dict[Timeframe, TFFeatures],
                     bias: BiasReading,
                     opposing_zone_dist_atr: Optional[float]) -> StrongTrendReading:
        """Nine measured factors; the trailing stop needs most of them.

        Mandatory: H1 and M30 aligned, and at least `strong_trend_min_bos`
        recent M5 breaks of structure in the trade direction.  Without those
        the reading can never be 'strong', however many soft factors pass."""
        cfg = self.cfg
        direction = bias.direction
        factors: List[str] = []
        score = 0
        m5 = feats.get(Timeframe.M5)
        m15 = feats.get(Timeframe.M15)
        if direction is None or m5 is None:
            return StrongTrendReading(None, 0, ["no higher-timeframe bias"],
                                      False)

        def add(ok: bool, text: str) -> bool:
            nonlocal score
            factors.append(("PASS " if ok else "fail ") + text)
            if ok:
                score += 1
            return ok

        h1_ok = add(bias.h1_trend == (TrendState.BULLISH
                                      if direction == Direction.LONG
                                      else TrendState.BEARISH),
                    f"H1 trend {bias.h1_trend.value}")
        m30_ok = add(bias.m30_trend == (TrendState.BULLISH
                                        if direction == Direction.LONG
                                        else TrendState.BEARISH),
                     f"M30 trend {bias.m30_trend.value}")
        add(bias.m15_trend == (TrendState.BULLISH
                               if direction == Direction.LONG
                               else TrendState.BEARISH),
            f"M15 trend {bias.m15_trend.value}")

        # repeated BOS in the trade direction on M5 (last 40 bars)
        n = len(m5.candles)
        recent_bos = [e for e in m5.structure.events
                      if e.index >= n - 40
                      and e.kind in (StructureEventKind.BOS,
                                     StructureEventKind.MSS)
                      and e.direction == direction]
        bos_ok = add(len(recent_bos) >= cfg.strong_trend_min_bos,
                     f"{len(recent_bos)} M5 BOS/MSS in direction "
                     f"(need {cfg.strong_trend_min_bos})")

        add(m5.eff_ratio >= cfg.strong_trend_min_efficiency,
            f"M5 directional efficiency {m5.eff_ratio:.2f} "
            f"(need {cfg.strong_trend_min_efficiency:.2f})")
        add(cfg.strong_trend_atr_pct_low <= m5.atr_percentile
            <= cfg.strong_trend_atr_pct_high,
            f"ATR percentile {m5.atr_percentile:.2f} healthy")

        # continued displacement: a displacement candle in the direction in
        # the last 12 M5 bars
        disp = False
        atr_list = m5.atr
        for i in range(max(0, n - 12), n):
            c = m5.candles[i]
            a = atr_list[i] if i < len(atr_list) else 0.0
            if a <= 0:
                continue
            if c.body >= cfg.displacement_atr_mult * a \
                    and c.body_ratio >= cfg.displacement_body_ratio \
                    and ((c.close > c.open) == (direction == Direction.LONG)):
                disp = True
                break
        add(disp, "recent M5 displacement in direction")

        add(opposing_zone_dist_atr is None
            or opposing_zone_dist_atr >= cfg.strong_trend_opposing_zone_atr,
            "no immediate opposing HTF zone"
            + ("" if opposing_zone_dist_atr is None
               else f" ({opposing_zone_dist_atr:.2f} ATR away)"))

        # momentum has not materially weakened: current ROC keeps the sign and
        # at least half the magnitude of the M15 ROC
        roc_ok = (m5.roc > 0) == (direction == Direction.LONG)
        if m15 is not None and roc_ok:
            roc_ok = abs(m5.roc) >= 0.25 * abs(m15.roc) or abs(m15.roc) < 1e-9
        add(roc_ok, f"momentum intact (M5 ROC {m5.roc:+.4f})")

        mandatory = h1_ok and m30_ok and bos_ok
        is_strong = mandatory and score >= cfg.strong_trend_min_factors
        if not mandatory:
            factors.append("NOT STRONG: mandatory H1+M30 alignment and "
                           "repeated BOS are required")
        return StrongTrendReading(direction, score, factors, is_strong,
                                  len(recent_bos), m5.eff_ratio,
                                  m5.atr_percentile)

    # ---------------------------------------------------------------- pools
    def pools(self, feats: Dict[Timeframe, TFFeatures],
              session_marks: Dict[str, Optional[float]],
              m5_candles: List[Candle]) -> Tuple[List[LiquidityLevel],
                                                 List[SweptPool]]:
        """Important pools from M15 (structure-scale) plus session/day marks,
        with their swept state resolved on M5 candles."""
        m15 = feats.get(Timeframe.M15)
        if m15 is None:
            return [], []
        detector = LiquidityDetector(self.cfg)
        levels = detector.detect_levels(m15.candles, m15.structure.swings,
                                        session_marks)
        levels = [lv for lv in levels if lv.kind in IMPORTANT_POOLS]
        sweeps = detector.update_states(levels, m5_candles,
                                       None)
        n = len(m5_candles)
        out: List[SweptPool] = []
        for ev in sweeps:
            out.append(SweptPool(level=ev.level, swept_time=ev.time,
                                 extreme=ev.extreme,
                                 closed_back=ev.closed_back,
                                 displaced_away=ev.displaced_away,
                                 bars_since=max(0, n - 1 - ev.index)))
        out.sort(key=lambda s: s.bars_since)
        return levels, out

    # ----------------------------------------------------------------- build
    def build(self, now: datetime, bid: float, ask: float,
              spread_points: float, point: float, regime: str,
              session: SessionName,
              feats: Dict[Timeframe, TFFeatures],
              session_marks: Dict[str, Optional[float]],
              m5_candles: List[Candle],
              prev_day: Optional[Tuple[float, float]] = None) -> MarketState:
        bias = self.bias(feats)
        loc = self.location(feats, bid)
        pools, sweeps = self.pools(feats, session_marks, m5_candles)
        m5 = feats.get(Timeframe.M5)
        m15 = feats.get(Timeframe.M15)
        state = MarketState(
            now=now, bid=bid, ask=ask, spread_points=spread_points,
            spread_price=spread_points * point, point=point, regime=regime,
            session=session, features=feats, bias=bias, location=loc,
            pools=pools, recent_sweeps=sweeps, session_marks=session_marks,
            prev_day_high=prev_day[0] if prev_day else None,
            prev_day_low=prev_day[1] if prev_day else None,
            atr_m5=m5.atr_now if m5 else 0.0,
            atr_m15=m15.atr_now if m15 else 0.0)
        # strong-trend needs the state's own zone lookup
        dist_atr: Optional[float] = None
        if bias.direction is not None and state.atr_m5 > 0:
            zone = state.opposing_htf_zone(bias.direction, bid)
            if zone is not None:
                edge = zone.lower if bias.direction == Direction.LONG \
                    else zone.upper
                dist_atr = abs(edge - bid) / state.atr_m5
        state.strong_trend = self.strong_trend(feats, bias, dist_atr)
        return state
