"""
Strategy engine: builds the multi-timeframe AnalysisContext and runs the six
entry models, then scores and gates the best candidate into a Setup.

Timeframe roles are FIXED per the trading-system spec:
    M15 — macro bias & structure (HH/HL vs LH/LL, zones, premium/discount)
    M5  — decision layer (CHoCH/BOS confirmation, sweeps, displacement, FVG)
    M1  — entry trigger only (see entry_trigger.py; never trades alone)
    H1/H4/D1 — higher-timeframe zone context for the HTF_ZONE_REACTION model

All decisions use COMPLETED candles only.  The six models:
    1 TREND_CONTINUATION       bias + retrace into demand/supply + confirmation
    2 LIQUIDITY_SWEEP_REVERSAL sweep + displacement + CHoCH/MSS at zone/extreme
    3 BREAK_RETEST             displacement break, close beyond, retest holds
    4 RANGE_EXTREME            sweep beyond range edge, reclaim, CHoCH to mid
    5 SESSION_LIQUIDITY        Asian range swept by London/NY, reclaim+confirm
    6 HTF_ZONE_REACTION        D1/H4/H1 zone + LTF sweep + displacement + CHoCH

Counter-bias trades are rejected outright unless cfg.allow_reversals is True
(and even then they need cfg.countertrend_min_score).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from ..core.config import Config
from ..core.helpers import atr_series, is_displacement
from ..core.models import (Candle, Direction, FairValueGap, LiquidityKind,
                           LiquidityLevel, OrderBlock, Regime, RegimeReading,
                           SessionName, Setup, SetupGrade,
                           SetupModel, StructureEvent, StructureEventKind,
                           SweepEvent, Timeframe, TimeframePlan, TrendState,
                           Zone, ZoneKind, new_id)
from .fair_value_gap import FVGDetector
from .liquidity import LiquidityDetector
from .market_structure import StructureAnalyzer, StructureState
from .regime import MarketRegimeDetector
from .setup_scoring import SetupScorer
from .supply_demand import OrderBlockDetector, SupplyDemandDetector


@dataclass
class TFAnalysis:
    """Everything the engine knows about one timeframe (completed candles)."""
    timeframe: Timeframe
    candles: List[Candle]
    atr: List[float]
    structure: StructureState
    zones: List[Zone]
    fvgs: List[FairValueGap]
    order_blocks: List[OrderBlock]
    liquidity: List[LiquidityLevel]
    sweeps: List[SweepEvent]

    @property
    def last(self) -> Candle:
        return self.candles[-1]

    @property
    def atr_now(self) -> float:
        return self.atr[-1] if self.atr else 0.0


@dataclass
class AnalysisContext:
    time: object                      # datetime: close time of newest candle
    price: float                      # last decision-TF close
    spread_points: float
    point: float
    tf_plan: TimeframePlan
    regime: RegimeReading
    session: SessionName
    news_blocked: bool
    news_reason: str
    news_complete: bool
    tfs: Dict[Timeframe, TFAnalysis]
    asian_range: Optional[Tuple[float, float]] = None

    def tf(self, timeframe: Timeframe) -> Optional[TFAnalysis]:
        return self.tfs.get(timeframe)

    @property
    def bias(self) -> Optional[TFAnalysis]:
        return self.tf(self.tf_plan.bias_tf)

    @property
    def structure_tf(self) -> Optional[TFAnalysis]:
        return self.tf(self.tf_plan.structure_tf)

    @property
    def decision(self) -> Optional[TFAnalysis]:
        return self.tf(self.tf_plan.decision_tf)

    @property
    def entry(self) -> Optional[TFAnalysis]:
        return self.tf(self.tf_plan.entry_tf)


@dataclass
class Candidate:
    """A raw model output before scoring/gating."""
    model: SetupModel
    direction: Direction
    zone: Optional[Zone]
    sweep: Optional[SweepEvent]
    structure_event: Optional[StructureEvent]
    fvg: Optional[FairValueGap]
    order_block: Optional[OrderBlock]
    displacement: bool
    stop_anchor: float                # raw structural invalidation price
    stop_reason: str                  # human explanation of the anchor
    retest_level: Optional[float]     # M1 break-retest reference level
    note: str


FIXED_PLAN = TimeframePlan(
    bias_tf=Timeframe.M15, structure_tf=Timeframe.M15,
    decision_tf=Timeframe.M5, entry_tf=Timeframe.M1,
    management_tf=Timeframe.M5,
    reason="fixed per spec: M15 bias, M5 decision, M1 trigger")


class StrategyEngine:

    RECENT = 6      # candles: how recent confirmation must be
    ZONE_TOUCH_LOOKBACK = 8

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.scorer = SetupScorer(cfg)

    def _disp(self, candle: Candle, atr: float) -> bool:
        return is_displacement(candle, atr, self.cfg.displacement_atr_mult,
                               self.cfg.displacement_body_ratio)

    # ------------------------------------------------------------------ build
    def build_tf_analysis(self, tf: Timeframe,
                          candles: List[Candle],
                          session_marks: Optional[Dict[str, float]] = None,
                          external: bool = False) -> TFAnalysis:
        cfg = self.cfg
        left = cfg.external_swing_left if external else cfg.swing_left
        right = cfg.external_swing_right if external else cfg.swing_right
        analyzer = StructureAnalyzer(cfg, left, right)
        atr = atr_series(candles, cfg.atr_period)
        structure = analyzer.analyze(candles, atr)
        zones = SupplyDemandDetector(cfg, tf).detect(candles, structure)
        fvgs = FVGDetector(cfg, tf).detect(candles)
        liq_det = LiquidityDetector(cfg)
        levels = liq_det.detect_levels(candles, structure.swings, session_marks)
        sweeps = liq_det.update_states(levels, candles, atr)
        obs = OrderBlockDetector(cfg, tf).detect(candles, structure, sweeps)
        return TFAnalysis(tf, candles, atr, structure, zones, fvgs, obs,
                          levels, sweeps)

    # ------------------------------------------------------------------ helpers
    def _htf_bias(self, ctx: AnalysisContext) -> TrendState:
        bias = ctx.bias
        struct = ctx.structure_tf
        b = bias.structure.trend if bias else TrendState.UNDEFINED
        s = struct.structure.trend if struct else TrendState.UNDEFINED
        if b == s:
            return b
        if b in (TrendState.RANGING, TrendState.UNDEFINED):
            return s
        if s in (TrendState.RANGING, TrendState.UNDEFINED):
            return b
        return TrendState.RANGING     # conflict => treat as no clear bias

    def _recent_event(self, tfa: TFAnalysis, direction: Direction,
                      kinds: Tuple[StructureEventKind, ...],
                      recency: Optional[int] = None) -> Optional[StructureEvent]:
        rec = recency if recency is not None else self.RECENT
        n = len(tfa.candles)
        for ev in reversed(tfa.structure.events):
            if ev.index < n - rec:
                return None
            if ev.direction == direction and ev.kind in kinds:
                return ev
        return None

    def _recent_sweep(self, tfa: TFAnalysis, buy_side: bool,
                      recency: int = 12) -> Optional[SweepEvent]:
        n = len(tfa.candles)
        for sv in reversed(tfa.sweeps):
            if sv.index < n - recency:
                return None
            if sv.level.buy_side == buy_side and sv.valid:
                return sv
        return None

    def _zone_in_play(self, tfa: TFAnalysis, kind: ZoneKind,
                      tolerance: float) -> Optional[Zone]:
        """Zone touched by price within the last few candles."""
        recent = tfa.candles[-self.ZONE_TOUCH_LOOKBACK:]
        hi = max(c.high for c in recent)
        lo = min(c.low for c in recent)
        best: Optional[Zone] = None
        for z in SupplyDemandDetector.active_zones(tfa.zones, kind):
            touched = (z.lower - tolerance <= hi and lo <= z.upper + tolerance)
            if touched and (best is None or z.quality() > best.quality()):
                best = z
        return best

    def _confluence(self, tfa: TFAnalysis, direction: Direction,
                    around: float, tolerance: float
                    ) -> Tuple[Optional[FairValueGap], Optional[OrderBlock]]:
        fvg = None
        for g in FVGDetector.usable(tfa.fvgs, direction):
            if g.lower - tolerance <= around <= g.upper + tolerance:
                fvg = g
                break
        ob = None
        for b in OrderBlockDetector.usable(tfa.order_blocks, direction):
            if b.lower - tolerance <= around <= b.upper + tolerance:
                ob = b
                break
        return fvg, ob

    def _not_extended(self, ctx: AnalysisContext,
                      event: Optional[StructureEvent],
                      anchor: Optional[float]) -> bool:
        """Do-not-chase rule: current price must not be more than
        extension_max_atr * ATR beyond the confirmation level/anchor."""
        dec = ctx.decision
        if dec is None or dec.atr_now <= 0:
            return False
        ref = None
        if event is not None:
            ref = event.broken_level
        elif anchor is not None:
            ref = anchor
        if ref is None:
            return True
        return abs(ctx.price - ref) <= self.cfg.extension_max_atr * dec.atr_now

    # ------------------------------------------------------------------ models
    def model_trend_continuation(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 1: 15m bias + retracement into supply/demand + 5m confirmation."""
        bias = self._htf_bias(ctx)
        if bias not in (TrendState.BULLISH, TrendState.BEARISH):
            return None
        direction = Direction.LONG if bias == TrendState.BULLISH else Direction.SHORT
        dec = ctx.decision
        struct_tf = ctx.structure_tf
        if dec is None or struct_tf is None:
            return None
        tol = 0.5 * dec.atr_now
        want_zone = ZoneKind.DEMAND if direction == Direction.LONG else ZoneKind.SUPPLY
        zone = (self._zone_in_play(struct_tf, want_zone, tol)
                or self._zone_in_play(dec, want_zone, tol))
        # broken-structure retest / OB / FVG also qualify as the "area"
        fvg, ob = self._confluence(dec, direction, ctx.price, tol)
        if zone is None and fvg is None and ob is None:
            return None
        ev = self._recent_event(dec, direction,
                                (StructureEventKind.BOS, StructureEventKind.CHOCH,
                                 StructureEventKind.MSS))
        if ev is None:
            return None
        sweep = self._recent_sweep(dec, buy_side=(direction == Direction.SHORT))
        disp = ev.displacement or self._disp(dec.last, dec.atr_now)
        if not disp and sweep is None:
            return None                       # need rejection evidence
        if not self._not_extended(ctx, ev, zone.mid if zone else None):
            return None
        # structural stop anchor
        if direction == Direction.SHORT:
            anchors = [(dec.last.high, "above last 5m candle high")]
            if sweep:
                anchors.append((sweep.extreme, "above the swept high"))
            if zone:
                anchors.append((zone.upper, "above the supply zone"))
            if dec.structure.protected_high:
                anchors.append((dec.structure.protected_high.price,
                                "above the protected lower-high"))
            stop_anchor, stop_reason = max(anchors, key=lambda a: a[0])
        else:
            anchors = [(dec.last.low, "below last 5m candle low")]
            if sweep:
                anchors.append((sweep.extreme, "below the swept low"))
            if zone:
                anchors.append((zone.lower, "below the demand zone"))
            if dec.structure.protected_low:
                anchors.append((dec.structure.protected_low.price,
                                "below the protected higher-low"))
            stop_anchor, stop_reason = min(anchors, key=lambda a: a[0])
        retest = ev.broken_level
        if fvg is not None:
            retest = fvg.midpoint
        elif ob is not None:
            retest = ob.upper if direction == Direction.SHORT else ob.lower
        return Candidate(SetupModel.TREND_CONTINUATION, direction, zone, sweep,
                         ev, fvg, ob, disp, stop_anchor, stop_reason, retest,
                         f"{bias.value} continuation off "
                         f"{'zone ' + zone.pattern.value if zone else 'confluence'}"
                         f" with {ev.kind.value}")

    def model_liquidity_sweep_reversal(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 2: zone/discount + sweep + displacement + 5m CHoCH/MSS."""
        dec = ctx.decision
        struct_tf = ctx.structure_tf
        if dec is None or struct_tf is None:
            return None
        analyzer = StructureAnalyzer(self.cfg)
        for direction in (Direction.LONG, Direction.SHORT):
            sweep = self._recent_sweep(dec, buy_side=(direction == Direction.SHORT))
            if sweep is None:
                continue
            # meaningful HTF location: zone or premium/discount extreme
            want_zone = ZoneKind.DEMAND if direction == Direction.LONG else ZoneKind.SUPPLY
            tol = 0.5 * dec.atr_now
            zone = self._zone_in_play(struct_tf, want_zone, tol)
            pd_label, _pos = analyzer.premium_discount(struct_tf.structure, ctx.price)
            good_location = zone is not None or \
                (direction == Direction.LONG and pd_label == "DISCOUNT") or \
                (direction == Direction.SHORT and pd_label == "PREMIUM")
            if not good_location:
                continue
            # counter-bias reversals demand MSS or CHoCH WITH displacement
            ev = self._recent_event(dec, direction,
                                    (StructureEventKind.CHOCH, StructureEventKind.MSS))
            if ev is None:
                continue
            bias = self._htf_bias(ctx)
            countertrend = (direction == Direction.LONG and bias == TrendState.BEARISH) \
                or (direction == Direction.SHORT and bias == TrendState.BULLISH)
            if countertrend and not (ev.kind == StructureEventKind.MSS
                                     or ev.displacement):
                continue
            # protected swing beyond the sweep must exist (structure proved)
            if direction == Direction.LONG:
                prot = dec.structure.last_confirmed_low
                if prot is None or prot.price < sweep.extreme:
                    prot_ok = prot is not None and prot.index > sweep.index
                else:
                    prot_ok = True
                if not prot_ok:
                    continue
                stop_anchor = min(sweep.extreme, dec.last.low)
                stop_reason = "below the swept low"
            else:
                prot = dec.structure.last_confirmed_high
                if prot is None or prot.price > sweep.extreme:
                    prot_ok = prot is not None and prot.index > sweep.index
                else:
                    prot_ok = True
                if not prot_ok:
                    continue
                stop_anchor = max(sweep.extreme, dec.last.high)
                stop_reason = "above the swept high"
            if not self._not_extended(ctx, ev, sweep.level.price):
                continue
            fvg, ob = self._confluence(dec, direction, ev.broken_level,
                                       0.5 * dec.atr_now)
            disp = ev.displacement or sweep.displaced_away
            retest = fvg.midpoint if fvg else ev.broken_level
            return Candidate(SetupModel.LIQUIDITY_SWEEP_REVERSAL, direction,
                             zone, sweep, ev, fvg, ob, disp, stop_anchor,
                             stop_reason, retest,
                             f"sweep of {sweep.level.kind.value} then "
                             f"{ev.kind.value} ({pd_label.lower()})")
        return None

    def model_break_retest(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 3: displacement break + close beyond + retest holding."""
        dec = ctx.decision
        if dec is None or dec.atr_now <= 0:
            return None
        cfg = self.cfg
        n = len(dec.candles)
        for ev in reversed(dec.structure.events):
            if ev.index < n - cfg.retest_max_candles:
                break
            if not ev.displacement:
                continue
            direction = ev.direction
            level = ev.broken_level
            # find a completed retest after the break: price returned to the
            # level and the latest candle rejected in the break direction
            touched = False
            for j in range(ev.index + 1, n):
                c = dec.candles[j]
                if direction == Direction.LONG and c.low <= level + 0.15 * dec.atr_now:
                    touched = True
                if direction == Direction.SHORT and c.high >= level - 0.15 * dec.atr_now:
                    touched = True
            if not touched:
                continue
            last = dec.last
            rejected = (direction == Direction.LONG and last.bullish
                        and last.close > level) or \
                       (direction == Direction.SHORT and last.bearish
                        and last.close < level)
            if not rejected:
                continue
            if not self._not_extended(ctx, ev, None):
                continue
            if direction == Direction.LONG:
                stop_anchor = min(c.low for c in dec.candles[ev.index:n])
                stop_reason = "below the retest low"
            else:
                stop_anchor = max(c.high for c in dec.candles[ev.index:n])
                stop_reason = "above the retest high"
            fvg, ob = self._confluence(dec, direction, level, 0.5 * dec.atr_now)
            sweep = self._recent_sweep(dec, buy_side=(direction == Direction.SHORT))
            return Candidate(SetupModel.BREAK_RETEST, direction, None, sweep,
                             ev, fvg, ob, True, stop_anchor, stop_reason, level,
                             f"break+retest of {level:.2f} ({ev.kind.value})")
        return None

    def model_range_extreme(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 4: sweep beyond range edge, reclaim, CHoCH toward mid."""
        if ctx.regime.regime != Regime.RANGE:
            return None
        dec = ctx.decision
        if dec is None or dec.structure.dealing_range is None:
            return None
        rng = dec.structure.dealing_range
        pos = rng.position_of(ctx.price)
        edge = self.cfg.range_edge_fraction
        if edge < pos < 1.0 - edge:
            return None                # never trade the middle of the range
        direction = Direction.LONG if pos <= edge else Direction.SHORT
        sweep = self._recent_sweep(dec, buy_side=(direction == Direction.SHORT))
        if sweep is None:
            return None
        ev = self._recent_event(dec, direction,
                                (StructureEventKind.CHOCH, StructureEventKind.MSS))
        if ev is None:
            return None
        stop_reason = ("below the swept range low" if direction == Direction.LONG
                       else "above the swept range high")
        return Candidate(SetupModel.RANGE_EXTREME, direction, None, sweep, ev,
                         None, None, ev.displacement, sweep.extreme,
                         stop_reason, ev.broken_level,
                         f"range edge (pos {pos:.2f}) sweep+reclaim")

    def model_session_liquidity(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 5: Asian range swept by London/NY, reclaim + confirmation."""
        if ctx.session not in (SessionName.LONDON, SessionName.NEW_YORK,
                               SessionName.OVERLAP):
            return None
        if ctx.asian_range is None:
            return None
        dec = ctx.decision
        if dec is None:
            return None
        asian_low, asian_high = ctx.asian_range
        for direction, swept_side in ((Direction.LONG, False),
                                      (Direction.SHORT, True)):
            sweep = self._recent_sweep(dec, buy_side=swept_side, recency=18)
            if sweep is None:
                continue
            # the swept level must belong to the Asian range boundary area
            tol = 0.6 * dec.atr_now
            boundary = asian_low if direction == Direction.LONG else asian_high
            if abs(sweep.level.price - boundary) > tol and \
                    sweep.level.kind not in (LiquidityKind.SESSION_LOW,
                                             LiquidityKind.SESSION_HIGH):
                continue
            ev = self._recent_event(dec, direction,
                                    (StructureEventKind.CHOCH,
                                     StructureEventKind.MSS,
                                     StructureEventKind.BOS))
            if ev is None or not (ev.displacement or sweep.displaced_away):
                continue
            bias = self._htf_bias(ctx)
            against = (direction == Direction.LONG and bias == TrendState.BEARISH) \
                or (direction == Direction.SHORT and bias == TrendState.BULLISH)
            if against and ev.kind != StructureEventKind.MSS:
                continue          # session reversals against 15m bias need MSS
            fvg, ob = self._confluence(dec, direction, ev.broken_level,
                                       0.5 * dec.atr_now)
            stop_reason = ("below the swept Asian low" if direction == Direction.LONG
                           else "above the swept Asian high")
            return Candidate(SetupModel.SESSION_LIQUIDITY, direction, None,
                             sweep, ev, fvg, ob, True, sweep.extreme,
                             stop_reason, ev.broken_level,
                             f"{ctx.session.value} sweep of Asian "
                             f"{'low' if direction == Direction.LONG else 'high'}")
        return None

    def model_htf_zone_reaction(self, ctx: AnalysisContext) -> Optional[Candidate]:
        """MODEL 6: D1/H4/H1 zone + LTF sweep + displacement + CHoCH."""
        entry_tfa = ctx.decision      # confirmation is read on the 5m layer
        if entry_tfa is None:
            return None
        tol_src = ctx.decision or entry_tfa
        for htf in (Timeframe.D1, Timeframe.H4, Timeframe.H1):
            tfa = ctx.tf(htf)
            if tfa is None:
                continue
            tol = 0.35 * tfa.atr_now if tfa.atr_now > 0 else 0.0
            for kind, direction in ((ZoneKind.DEMAND, Direction.LONG),
                                    (ZoneKind.SUPPLY, Direction.SHORT)):
                zone = self._zone_in_play(tfa, kind, tol)
                if zone is None:
                    continue
                # price must actually be at/near the zone now
                near = zone.lower - tol <= ctx.price <= zone.upper + tol or \
                    abs(ctx.price - zone.mid) <= 1.2 * tol_src.atr_now
                if not near:
                    continue
                sweep = self._recent_sweep(entry_tfa,
                                           buy_side=(direction == Direction.SHORT))
                ev = self._recent_event(entry_tfa, direction,
                                        (StructureEventKind.CHOCH,
                                         StructureEventKind.MSS))
                if ev is None or not (ev.displacement or (sweep and sweep.displaced_away)):
                    continue
                if direction == Direction.LONG:
                    stop_anchor = min(zone.lower,
                                      sweep.extreme if sweep else zone.lower)
                    stop_reason = f"below the {htf.value} demand zone"
                else:
                    stop_anchor = max(zone.upper,
                                      sweep.extreme if sweep else zone.upper)
                    stop_reason = f"above the {htf.value} supply zone"
                zone.htf_aligned = True
                fvg, ob = self._confluence(entry_tfa, direction,
                                           ev.broken_level,
                                           0.5 * entry_tfa.atr_now)
                return Candidate(SetupModel.HTF_ZONE_REACTION, direction,
                                 zone, sweep, ev, fvg, ob,
                                 ev.displacement, stop_anchor, stop_reason,
                                 fvg.midpoint if fvg else ev.broken_level,
                                 f"{htf.value} {kind.value} reaction")
        return None

    # ------------------------------------------------------------------ assembly
    MODELS_BY_REGIME: Dict[Regime, Tuple[SetupModel, ...]] = {
        Regime.STRONG_BULL: (SetupModel.TREND_CONTINUATION, SetupModel.BREAK_RETEST,
                             SetupModel.SESSION_LIQUIDITY, SetupModel.HTF_ZONE_REACTION),
        Regime.WEAK_BULL: (SetupModel.TREND_CONTINUATION, SetupModel.HTF_ZONE_REACTION,
                           SetupModel.SESSION_LIQUIDITY, SetupModel.LIQUIDITY_SWEEP_REVERSAL),
        Regime.STRONG_BEAR: (SetupModel.TREND_CONTINUATION, SetupModel.BREAK_RETEST,
                             SetupModel.SESSION_LIQUIDITY, SetupModel.HTF_ZONE_REACTION),
        Regime.WEAK_BEAR: (SetupModel.TREND_CONTINUATION, SetupModel.HTF_ZONE_REACTION,
                           SetupModel.SESSION_LIQUIDITY, SetupModel.LIQUIDITY_SWEEP_REVERSAL),
        Regime.RANGE: (SetupModel.RANGE_EXTREME, SetupModel.SESSION_LIQUIDITY,
                       SetupModel.HTF_ZONE_REACTION),
        Regime.COMPRESSION: (SetupModel.HTF_ZONE_REACTION,),
        Regime.EXPANSION: (SetupModel.BREAK_RETEST, SetupModel.TREND_CONTINUATION),
        Regime.REVERSAL_ATTEMPT: (SetupModel.LIQUIDITY_SWEEP_REVERSAL,
                                  SetupModel.HTF_ZONE_REACTION),
        Regime.NEWS_VOLATILITY: (),
        Regime.ABNORMAL_SPREAD: (),
        Regime.UNSAFE: (),
    }

    def evaluate(self, ctx: AnalysisContext,
                 cost_price_units: float,
                 reject_cb: Optional[Callable[[str, str, str], None]] = None
                 ) -> Optional[Setup]:
        """Run enabled models for the regime, score, gate, return best Setup.
        reject_cb(model, stage, reason) journals every rejected candidate."""
        enabled = self.MODELS_BY_REGIME.get(ctx.regime.regime, ())
        if not enabled:
            if reject_cb:
                reject_cb("ALL", "regime", f"no models enabled in regime "
                          f"{ctx.regime.regime.value}: {ctx.regime.reason}")
            return None
        model_fns = {
            SetupModel.TREND_CONTINUATION: self.model_trend_continuation,
            SetupModel.LIQUIDITY_SWEEP_REVERSAL: self.model_liquidity_sweep_reversal,
            SetupModel.BREAK_RETEST: self.model_break_retest,
            SetupModel.RANGE_EXTREME: self.model_range_extreme,
            SetupModel.SESSION_LIQUIDITY: self.model_session_liquidity,
            SetupModel.HTF_ZONE_REACTION: self.model_htf_zone_reaction,
        }
        best: Optional[Setup] = None
        for model in enabled:
            try:
                cand = model_fns[model](ctx)
            except Exception as exc:            # a crashed model must never
                if reject_cb:                   # take the bot down
                    reject_cb(model.value, "exception", str(exc))
                continue
            if cand is None:
                continue
            setup = self._assemble(ctx, cand, cost_price_units, reject_cb)
            if setup is None:
                continue
            if best is None or setup.score > best.score:
                best = setup
        return best

    def _assemble(self, ctx: AnalysisContext, cand: Candidate,
                  cost: float,
                  reject_cb: Optional[Callable[[str, str, str], None]]
                  ) -> Optional[Setup]:
        cfg = self.cfg
        dec = ctx.decision
        if dec is None or dec.atr_now <= 0:
            return None
        atr = dec.atr_now
        direction = cand.direction
        price = ctx.price

        def reject(stage: str, reason: str) -> None:
            if reject_cb:
                reject_cb(cand.model.value, stage, reason)

        # ---- 15m bias gate: reversals must be explicitly enabled ----------
        htf_bias = self._htf_bias(ctx)
        countertrend = (direction == Direction.LONG and htf_bias == TrendState.BEARISH) \
            or (direction == Direction.SHORT and htf_bias == TrendState.BULLISH)
        if countertrend and not cfg.allow_reversals:
            reject("bias", f"{direction.value} against 15m bias "
                   f"{htf_bias.value} and allow_reversals is False")
            return None

        # ---- stop with buffer (spread + configured ATR buffer) ------------
        buffer = cfg.stop_buffer_atr * atr + ctx.spread_points * ctx.point
        stop = cand.stop_anchor + buffer if direction == Direction.SHORT \
            else cand.stop_anchor - buffer
        stop_dist = abs(price - stop)
        if stop_dist < cfg.min_stop_atr * atr:
            reject("stop", f"stop {stop_dist:.2f} inside noise "
                   f"(< {cfg.min_stop_atr} ATR = {cfg.min_stop_atr * atr:.2f})")
            return None
        if stop_dist > cfg.max_stop_atr * atr:
            reject("stop", f"stop {stop_dist:.2f} too wide "
                   f"(> {cfg.max_stop_atr} ATR = {cfg.max_stop_atr * atr:.2f})")
            return None

        entry_price = price               # market entry after M1 trigger

        # ---- targets: logical opposing liquidity ---------------------------
        struct_tf = ctx.structure_tf or dec
        pool = list(dec.liquidity) + list(struct_tf.liquidity)
        targets = LiquidityDetector.targets_beyond(pool, entry_price,
                                                   direction, count=4)
        # opposing zone edges also act as targets
        opposing_kind = ZoneKind.SUPPLY if direction == Direction.LONG else ZoneKind.DEMAND
        for z in SupplyDemandDetector.active_zones(struct_tf.zones, opposing_kind):
            edge = z.lower if direction == Direction.LONG else z.upper
            if (direction == Direction.LONG and edge > entry_price) or \
               (direction == Direction.SHORT and edge < entry_price):
                targets.append(LiquidityLevel(new_id("liq"),
                                              LiquidityKind.RANGE_HIGH if direction == Direction.LONG
                                              else LiquidityKind.RANGE_LOW,
                                              edge, z.created_time,
                                              buy_side=(direction == Direction.LONG)))
        targets.sort(key=lambda l: l.price if direction == Direction.LONG else -l.price)
        # minimum meaningful distance for TP1: 1 ATR or min_rr, whichever larger
        min_tp1 = entry_price + direction.sign * max(atr,
                                                     cfg.min_rr * stop_dist * 0.75)
        usable = [t for t in targets
                  if (direction == Direction.LONG and t.price >= min_tp1)
                  or (direction == Direction.SHORT and t.price <= min_tp1)]
        if not usable:
            reject("target", "no opposing liquidity far enough for TP1")
            return None
        tp1 = usable[0].price
        target_reason = f"opposing {usable[0].kind.value} at {tp1:.2f}"
        tp2 = usable[1].price if len(usable) > 1 else \
            entry_price + direction.sign * min(cfg.preferred_rr_cap * stop_dist,
                                               2.0 * abs(tp1 - entry_price))
        runner = usable[2].price if len(usable) > 2 else None
        # never target beyond major opposing structure blindly: cap runner
        if runner is not None and abs(runner - entry_price) > cfg.preferred_rr_cap * stop_dist * 1.5:
            runner = None

        # ---- net RR check ----------------------------------------------------
        reward1 = abs(tp1 - entry_price) - cost
        risk1 = stop_dist + cost
        rr1 = reward1 / risk1 if risk1 > 0 else 0.0

        # ---- score ----------------------------------------------------------
        analyzer = StructureAnalyzer(cfg)
        pd_label, _ = analyzer.premium_discount(struct_tf.structure, entry_price)
        breakdown = self.scorer.score(
            direction=direction, htf_bias=htf_bias, zone=cand.zone,
            sweep=cand.sweep, structure_event=cand.structure_event,
            displacement=cand.displacement, fvg=cand.fvg,
            order_block=cand.order_block, pd_zone=pd_label,
            session=ctx.session, news_blocked=ctx.news_blocked,
            news_protection_complete=ctx.news_complete,
            rr_tp1=rr1, target_is_liquidity=usable[0].kind not in
            (LiquidityKind.RANGE_HIGH, LiquidityKind.RANGE_LOW))
        score = breakdown.total
        grade = SetupGrade.from_score(score)

        if rr1 < cfg.min_rr:
            reject("rr", f"net RR to TP1 {rr1:.2f} < required {cfg.min_rr:.2f}")
            return None

        threshold = cfg.countertrend_min_score if countertrend else cfg.min_score
        if ctx.session == SessionName.ASIA:
            threshold = max(threshold, cfg.asia_min_score)
        if score < threshold:
            reject("score", f"score {score:.1f} < threshold {threshold:.1f} "
                   f"({'countertrend' if countertrend else 'with-trend'}, "
                   f"session {ctx.session.value})")
            return None

        return Setup(
            setup_id=new_id("setup"), model=cand.model, direction=direction,
            created_time=ctx.time, signal_price=price,
            entry_price=entry_price, stop_price=stop, tp1=tp1, tp2=tp2,
            runner_target=runner, score=score,
            grade=grade, breakdown=breakdown, tf_plan=ctx.tf_plan,
            regime=ctx.regime.regime, session=ctx.session,
            htf_bias=htf_bias, zone=cand.zone, sweep=cand.sweep,
            structure_event=cand.structure_event, fvg=cand.fvg,
            order_block=cand.order_block, atr=atr,
            spread_points=ctx.spread_points, reason=cand.note,
            stop_reason=f"{cand.stop_reason} + {cfg.stop_buffer_atr} ATR buffer",
            target_reason=target_reason)


class ContextBuilder:
    """Builds an AnalysisContext from per-timeframe COMPLETED candle series
    (supplied by the main cBot from native cTrader Bars)."""

    def __init__(self, cfg: Config, engine: StrategyEngine,
                 sessions, news):
        self.cfg = cfg
        self.engine = engine
        self.sessions = sessions
        self.news = news

    def build(self, series: Dict[Timeframe, List[Candle]],
              spread_points: float, point: float,
              now) -> Optional[AnalysisContext]:
        cfg = self.cfg
        plan = FIXED_PLAN
        needed = {plan.bias_tf, plan.structure_tf, plan.decision_tf,
                  plan.entry_tf, plan.management_tf,
                  Timeframe.H1, Timeframe.H4, Timeframe.D1}

        news_blocked, news_reason = self.news.blackout(now)
        session = self.sessions.session_at(now)
        day = now.date()
        marks = self.sessions.marks_for(day)

        tfs: Dict[Timeframe, TFAnalysis] = {}
        for tf in needed:
            cs = series.get(tf)
            if not cs or len(cs) < cfg.atr_period + 10:
                continue
            external = tf in (Timeframe.H4, Timeframe.D1)
            tfs[tf] = self.engine.build_tf_analysis(
                tf, cs, marks if tf == plan.decision_tf else None,
                external=external)
        if plan.decision_tf not in tfs or plan.structure_tf not in tfs \
                or plan.bias_tf not in tfs:
            return None
        if cfg.strict_mode and plan.entry_tf not in tfs \
                and cfg.require_m1_trigger:
            return None       # strict: no M1 data => no trading

        # regime on the structure timeframe (M15)
        sfa = tfs[plan.structure_tf]
        regime = MarketRegimeDetector(cfg).classify(
            sfa.candles, sfa.structure, spread_points, news_blocked)
        return AnalysisContext(
            time=now, price=series[plan.decision_tf][-1].close,
            spread_points=spread_points, point=point, tf_plan=plan,
            regime=regime, session=session, news_blocked=news_blocked,
            news_reason=news_reason,
            news_complete=self.news.protection_complete(),
            tfs=tfs, asian_range=self.sessions.asian_range(day))
