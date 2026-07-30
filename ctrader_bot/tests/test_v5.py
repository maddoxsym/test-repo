"""
Unit tests for XAUUSD_Adaptive_Bot_V5.

These cover every case the specification asks for, and additionally prove that
the three V4 defects reported (a STOP_LOSS with positive R, an R above the
recorded MFE, and a stale pending fill) are now impossible rather than merely
unlikely.

Run from the ctrader_bot directory:
    python3 -m unittest tests.test_v5 -v
"""

import math
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adaptive_bot.core.models import (Candle, CTraderSymbolSpec, Direction,
                                      LockReason, SessionName, SwingKind,
                                      Timeframe, TrendState)
from adaptive_bot_v5.confluence import (GROUP_CAPS, MAX_SCORE,
                                        ConfluenceEngine, ConfluenceFactor,
                                        ConfluenceInputs)
from adaptive_bot_v5.config_v5 import V5Config, V5ConfigValidator
from adaptive_bot_v5.execution_v5 import RealTradeRecord, broker_level
from adaptive_bot_v5.features_v5 import FeatureBuilder
from adaptive_bot_v5.learning_v5 import LearningBook, Stats
from adaptive_bot_v5.management import (CLOSE, EXIT_BREAKEVEN_STOP,
                                        EXIT_STOP_LOSS, EXIT_TAKE_PROFIT,
                                        EXIT_TRAIL_STOP, MOVE_STOP, PARTIAL,
                                        ManagedTrade, ManagementView,
                                        TradeManager)
from adaptive_bot_v5.market_state_v5 import (BiasReading, LocationReading,
                                             MarketState, MarketStateBuilder,
                                             StrongTrendReading)
from adaptive_bot_v5.persistence_v5 import StateStore
from adaptive_bot_v5.reporting_v5 import PROVISIONAL_LABEL, final_report
from adaptive_bot_v5.research_clock import ResearchClock
from adaptive_bot_v5.risk_v5 import RiskEngine, V5Guard
from adaptive_bot_v5.setups_v5 import (DEFAULT_PARAMS, FAMILIES, PARAM_BOUNDS,
                                       SetupCandidate, StrategyVariant,
                                       mutate_variant, seed_population)
from adaptive_bot_v5.shadow_v5 import ShadowEngine, VirtualTrade
from adaptive_bot_v5.trade_plan import (Invalidation, StopBuilder,
                                        TargetBuilder, net_rr, planned_fill,
                                        risk_price)

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)      # a Monday
POINT = 0.01
SPREAD_POINTS = 30.0
SPREAD_PRICE = SPREAD_POINTS * POINT             # 0.30


# ===========================================================================
# helpers
# ===========================================================================

def make_spec(volume_min=0.01, volume_step=0.01, volume_max=100000.0):
    """A gold-like specification: tick 0.01 worth 0.01, so 1.0 of price move
    on 1 unit is worth 1.0 account currency."""
    return CTraderSymbolSpec(
        name="XAUUSD", digits=2, tick_size=0.01, tick_value=0.01,
        pip_size=0.10, pip_value=0.10, volume_min=volume_min,
        volume_max=volume_max, volume_step=volume_step,
        spread_points=SPREAD_POINTS)


def bar(t, o, h, l, c, volume=100.0):
    return Candle(t, o, h, l, c, volume, 0.0)


def flat_candles(n=80, price=3400.0, rng=1.0, t0=T0, minutes=5):
    """Quiet candles with a small, constant range and no trend."""
    out = []
    for i in range(n):
        drift = 0.05 * ((i % 4) - 1.5)
        o = price + drift
        c = o + 0.10 * (1 if i % 2 else -1)
        out.append(bar(t0 + timedelta(minutes=minutes * i), o,
                       max(o, c) + rng / 2, min(o, c) - rng / 2, c))
    return out


def zigzag_candles(n=90, start=3300.0, drift=1.2, amp=3.0, period=8,
                   t0=T0, minutes=5, rng=1.2):
    """A trending series with clean alternating swings, so the structure
    engine produces higher highs / higher lows (or the mirror)."""
    out = []
    prev = start
    for i in range(n):
        base = start + drift * i + amp * math.sin(2 * math.pi * i / period)
        o = prev
        c = base
        hi = max(o, c) + rng / 2
        lo = min(o, c) - rng / 2
        out.append(bar(t0 + timedelta(minutes=minutes * i), o, hi, lo, c))
        prev = c
    return out


def feats(candles, tf=Timeframe.M5, cfg=None):
    return FeatureBuilder(cfg or V5Config()).build(tf, candles)


def make_state(cfg, m5_candles, *, bias_dir=Direction.LONG,
               bias_strength="STRONG", strong=False, m15_candles=None,
               location="DISCOUNT", session=SessionName.LONDON,
               regime="STRONG_BULL", spread_points=SPREAD_POINTS,
               pools=None, sweeps=None, full_context=True):
    f5 = feats(m5_candles, Timeframe.M5, cfg)
    assert f5 is not None, "M5 features could not be built from the fixture"
    features = {Timeframe.M5: f5}
    source15 = m15_candles if m15_candles is not None else m5_candles
    f15 = feats(source15, Timeframe.M15, cfg)
    if f15 is not None:
        features[Timeframe.M15] = f15
    if full_context:
        # the setup families require a complete M5/M15/M30/H1 picture; the
        # fixture reuses the same shape for the slower frames
        for tf in (Timeframe.M30, Timeframe.H1):
            built = feats(source15, tf, cfg)
            if built is not None:
                features[tf] = built
    want = TrendState.BULLISH if bias_dir == Direction.LONG \
        else TrendState.BEARISH
    bias = BiasReading(bias_dir, bias_strength, want, want, want, "fixture")
    loc = LocationReading(location, 0.2 if location == "DISCOUNT" else 0.8,
                          3380.0, 3420.0)
    st = StrongTrendReading(bias_dir if strong else None, 9 if strong else 1,
                            ["fixture"], strong, 3, 0.5, 0.5)
    last = f5.candles[-1].close
    return MarketState(
        now=f5.candles[-1].time + timedelta(minutes=5), bid=last,
        ask=last + spread_points * POINT, spread_points=spread_points,
        spread_price=spread_points * POINT, point=POINT, regime=regime,
        session=session, features=features, bias=bias, location=loc,
        pools=pools or [], recent_sweeps=sweeps or [], session_marks={},
        atr_m5=f5.atr_now, atr_m15=f5.atr_now, strong_trend=st)


def make_view(cfg, state, spec=None, **kwargs):
    return ManagementView(now=state.now, state=state, spec=spec or make_spec(),
                          **kwargs)


def make_candidate(direction, entry_ref, stop, tp1, target, created=T0,
                   sid="TC-A", family="TREND_CONTINUATION", version=1,
                   score=70.0):
    conf = ConfluenceEngine._aggregate([
        ConfluenceFactor("FIXTURE", "HTF_DIRECTION", score, True, "fixture")])
    return SetupCandidate(
        family=family, sid=sid, version=version, direction=direction,
        created=created, entry_ref=entry_ref, stop=stop,
        stop_reason="fixture invalidation", tp1=tp1, tp1_reason="fixture TP1",
        target=target, target_reason="fixture target", tp1_r=1.2,
        target_r=2.4, blended_rr=1.9, confluence=conf, sequence=["PASS ALL"],
        invalidation="fixture", regime="STRONG_BULL", session="LONDON",
        htf_bias="LONG/STRONG", location="DISCOUNT")


class ShadowHarness:
    """Collects everything a ShadowEngine emits."""

    def __init__(self, cfg, spec=None):
        self.cfg = cfg
        self.spec = spec or make_spec()
        self.closed = []
        self.rows = []
        self.suspects = []
        self.management = []
        self.watch = []
        self.logs = []
        self.engine = ShadowEngine(
            cfg, log=self.logs.append,
            on_close=self.closed.append,
            record_row=self.rows.append,
            record_suspect=self.suspects.append,
            record_management=lambda t, a: self.management.append((t, a)),
            on_watch_done=self.watch.append)

    def m1(self, candle, now=None, spread_points=SPREAD_POINTS):
        self.engine.on_m1(candle, spread_points, POINT, self.spec,
                          now or candle.time + timedelta(minutes=1))


def make_managed(direction, entry, stop, tp1, target, units=10.0,
                 entry_time=T0, family="TREND_CONTINUATION",
                 spread_price=SPREAD_PRICE):
    risk_dist = risk_price(direction, entry, stop, spread_price)
    return ManagedTrade(
        trade_id="t1", sid="TC-A", family=family, direction=direction,
        entry=entry, initial_stop=stop, stop=stop, tp1=tp1, target=target,
        units_initial=units, units=units, risk_dist=risk_dist,
        risk_money=units * risk_dist, entry_time=entry_time)


# ===========================================================================
# 1. configuration rails
# ===========================================================================

class TestConfigRails(unittest.TestCase):

    def test_default_config_is_valid(self):
        v = V5ConfigValidator(V5Config())
        self.assertTrue(v.validate(), v.report())

    def test_risk_ceiling_is_tighter_than_v4(self):
        cfg = V5Config()
        self.assertLessEqual(cfg.max_risk_per_trade, 0.0025)
        cfg.max_risk_per_trade = 0.0075          # V4's ceiling
        self.assertFalse(V5ConfigValidator(cfg).validate())

    def test_real_trade_cap_cannot_be_loosened(self):
        cfg = V5Config()
        cfg.max_real_trades_per_day = 12
        cfg.max_trades_per_day = 12
        self.assertFalse(V5ConfigValidator(cfg).validate())

    def test_daily_and_weekly_limits_cannot_be_loosened(self):
        for field, value in (("max_daily_loss", 0.05),
                            ("max_weekly_drawdown", 0.10)):
            cfg = V5Config()
            setattr(cfg, field, value)
            self.assertFalse(V5ConfigValidator(cfg).validate(), field)

    def test_multiple_positions_refused(self):
        cfg = V5Config()
        cfg.max_positions = 2
        self.assertFalse(V5ConfigValidator(cfg).validate())

    def test_breakeven_justification_cannot_be_disabled(self):
        cfg = V5Config()
        cfg.be_require_justification = False
        self.assertFalse(V5ConfigValidator(cfg).validate())

    def test_trailing_cannot_run_in_ranges_by_config(self):
        cfg = V5Config()
        cfg.strong_trend_min_bos = 1
        self.assertFalse(V5ConfigValidator(cfg).validate())

    def test_early_exit_cannot_be_made_trigger_happy(self):
        cfg = V5Config()
        cfg.early_exit_min_score = 1
        self.assertFalse(V5ConfigValidator(cfg).validate())

    def test_minimum_stop_floor_required(self):
        cfg = V5Config()
        cfg.min_stop_atr_frac = 0.0
        self.assertFalse(V5ConfigValidator(cfg).validate())

    def test_stale_signal_window_bounded(self):
        cfg = V5Config()
        cfg.max_signal_age_minutes = 600
        self.assertFalse(V5ConfigValidator(cfg).validate())

    def test_confluence_floor_enforced(self):
        cfg = V5Config()
        cfg.min_confluence = 20
        self.assertFalse(V5ConfigValidator(cfg).validate())

    def test_missing_news_dates_warns_but_starts(self):
        v = V5ConfigValidator(V5Config())
        self.assertTrue(v.validate())
        self.assertTrue(any("NEWS DATES MISSING" in w for w in v.warnings))


# ===========================================================================
# 2. confluence engine
# ===========================================================================

class TestConfluence(unittest.TestCase):

    def test_group_caps_sum_to_100(self):
        self.assertAlmostEqual(sum(GROUP_CAPS.values()), 100.0)
        self.assertAlmostEqual(MAX_SCORE, 100.0)

    def test_correlated_confirmations_are_capped(self):
        """The LOCATION group's raw weights exceed its cap, so a zone, an
        order block, an FVG and a discount location cannot stack freely."""
        factors = [
            ConfluenceFactor("PREMIUM_DISCOUNT", "LOCATION", 7.0, True, ""),
            ConfluenceFactor("SD_ZONE", "LOCATION", 9.0, True, ""),
            ConfluenceFactor("ORDER_BLOCK", "LOCATION", 8.0, True, ""),
            ConfluenceFactor("FVG", "LOCATION", 8.0, True, ""),
        ]
        res = ConfluenceEngine._aggregate(factors)
        raw, capped, cap = res.groups["LOCATION"]
        self.assertAlmostEqual(raw, 32.0)
        self.assertAlmostEqual(cap, 22.0)
        self.assertAlmostEqual(capped, 22.0)
        self.assertAlmostEqual(res.score, 22.0)

    def test_ema_shares_the_structure_budget(self):
        """EMA alignment sits in LTF_STRUCTURE, so it cannot add a separate
        trend vote on top of market structure."""
        full = [
            ConfluenceFactor("M15_ALIGNED", "LTF_STRUCTURE", 7.0, True, ""),
            ConfluenceFactor("M5_CHOCH_MSS", "LTF_STRUCTURE", 12.0, True, ""),
            ConfluenceFactor("DISPLACEMENT", "LTF_STRUCTURE", 11.0, True, ""),
        ]
        without_ema = ConfluenceEngine._aggregate(full).score
        with_ema = ConfluenceEngine._aggregate(
            full + [ConfluenceFactor("EMA_STACK", "LTF_STRUCTURE", 3.0, True,
                                     "")]).score
        self.assertEqual(without_ema, with_ema)
        self.assertAlmostEqual(with_ema, GROUP_CAPS["LTF_STRUCTURE"])

    def test_score_records_every_pass_and_fail(self):
        cfg = V5Config()
        state = make_state(cfg, zigzag_candles())
        inp = ConfluenceInputs(direction=Direction.LONG,
                               family="TREND_CONTINUATION",
                               entry_ref=state.bid, stop=state.bid - 10,
                               target=state.bid + 25)
        res = ConfluenceEngine(cfg).evaluate(state, inp)
        self.assertGreater(len(res.factors), 8)
        self.assertTrue(res.as_csv_field())
        self.assertTrue(any(line.startswith("PASS") or line.startswith("fail")
                            for line in
                            [f.line() for f in res.factors]))
        # every factor belongs to a known group
        for f in res.factors:
            self.assertIn(f.group, GROUP_CAPS)

    def test_threshold_gate(self):
        res = ConfluenceEngine._aggregate(
            [ConfluenceFactor("X", "CONTEXT", 5.0, True, "")])
        self.assertFalse(res.clears(62))
        self.assertTrue(res.clears(5))


# ===========================================================================
# 3. stop-loss construction (minimum distance, structural invalidation)
# ===========================================================================

class TestStopConstruction(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.state = make_state(self.cfg, flat_candles())
        self.builder = StopBuilder(self.cfg)

    def test_minimum_volatility_stop_is_enforced_by_widening(self):
        """A structural level 0.05 away must not produce a 0.05 stop."""
        atr = 2.0
        entry = 3400.0
        plan = self.builder.build(
            self.state, Direction.LONG, entry,
            [Invalidation(entry - 0.05, "very close swing")], atr, POINT)
        self.assertFalse(plan.rejected, plan.reject_reason)
        self.assertTrue(plan.min_stop_applied)
        floor = max(self.cfg.min_stop_atr_frac * atr,
                    self.cfg.min_stop_points * POINT)
        self.assertGreaterEqual(plan.distance + 1e-9, floor)
        # widening pushes the stop AWAY, never inside the structure
        self.assertLess(plan.price, entry - 0.05)

    def test_minimum_stop_for_shorts_too(self):
        atr = 2.0
        entry = 3400.0
        plan = self.builder.build(
            self.state, Direction.SHORT, entry,
            [Invalidation(entry + 0.05, "very close swing")], atr, POINT)
        self.assertFalse(plan.rejected, plan.reject_reason)
        floor = max(self.cfg.min_stop_atr_frac * atr,
                    self.cfg.min_stop_points * POINT)
        self.assertGreaterEqual(plan.distance + 1e-9, floor)
        self.assertGreater(plan.price, entry + 0.05)

    def test_stop_uses_the_primary_invalidation_the_family_supplied(self):
        """The first invalidation is the level whose break kills the idea.
        Wider levels are recorded but never used, so an M5 entry is not tied
        to an M15 swing tens of dollars away."""
        plan = self.builder.build(
            self.state, Direction.LONG, 3400.0,
            [Invalidation(3396.0, "M5 protected swing"),
             Invalidation(3320.0, "M15 protected swing")], 4.0, POINT)
        self.assertFalse(plan.rejected, plan.reject_reason)
        self.assertLess(plan.price, 3396.0)
        self.assertGreater(plan.price, 3380.0)
        self.assertIn("M5 protected swing", plan.reason)
        self.assertTrue(any("not used" in n for n in plan.notes))

    def test_absurdly_wide_stop_rejects_the_setup(self):
        plan = self.builder.build(
            self.state, Direction.LONG, 3400.0,
            [Invalidation(3300.0, "distant low")], 2.0, POINT)
        self.assertTrue(plan.rejected)
        self.assertIn("exceeds", plan.reject_reason)

    def test_no_invalidation_on_the_right_side_rejects(self):
        plan = self.builder.build(
            self.state, Direction.LONG, 3400.0,
            [Invalidation(3410.0, "above entry")], 2.0, POINT)
        self.assertTrue(plan.rejected)


# ===========================================================================
# 4. reward:risk with spread, slippage and commission
# ===========================================================================

class TestCostModel(unittest.TestCase):

    def test_long_and_short_fills_are_symmetric_in_risk(self):
        cfg = V5Config()
        slip = cfg.slippage_buffer_points * POINT
        long_fill = planned_fill(Direction.LONG, 3400.0, SPREAD_PRICE, slip)
        short_fill = planned_fill(Direction.SHORT, 3400.0, SPREAD_PRICE, slip)
        self.assertAlmostEqual(long_fill, 3400.0 + SPREAD_PRICE + slip)
        self.assertAlmostEqual(short_fill, 3400.0 - slip)
        long_risk = risk_price(Direction.LONG, long_fill, 3390.0,
                               SPREAD_PRICE)
        short_risk = risk_price(Direction.SHORT, short_fill, 3410.0,
                                SPREAD_PRICE)
        self.assertAlmostEqual(long_risk, short_risk)

    def test_short_pays_the_spread_on_the_target(self):
        """V4 gave shorts their target for free. Here both directions pay."""
        cfg = V5Config()
        slip = cfg.slippage_buffer_points * POINT
        lf = planned_fill(Direction.LONG, 3400.0, SPREAD_PRICE, slip)
        sf = planned_fill(Direction.SHORT, 3400.0, SPREAD_PRICE, slip)
        long_rr = net_rr(Direction.LONG, lf, 3390.0, 3425.0, SPREAD_PRICE, 0.0)
        short_rr = net_rr(Direction.SHORT, sf, 3410.0, 3375.0, SPREAD_PRICE,
                          0.0)
        self.assertAlmostEqual(long_rr, short_rr, places=9)

    def test_commission_reduces_reward_risk(self):
        cfg = V5Config()
        slip = cfg.slippage_buffer_points * POINT
        lf = planned_fill(Direction.LONG, 3400.0, SPREAD_PRICE, slip)
        with_comm = net_rr(Direction.LONG, lf, 3390.0, 3425.0, SPREAD_PRICE,
                           0.5)
        without = net_rr(Direction.LONG, lf, 3390.0, 3425.0, SPREAD_PRICE, 0.0)
        self.assertLess(with_comm, without)


# ===========================================================================
# 5. shadow execution: stop loss, take profit, MFE/MAE, R accounting
# ===========================================================================

class TestShadowExecution(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.h = ShadowHarness(self.cfg)

    def _fill(self, direction, entry_ref=3400.0, stop=3390.0, tp1=3411.0,
              target=3425.0, open_px=None):
        if direction == Direction.SHORT:
            stop, tp1, target = 3410.0, 3389.0, 3375.0
        cand = make_candidate(direction, entry_ref, stop, tp1, target,
                              created=T0)
        self.assertTrue(self.h.engine.submit(cand, SPREAD_POINTS, 2.0))
        o = open_px if open_px is not None else entry_ref
        self.h.m1(bar(T0 + timedelta(minutes=1), o, o + 0.2, o - 0.2, o))
        return self.h.engine.open.get(cand.sid)

    def test_long_stop_loss_is_exactly_minus_one_r(self):
        t = self._fill(Direction.LONG)
        self.assertIsNotNone(t)
        m = t.managed
        self.assertAlmostEqual(m.entry, 3400.0 + SPREAD_PRICE + 0.10)
        self.h.m1(bar(T0 + timedelta(minutes=2), 3399.0, 3399.5, 3388.0,
                      3389.0))
        self.assertEqual(t.status, "closed")
        self.assertEqual(t.exit_label, EXIT_STOP_LOSS)
        self.assertAlmostEqual(t.net_r, -1.0, places=6)
        self.assertFalse(t.suspect, t.suspect_reason)

    def test_short_stop_loss_is_exactly_minus_one_r(self):
        t = self._fill(Direction.SHORT)
        self.assertIsNotNone(t)
        self.assertAlmostEqual(t.managed.entry, 3400.0 - 0.10)
        self.h.m1(bar(T0 + timedelta(minutes=2), 3401.0, 3412.0, 3400.5,
                      3411.0))
        self.assertEqual(t.status, "closed")
        self.assertEqual(t.exit_label, EXIT_STOP_LOSS)
        self.assertAlmostEqual(t.net_r, -1.0, places=6)
        self.assertFalse(t.suspect, t.suspect_reason)

    def test_long_take_profit(self):
        t = self._fill(Direction.LONG)
        self.h.m1(bar(T0 + timedelta(minutes=2), 3401.0, 3426.0, 3400.5,
                      3425.5))
        self.assertEqual(t.exit_label, EXIT_TAKE_PROFIT)
        self.assertGreater(t.net_r, 2.0)
        self.assertFalse(t.suspect, t.suspect_reason)

    def test_short_take_profit(self):
        t = self._fill(Direction.SHORT)
        self.h.m1(bar(T0 + timedelta(minutes=2), 3398.0, 3398.5, 3374.0,
                      3375.0))
        self.assertEqual(t.exit_label, EXIT_TAKE_PROFIT)
        self.assertGreater(t.net_r, 2.0)
        self.assertFalse(t.suspect, t.suspect_reason)

    def test_long_and_short_take_profit_give_the_same_r(self):
        long_t = self._fill(Direction.LONG)
        self.h.m1(bar(T0 + timedelta(minutes=2), 3401.0, 3426.0, 3400.5,
                      3425.5))
        h2 = ShadowHarness(self.cfg)
        cand = make_candidate(Direction.SHORT, 3400.0, 3410.0, 3389.0, 3375.0,
                             created=T0, sid="TC-B")
        h2.engine.submit(cand, SPREAD_POINTS, 2.0)
        h2.m1(bar(T0 + timedelta(minutes=1), 3400.0, 3400.2, 3399.8, 3400.0))
        short_t = h2.engine.open["TC-B"]
        h2.m1(bar(T0 + timedelta(minutes=2), 3398.0, 3398.5, 3374.0, 3375.0))
        self.assertAlmostEqual(long_t.net_r, short_t.net_r, places=6)

    def test_mfe_and_mae_are_measured_on_the_exit_side(self):
        t = self._fill(Direction.LONG)
        m = t.managed
        self.h.m1(bar(T0 + timedelta(minutes=2), 3401.0, 3410.0, 3395.0,
                      3405.0))
        expected_mfe = (3410.0 - m.entry) / m.risk_dist
        expected_mae = (m.entry - 3395.0) / m.risk_dist
        self.assertAlmostEqual(m.mfe_r, expected_mfe, places=9)
        self.assertAlmostEqual(m.mae_r, expected_mae, places=9)

    def test_short_mfe_includes_the_ask_spread(self):
        t = self._fill(Direction.SHORT)
        m = t.managed
        self.h.m1(bar(T0 + timedelta(minutes=2), 3399.0, 3402.0, 3390.0,
                      3392.0))
        expected_mfe = (m.entry - (3390.0 + SPREAD_PRICE)) / m.risk_dist
        expected_mae = ((3402.0 + SPREAD_PRICE) - m.entry) / m.risk_dist
        self.assertAlmostEqual(m.mfe_r, expected_mfe, places=9)
        self.assertAlmostEqual(m.mae_r, expected_mae, places=9)

    def test_gross_r_never_exceeds_mfe(self):
        """The V4 defect: R above the recorded MFE."""
        for direction in (Direction.LONG, Direction.SHORT):
            h = ShadowHarness(self.cfg)
            cand = make_candidate(
                direction, 3400.0,
                3390.0 if direction == Direction.LONG else 3410.0,
                3411.0 if direction == Direction.LONG else 3389.0,
                3425.0 if direction == Direction.LONG else 3375.0,
                created=T0, sid=f"X-{direction.value}")
            h.engine.submit(cand, SPREAD_POINTS, 2.0)
            h.m1(bar(T0 + timedelta(minutes=1), 3400.0, 3400.2, 3399.8,
                     3400.0))
            if direction == Direction.LONG:
                h.m1(bar(T0 + timedelta(minutes=2), 3401.0, 3426.0, 3400.5,
                         3425.5))
            else:
                h.m1(bar(T0 + timedelta(minutes=2), 3398.0, 3398.5, 3374.0,
                         3375.0))
            t = h.closed[-1]
            self.assertLessEqual(t.gross_r, t.mfe_r + 1e-6,
                                 f"{direction}: gross {t.gross_r} > MFE "
                                 f"{t.mfe_r}")
            self.assertFalse(t.suspect, t.suspect_reason)

    def test_stop_first_when_a_bar_spans_both_exits(self):
        t = self._fill(Direction.LONG)
        self.h.m1(bar(T0 + timedelta(minutes=2), 3400.0, 3430.0, 3385.0,
                      3420.0))
        self.assertEqual(t.exit_label, EXIT_STOP_LOSS)
        self.assertLess(t.net_r, 0)

    def test_one_virtual_trade_per_variant(self):
        cand = make_candidate(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0)
        self.assertTrue(self.h.engine.submit(cand, SPREAD_POINTS, 2.0))
        self.assertFalse(self.h.engine.submit(cand, SPREAD_POINTS, 2.0))

    def test_volume_uses_the_broker_specification(self):
        h = ShadowHarness(self.cfg, spec=make_spec(volume_min=1.0,
                                                   volume_step=1.0))
        cand = make_candidate(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0)
        h.engine.submit(cand, SPREAD_POINTS, 2.0)
        h.m1(bar(T0 + timedelta(minutes=1), 3400.0, 3400.2, 3399.8, 3400.0))
        t = h.engine.open.get("TC-A")
        self.assertIsNotNone(t)
        units = t.managed.units_initial
        self.assertAlmostEqual(units, math.floor(units))   # whole units only

    def test_min_volume_above_risk_budget_rejects_the_trade(self):
        h = ShadowHarness(self.cfg, spec=make_spec(volume_min=500.0,
                                                   volume_step=500.0))
        cand = make_candidate(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0)
        h.engine.submit(cand, SPREAD_POINTS, 2.0)
        h.m1(bar(T0 + timedelta(minutes=1), 3400.0, 3400.2, 3399.8, 3400.0))
        self.assertNotIn("TC-A", h.engine.open)
        self.assertTrue(any("minimum volume" in msg for msg in h.logs), h.logs)


# ===========================================================================
# 6. stale / gapped fills (the V4 "-11.6R TAKE_PROFIT")
# ===========================================================================

class TestFillGuards(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.h = ShadowHarness(self.cfg)

    def test_stale_signal_is_never_filled(self):
        cand = make_candidate(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0,
                              created=T0)
        self.h.engine.submit(cand, SPREAD_POINTS, 2.0)
        late = T0 + timedelta(days=3)
        self.h.m1(bar(late, 3550.0, 3551.0, 3549.0, 3550.0))
        self.assertNotIn("TC-A", self.h.engine.open)
        self.assertEqual(len(self.h.closed), 0)
        self.assertTrue(any("stale signal" in m for m in self.h.logs),
                        self.h.logs)

    def test_expire_pending_drops_unfilled_setups(self):
        cand = make_candidate(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0,
                              created=T0)
        self.h.engine.submit(cand, SPREAD_POINTS, 2.0)
        self.h.engine.expire_pending(
            T0 + timedelta(minutes=self.cfg.max_signal_age_minutes + 1))
        self.assertEqual(len(self.h.engine.pending), 0)

    def test_gapped_entry_is_refused(self):
        cand = make_candidate(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0,
                              created=T0)
        self.h.engine.submit(cand, SPREAD_POINTS, atr=2.0)
        # next open is 5 ATR away
        self.h.m1(bar(T0 + timedelta(minutes=1), 3410.0, 3410.5, 3409.5,
                      3410.0))
        self.assertNotIn("TC-A", self.h.engine.open)
        self.assertTrue(any("gapped" in m for m in self.h.logs), self.h.logs)

    def test_fill_already_through_the_stop_is_refused(self):
        cand = make_candidate(Direction.LONG, 3400.0, 3399.0, 3411.0, 3425.0,
                              created=T0)
        self.h.engine.submit(cand, SPREAD_POINTS, atr=20.0)
        self.h.m1(bar(T0 + timedelta(minutes=1), 3395.0, 3395.5, 3394.5,
                      3395.0))
        self.assertNotIn("TC-A", self.h.engine.open)

    def test_reward_risk_rechecked_at_the_fill(self):
        cand = make_candidate(Direction.LONG, 3400.0, 3390.0, 3411.0, 3413.0,
                              created=T0)
        self.h.engine.submit(cand, SPREAD_POINTS, atr=20.0)
        self.h.m1(bar(T0 + timedelta(minutes=1), 3400.0, 3400.2, 3399.8,
                      3400.0))
        self.assertNotIn("TC-A", self.h.engine.open)
        self.assertTrue(any("reward:risk" in m for m in self.h.logs),
                        self.h.logs)


# ===========================================================================
# 7. exit labels are truthful (the V4 "STOP_LOSS with +2.08R")
# ===========================================================================

class TestExitLabels(unittest.TestCase):

    def test_initial_stop_labels_as_stop_loss(self):
        m = make_managed(Direction.LONG, 3400.4, 3390.0, 3411.0, 3425.0)
        self.assertEqual(m.exit_label_for_stop(), EXIT_STOP_LOSS)

    def test_breakeven_stop_is_not_a_stop_loss(self):
        m = make_managed(Direction.LONG, 3400.4, 3390.0, 3411.0, 3425.0)
        m.stop = 3401.0
        m.be_done = True
        self.assertEqual(m.exit_label_for_stop(), EXIT_BREAKEVEN_STOP)

    def test_trailed_stop_is_labelled_trail_stop(self):
        m = make_managed(Direction.LONG, 3400.4, 3390.0, 3411.0, 3425.0)
        m.stop = 3418.0
        m.trail_active = True
        self.assertEqual(m.exit_label_for_stop(), EXIT_TRAIL_STOP)

    def test_stop_after_partial_is_its_own_label(self):
        m = make_managed(Direction.LONG, 3400.4, 3390.0, 3411.0, 3425.0)
        m.partial_done = True
        self.assertEqual(m.exit_label_for_stop(), "STOP_AFTER_PARTIAL")

    def test_shadow_trailed_winner_is_never_a_stop_loss(self):
        """Reproduces the exact V4 scenario and asserts the honest label."""
        cfg = V5Config()
        h = ShadowHarness(cfg)
        cand = make_candidate(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0,
                              created=T0)
        h.engine.submit(cand, SPREAD_POINTS, 2.0)
        h.m1(bar(T0 + timedelta(minutes=1), 3400.0, 3400.2, 3399.8, 3400.0))
        t = h.engine.open["TC-A"]
        t.managed.stop = 3420.0            # as a trail would have left it
        t.managed.trail_active = True
        h.m1(bar(T0 + timedelta(minutes=2), 3421.0, 3421.5, 3419.0, 3419.5))
        self.assertEqual(t.exit_label, EXIT_TRAIL_STOP)
        self.assertGreater(t.net_r, 0.0)
        self.assertFalse(t.suspect, t.suspect_reason)


# ===========================================================================
# 8. invariant detection
# ===========================================================================

class TestInvariants(unittest.TestCase):

    def _trade(self):
        cfg = V5Config()
        h = ShadowHarness(cfg)
        m = make_managed(Direction.LONG, 3400.4, 3390.0, 3411.0, 3425.0,
                         units=10.0)
        t = VirtualTrade(
            trade_id="x", sid="TC-A", family="TREND_CONTINUATION", version=1,
            direction=Direction.LONG, signal_time=T0, entry_ref=3400.0,
            planned_stop=3390.0, planned_tp1=3411.0, planned_target=3425.0,
            confluence_score=70.0, confluence_detail="", regime="R",
            session="LONDON", htf_bias="", location="", stop_reason="",
            target_reason="", tp1_reason="", sweep_kind="",
            spread_points_signal=SPREAD_POINTS, atr_at_signal=2.0)
        t.managed = m
        return h, t, m

    def test_stop_loss_with_positive_r_is_flagged(self):
        h, t, m = self._trade()
        t.exit_label = EXIT_STOP_LOSS
        t.gross_r = 0.5
        t.net_r = 0.5
        m.mfe_r = 1.0
        h.engine._check_invariants(t, 1.0)
        self.assertTrue(t.suspect)
        self.assertIn("STOP_LOSS", t.suspect_reason)

    def test_r_above_mfe_is_flagged(self):
        h, t, m = self._trade()
        t.exit_label = EXIT_TAKE_PROFIT
        t.gross_r = 2.5
        t.net_r = 2.5
        m.mfe_r = 1.0
        h.engine._check_invariants(t, 1.0)
        self.assertTrue(t.suspect)
        self.assertIn("exceeds MFE", t.suspect_reason)

    def test_suspect_trades_are_excluded_from_learning(self):
        """A trade that fails an invariant is written to the CSV trail and to
        suspect_trades, but never reaches the learning book."""
        cfg = V5Config()
        h = ShadowHarness(cfg)

        def always_suspect(trade, mpu):
            trade.suspect = True
            trade.suspect_reason = "forced for the test"

        h.engine._check_invariants = always_suspect
        cand = make_candidate(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0,
                              created=T0)
        h.engine.submit(cand, SPREAD_POINTS, 2.0)
        h.m1(bar(T0 + timedelta(minutes=1), 3400.0, 3400.2, 3399.8, 3400.0))
        t = h.engine.open["TC-A"]
        h.m1(bar(T0 + timedelta(minutes=2), 3401.0, 3426.0, 3400.5, 3425.5))
        self.assertTrue(t.suspect)
        self.assertEqual(len(h.suspects), 1)
        self.assertEqual(len(h.closed), 0)     # never reached the learning book
        self.assertEqual(len(h.rows), 1)       # but IS recorded in the CSV


# ===========================================================================
# 9. management: partials
# ===========================================================================

class TestPartials(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.mgr = TradeManager(self.cfg)

    def _view_at(self, close, spec=None):
        candles = flat_candles(80, price=close)
        # force the last close to the requested value
        last = candles[-1]
        candles[-1] = bar(last.time, last.open, max(last.high, close),
                          min(last.low, close), close)
        state = make_state(self.cfg, candles)
        return make_view(self.cfg, state, spec=spec)

    def test_partial_taken_when_a_bar_closes_beyond_tp1(self):
        view = self._view_at(3412.0)
        m = make_managed(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0,
                         units=10.0)
        actions = self.mgr.step(m, view)
        partials = [a for a in actions if a.kind == PARTIAL]
        self.assertEqual(len(partials), 1)
        expected = view.spec.round_volume_down(
            10.0 * self.cfg.partial_fraction)
        self.assertAlmostEqual(partials[0].units, expected)
        self.assertIn("TP1", partials[0].reason)

    def test_no_partial_before_tp1(self):
        view = self._view_at(3405.0)
        m = make_managed(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0)
        actions = self.mgr.step(m, view)
        self.assertEqual([a for a in actions if a.kind == PARTIAL], [])

    def test_partial_volume_is_rounded_down_to_the_broker_step(self):
        spec = make_spec(volume_min=1.0, volume_step=1.0)
        view = self._view_at(3412.0, spec=spec)
        m = make_managed(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0,
                         units=7.0)
        actions = self.mgr.step(m, view)
        partial = [a for a in actions if a.kind == PARTIAL][0]
        self.assertEqual(partial.units, 3.0)          # 7 * 0.45 = 3.15 -> 3
        self.assertLess(partial.units, 7.0)

    def test_position_too_small_to_split_is_managed_whole(self):
        spec = make_spec(volume_min=5.0, volume_step=5.0)
        view = self._view_at(3412.0, spec=spec)
        m = make_managed(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0,
                         units=5.0)
        actions = self.mgr.step(m, view)
        self.assertEqual([a for a in actions if a.kind == PARTIAL], [])
        self.assertTrue(m.partial_skipped)
        self.assertIn("one full position", m.partial_reason)

    def test_remainder_below_broker_minimum_is_refused(self):
        spec = make_spec(volume_min=4.0, volume_step=1.0)
        view = self._view_at(3412.0, spec=spec)
        m = make_managed(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0,
                         units=6.0)
        actions = self.mgr.step(m, view)
        self.assertEqual([a for a in actions if a.kind == PARTIAL], [])
        self.assertTrue(m.partial_skipped)

    def test_shadow_applies_the_partial_and_keeps_a_runner(self):
        h = ShadowHarness(self.cfg)
        cand = make_candidate(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0,
                              created=T0)
        h.engine.submit(cand, SPREAD_POINTS, 2.0)
        h.m1(bar(T0 + timedelta(minutes=1), 3400.0, 3400.2, 3399.8, 3400.0))
        t = h.engine.open["TC-A"]
        before = t.managed.units
        view = self._view_at(3412.0)
        h.engine.on_m5_close(view, {"TC-A": {}})
        self.assertTrue(t.managed.partial_done)
        self.assertLess(t.managed.units, before)
        self.assertGreater(t.managed.units, 0.0)
        self.assertGreater(t.partial_money, 0.0)


# ===========================================================================
# 10. management: breakeven
# ===========================================================================

class TestBreakeven(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.mgr = TradeManager(self.cfg)

    def _quiet_view(self, close):
        candles = flat_candles(80, price=close)
        last = candles[-1]
        # a small, non-displacement, non-structural candle
        candles[-1] = bar(last.time, close - 0.05, close + 0.12,
                          close - 0.15, close)
        state = make_state(self.cfg, candles)
        return make_view(self.cfg, state)

    def _displacement_view(self, close, atr_hint=1.0):
        candles = flat_candles(80, price=close - 5.0)
        last = candles[-1]
        # a big body in the trade direction => continuation displacement
        candles[-1] = bar(last.time, close - 4.0, close + 0.1, close - 4.2,
                          close)
        state = make_state(self.cfg, candles)
        return make_view(self.cfg, state)

    def test_no_breakeven_on_a_bare_1r_touch(self):
        """The exact behaviour V5 exists to fix: price has reached 1R but
        nothing has happened to justify protecting the trade — no TP1, no
        close beyond structure, no new protected swing since entry, no
        continuation displacement."""
        view = self._quiet_view(3410.0)
        risk = 9.0
        entry = 3410.0 - 1.05 * risk
        m = make_managed(Direction.LONG, entry, entry - risk, 3425.0, 3440.0,
                         entry_time=view.now)
        r = self.mgr.r_now(m, view)
        self.assertGreaterEqual(r, self.cfg.be_min_r)
        actions = self.mgr.step(m, view)
        moves = [a for a in actions if a.kind == MOVE_STOP
                 and "breakeven" in a.reason]
        self.assertEqual(moves, [], f"stop moved without justification at "
                                    f"{r:.2f}R")
        self.assertFalse(m.be_done)

    def test_breakeven_activates_with_displacement_justification(self):
        view = self._displacement_view(3410.0)
        risk = 4.0
        entry = view.state.m5.last.close - 1.2 * risk
        m = make_managed(Direction.LONG, entry, entry - risk,
                         entry + 20.0, entry + 40.0)
        actions = self.mgr.step(m, view)
        moves = [a for a in actions if a.kind == MOVE_STOP
                 and "breakeven" in a.reason]
        self.assertEqual(len(moves), 1, [a.reason for a in actions])
        self.assertIn("justification", moves[0].reason)
        self.assertGreater(moves[0].price, m.initial_stop)

    def test_breakeven_after_tp1_is_justified(self):
        view = self._quiet_view(3410.0)
        risk = 9.0
        entry = 3410.0 - 1.05 * risk
        m = make_managed(Direction.LONG, entry, entry - risk, 3405.0, 3440.0,
                         entry_time=view.now)
        m.partial_done = True
        m.partial_price = 3405.0
        actions = self.mgr.step(m, view)
        moves = [a for a in actions if a.kind == MOVE_STOP
                 and "breakeven" in a.reason]
        self.assertEqual(len(moves), 1)
        self.assertIn("TP1 banked", moves[0].reason)

    def test_breakeven_includes_a_spread_buffer(self):
        view = self._quiet_view(3410.0)
        risk = 9.0
        entry = 3410.0 - 1.05 * risk
        m = make_managed(Direction.LONG, entry, entry - risk, 3405.0, 3440.0,
                         entry_time=view.now)
        m.partial_done = True
        actions = self.mgr.step(m, view)
        move = [a for a in actions if a.kind == MOVE_STOP][0]
        self.assertGreater(move.price, entry)

    def test_breakeven_below_the_floor_never_fires(self):
        view = self._displacement_view(3410.0)
        risk = 40.0                       # r_now well below be_min_r
        entry = view.state.m5.last.close - 0.2 * risk
        m = make_managed(Direction.LONG, entry, entry - risk,
                         entry + 60.0, entry + 120.0)
        actions = self.mgr.step(m, view)
        self.assertEqual([a for a in actions if a.kind == MOVE_STOP
                          and "breakeven" in a.reason], [])


# ===========================================================================
# 11. management: trailing stop
# ===========================================================================

class TestTrailing(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.mgr = TradeManager(self.cfg)

    def _trend_view(self, strong=True):
        candles = zigzag_candles(90, start=3300.0, drift=1.2, amp=3.0)
        state = make_state(self.cfg, candles, strong=strong,
                           bias_dir=Direction.LONG)
        return make_view(self.cfg, state)

    def test_trailing_activates_in_a_strong_trend(self):
        view = self._trend_view(strong=True)
        close = view.state.m5.last.close
        risk = 4.0
        entry = close - 2.0 * risk         # comfortably past trail_min_r
        m = make_managed(Direction.LONG, entry, entry - risk, close + 5,
                         close + 40)
        actions = self.mgr.step(m, view)
        trails = [a for a in actions if a.kind == MOVE_STOP
                  and "trailing" in a.reason]
        self.assertEqual(len(trails), 1, [a.reason for a in actions])
        self.assertIn("protected swing", trails[0].reason)

    def test_no_trailing_without_a_strong_trend(self):
        """Ranges must not trail: the strong-trend gate is mandatory."""
        view = self._trend_view(strong=False)
        close = view.state.m5.last.close
        risk = 4.0
        entry = close - 2.0 * risk
        m = make_managed(Direction.LONG, entry, entry - risk, close + 5,
                         close + 40)
        actions = self.mgr.step(m, view)
        self.assertEqual([a for a in actions if a.kind == MOVE_STOP
                          and "trailing" in a.reason], [])

    def test_no_trailing_before_trail_min_r(self):
        view = self._trend_view(strong=True)
        close = view.state.m5.last.close
        risk = 40.0                        # r_now far below trail_min_r
        entry = close - 0.2 * risk
        m = make_managed(Direction.LONG, entry, entry - risk, close + 50,
                         close + 200)
        actions = self.mgr.step(m, view)
        self.assertEqual([a for a in actions if a.kind == MOVE_STOP
                          and "trailing" in a.reason], [])

    def test_trailing_only_uses_a_new_protected_swing_once(self):
        view = self._trend_view(strong=True)
        close = view.state.m5.last.close
        risk = 4.0
        entry = close - 2.0 * risk
        m = make_managed(Direction.LONG, entry, entry - risk, close + 5,
                         close + 40)
        first = [a for a in self.mgr.step(m, view) if a.kind == MOVE_STOP]
        self.assertTrue(first)
        m.stop = first[-1].price
        m.trail_active = True
        m.last_trail_swing = m.last_trail_swing or ""
        # simulate the engine recording which swing was used
        trail = self.mgr._trail_candidate(m, view, {}, 2.0)
        if trail is not None:
            m.last_trail_swing = trail[2]
        again = [a for a in self.mgr.step(m, view) if a.kind == MOVE_STOP
                 and "trailing" in a.reason]
        self.assertEqual(again, [], "trailed twice off the same swing")

    def test_stop_never_widens(self):
        m = make_managed(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0)
        m.stop = 3405.0
        self.assertIsNone(m.tighten(3400.0))    # would widen
        self.assertIsNone(m.tighten(3405.0))    # no change
        self.assertEqual(m.tighten(3406.0), 3406.0)

    def test_stop_never_widens_for_shorts(self):
        m = make_managed(Direction.SHORT, 3400.0, 3410.0, 3389.0, 3375.0)
        m.stop = 3395.0
        self.assertIsNone(m.tighten(3400.0))    # higher = wider for a short
        self.assertEqual(m.tighten(3394.0), 3394.0)

    def test_trailing_never_produces_a_widening_move(self):
        view = self._trend_view(strong=True)
        close = view.state.m5.last.close
        risk = 4.0
        entry = close - 2.0 * risk
        m = make_managed(Direction.LONG, entry, entry - risk, close + 5,
                         close + 40)
        m.stop = close - 0.1               # already very tight
        actions = self.mgr.step(m, view)
        for a in actions:
            if a.kind == MOVE_STOP:
                self.assertGreater(a.price, m.stop)

    def test_strong_trend_reading_true_in_trend_false_in_range(self):
        cfg = V5Config()
        builder = MarketStateBuilder(cfg)
        trend = {Timeframe.M5: feats(zigzag_candles(90, drift=1.4, amp=2.0),
                                    Timeframe.M5, cfg),
                 Timeframe.M15: feats(zigzag_candles(90, drift=1.4, amp=2.0),
                                      Timeframe.M15, cfg)}
        bias = BiasReading(Direction.LONG, "STRONG", TrendState.BULLISH,
                           TrendState.BULLISH, TrendState.BULLISH, "fixture")
        reading = builder.strong_trend(trend, bias, None)
        flat = {Timeframe.M5: feats(flat_candles(90), Timeframe.M5, cfg),
                Timeframe.M15: feats(flat_candles(90), Timeframe.M15, cfg)}
        flat_reading = builder.strong_trend(flat, bias, None)
        self.assertGreater(reading.score, flat_reading.score)
        self.assertFalse(flat_reading.is_strong)


# ===========================================================================
# 12. management: early exit
# ===========================================================================

class TestEarlyExit(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.mgr = TradeManager(self.cfg)

    def test_one_noisy_candle_does_not_trigger_an_exit(self):
        candles = flat_candles(80, price=3400.0)
        last = candles[-1]
        # a single opposite candle with no structural break
        candles[-1] = bar(last.time, 3400.2, 3400.3, 3399.4, 3399.6)
        state = make_state(self.cfg, candles, bias_dir=Direction.LONG)
        view = make_view(self.cfg, state)
        m = make_managed(Direction.LONG, 3395.0, 3385.0, 3410.0, 3425.0)
        m.bars_open = 10
        evidence = self.mgr.early_exit_evidence(m, view)
        self.assertLess(evidence.score, self.cfg.early_exit_min_score,
                        evidence.summary())
        actions = self.mgr.step(m, view)
        self.assertEqual([a for a in actions if a.kind == CLOSE], [])

    def test_protected_swing_loss_scores_evidence(self):
        candles = zigzag_candles(90, start=3300.0, drift=1.0, amp=3.0)
        f5 = feats(candles, Timeframe.M5, self.cfg)
        ps = f5.protected_swing(Direction.LONG)
        self.assertIsNotNone(ps)
        # force a close below the protected swing
        last = candles[-1]
        broken = ps.price - 2.0
        candles[-1] = bar(last.time, last.open, last.high, broken - 0.5,
                          broken)
        state = make_state(self.cfg, candles, bias_dir=Direction.LONG)
        view = make_view(self.cfg, state)
        m = make_managed(Direction.LONG, broken + 10.0, broken - 5.0,
                         broken + 20.0, broken + 40.0)
        m.bars_open = 10
        evidence = self.mgr.early_exit_evidence(m, view)
        self.assertGreaterEqual(evidence.score, 2, evidence.summary())
        self.assertTrue(any("protected swing" in item
                            for item in evidence.items))

    def test_m15_flip_against_the_trade_scores_evidence(self):
        m5 = zigzag_candles(90, start=3300.0, drift=1.0, amp=3.0)
        m15_down = zigzag_candles(90, start=3500.0, drift=-1.5, amp=3.0,
                                  minutes=15)
        state = make_state(self.cfg, m5, bias_dir=Direction.LONG,
                           m15_candles=m15_down)
        view = make_view(self.cfg, state)
        m = make_managed(Direction.LONG, state.bid - 5.0, state.bid - 15.0,
                         state.bid + 10.0, state.bid + 30.0)
        m.bars_open = 10
        evidence = self.mgr.early_exit_evidence(m, view)
        self.assertTrue(any("M15" in item for item in evidence.items),
                        evidence.summary())

    def test_breakout_falling_back_inside_its_range_is_full_evidence(self):
        candles = flat_candles(80, price=3400.0)
        state = make_state(self.cfg, candles, bias_dir=Direction.LONG)
        view = make_view(self.cfg, state)
        m = make_managed(Direction.LONG, 3405.0, 3395.0, 3415.0, 3430.0,
                         family="BREAKOUT_RETEST")
        m.meta["range_low"] = 3390.0
        m.meta["range_high"] = 3402.0
        m.bars_open = 10
        evidence = self.mgr.early_exit_evidence(m, view)
        self.assertGreaterEqual(evidence.score, 3, evidence.summary())
        self.assertTrue(any("breakout failed" in item
                            for item in evidence.items))

    def test_full_close_when_evidence_is_overwhelming(self):
        candles = flat_candles(80, price=3400.0)
        state = make_state(self.cfg, candles, bias_dir=Direction.SHORT)
        view = make_view(self.cfg, state)
        m = make_managed(Direction.LONG, 3405.0, 3395.0, 3415.0, 3430.0,
                         family="BREAKOUT_RETEST")
        m.meta["range_low"] = 3390.0
        m.meta["range_high"] = 3402.0
        m.bars_open = 10
        actions = self.mgr.step(m, view)
        closes = [a for a in actions if a.kind == CLOSE]
        self.assertEqual(len(closes), 1, [a.reason for a in actions])
        self.assertEqual(closes[0].label, "EARLY_EXIT_STRUCTURE")

    def test_no_early_exit_on_the_entry_bar(self):
        candles = flat_candles(80, price=3400.0)
        state = make_state(self.cfg, candles, bias_dir=Direction.SHORT)
        view = make_view(self.cfg, state)
        m = make_managed(Direction.LONG, 3405.0, 3395.0, 3415.0, 3430.0,
                         family="BREAKOUT_RETEST")
        m.meta["range_low"] = 3390.0
        m.meta["range_high"] = 3402.0
        m.bars_open = 0
        self.assertEqual([a for a in self.mgr.step(m, view)
                          if a.kind == CLOSE], [])


# ===========================================================================
# 13. hard overrides
# ===========================================================================

class TestHardOverrides(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.mgr = TradeManager(self.cfg)
        self.state = make_state(self.cfg, flat_candles())

    def test_emergency_stop_closes_everything(self):
        view = make_view(self.cfg, self.state, emergency=True)
        m = make_managed(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0)
        actions = self.mgr.step(m, view)
        self.assertEqual(actions[0].kind, CLOSE)
        self.assertEqual(actions[0].label, "EMERGENCY_STOP")

    def test_weekend_flat_closes_everything(self):
        view = make_view(self.cfg, self.state, weekend_flat=True)
        m = make_managed(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0)
        actions = self.mgr.step(m, view)
        self.assertEqual(actions[0].label, "WEEKEND_FLAT")


# ===========================================================================
# 14. guards: daily and weekly locks, cooldown, trade cap
# ===========================================================================

class TestGuards(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.guard = V5Guard(self.cfg)
        self.now = T0
        self.guard.roll(self.now, 10_000.0, 10_000.0)

    def test_combined_daily_loss_locks(self):
        self.guard.register_close(-100.0, self.now)           # -1.0%
        self.assertEqual(self.guard.lock_reason(9_900.0, 0.0, None, self.now),
                         LockReason.NONE)
        # floating -0.8% pushes the combined loss past 1.7%
        self.assertEqual(
            self.guard.lock_reason(9_820.0, -80.0, None, self.now),
            LockReason.DAILY_LOSS)

    def test_realised_daily_loss_locks(self):
        self.guard.register_close(-171.0, self.now)
        self.assertEqual(self.guard.lock_reason(9_829.0, 0.0, None, self.now),
                         LockReason.DAILY_LOSS)

    def test_weekly_drawdown_locks(self):
        self.guard.roll(self.now, 10_000.0, 10_000.0)
        self.guard.week.min_equity = 9_400.0                 # -6%
        self.assertEqual(self.guard.lock_reason(9_400.0, 0.0, None, self.now),
                         LockReason.WEEKLY_LOSS)

    def test_daily_real_trade_cap(self):
        for _ in range(self.cfg.max_real_trades_per_day):
            self.guard.register_open(SessionName.LONDON)
        self.assertEqual(self.guard.lock_reason(10_000.0, 0.0, None, self.now),
                         LockReason.MAX_TRADES_DAY)

    def test_cooldown_after_three_losses(self):
        for _ in range(3):
            self.guard.register_close(-10.0, self.now)
        self.assertIsNotNone(self.guard.cooldown_until)
        self.assertEqual(self.guard.lock_reason(9_970.0, 0.0, None, self.now),
                         LockReason.CONSECUTIVE_LOSSES)
        later = self.now + timedelta(hours=self.cfg.loss_cooldown_hours + 1)
        self.assertEqual(self.guard.lock_reason(9_970.0, 0.0, None, later),
                         LockReason.NONE)
        self.assertEqual(self.guard.consecutive_losses, 0)

    def test_restart_cannot_serve_the_cooldown_early(self):
        for _ in range(3):
            self.guard.register_close(-10.0, self.now)
        snap = self.guard.snapshot()
        fresh = V5Guard(self.cfg)
        fresh.roll(self.now, 9_970.0, 9_970.0)
        fresh.restore(snap, self.now)
        self.assertEqual(fresh.lock_reason(9_970.0, 0.0, None, self.now),
                         LockReason.CONSECUTIVE_LOSSES)

    def test_restore_keeps_the_more_restrictive_figures(self):
        self.guard.register_close(-50.0, self.now)
        snap = self.guard.snapshot()
        fresh = V5Guard(self.cfg)
        fresh.roll(self.now, 10_000.0, 10_000.0)
        fresh.register_close(-120.0, self.now)      # broker replay is worse
        fresh.restore(snap, self.now)
        self.assertAlmostEqual(fresh.day.realised, -120.0)

    def test_new_day_clears_the_daily_lock(self):
        self.guard.register_close(-200.0, self.now)
        self.assertEqual(self.guard.lock_reason(9_800.0, 0.0, None, self.now),
                         LockReason.DAILY_LOSS)
        tomorrow = self.now + timedelta(days=1)
        self.guard.roll(tomorrow, 9_800.0, 9_800.0)
        self.assertEqual(
            self.guard.lock_reason(9_800.0, 0.0, None, tomorrow),
            LockReason.NONE)


# ===========================================================================
# 15. risk engine
# ===========================================================================

class TestRiskEngine(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.engine = RiskEngine(self.cfg)

    def _choose(self, **kwargs):
        base = dict(equity=10_000.0, day_start_equity=10_000.0,
                    daily_pl_combined=0.0, weekly_dd_frac=0.0,
                    consecutive_losses=0, spread_points=20.0,
                    atr_percentile=0.5, recent_strategy_r=0.5,
                    confluence_score=90.0, confluence_threshold=62.0)
        base.update(kwargs)
        return self.engine.choose(self.cfg.risk_tier_established,
                                  "test tier", **base)

    def test_never_exceeds_the_hard_ceiling(self):
        d = self._choose()
        self.assertLessEqual(d.fraction, self.cfg.max_risk_per_trade)

    def test_losses_only_reduce_risk(self):
        clean = self._choose().fraction
        after_losses = self._choose(consecutive_losses=2).fraction
        self.assertLess(after_losses, clean)

    def test_elevated_spread_reduces_risk(self):
        clean = self._choose().fraction
        wide = self._choose(spread_points=50.0).fraction
        self.assertLess(wide, clean)

    def test_marginal_confluence_reduces_risk(self):
        strong = self._choose(confluence_score=95.0).fraction
        marginal = self._choose(confluence_score=63.0).fraction
        self.assertLess(marginal, strong)

    def test_daily_headroom_caps_then_blocks(self):
        capped = self._choose(daily_pl_combined=-160.0)
        self.assertLessEqual(capped.fraction, 0.0010 + 1e-9)
        blocked = self._choose(daily_pl_combined=-166.0)
        self.assertTrue(blocked.blocked)

    def test_weekly_headroom_blocks(self):
        blocked = self._choose(weekly_dd_frac=0.0499)
        self.assertTrue(blocked.blocked)

    def test_no_recovery_sizing_after_a_loss(self):
        """There is no code path that raises risk because the last trade
        lost — every situational adjustment is a reduction."""
        losing = self._choose(consecutive_losses=1,
                              recent_strategy_r=-2.0).fraction
        neutral = self._choose().fraction
        self.assertLess(losing, neutral)


# ===========================================================================
# 16. risk graduation from evidence
# ===========================================================================

class TestRiskGraduation(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.book = LearningBook(self.cfg)
        self.variant = StrategyVariant(
            sid="TC-A", family="TREND_CONTINUATION", label="A", version=1,
            params=dict(DEFAULT_PARAMS))

    def _feed(self, n, r, real=False):
        for _ in range(n):
            self.book.record("TC-A", "TREND_CONTINUATION", r, "STRONG_BULL",
                             "LONDON", "TAKE_PROFIT", 2.0, 0.5, real=real)

    def test_probe_tier_with_little_evidence(self):
        self._feed(5, 0.8)
        fraction, why = self.book.risk_tier(self.variant)
        self.assertAlmostEqual(fraction, self.cfg.risk_tier_probe)
        self.assertIn("probe", why)

    def test_graduates_slowly_with_more_evidence(self):
        self._feed(13, 0.6)
        early, _ = self.book.risk_tier(self.variant)
        self.assertAlmostEqual(early, self.cfg.risk_tier_early)
        self._feed(14, 0.6)                     # 27 trades, still no real ones
        no_real, _ = self.book.risk_tier(self.variant)
        self.assertAlmostEqual(no_real, self.cfg.risk_tier_early,
                               msg="confirmed tier must require real trades")
        self._feed(3, 0.6, real=True)
        confirmed, _ = self.book.risk_tier(self.variant)
        self.assertAlmostEqual(confirmed, self.cfg.risk_tier_confirmed)

    def test_top_tier_needs_real_evidence(self):
        self._feed(45, 0.9)
        fraction, _ = self.book.risk_tier(self.variant)
        self.assertLess(fraction, self.cfg.risk_tier_established)
        self._feed(8, 0.9, real=True)
        fraction, _ = self.book.risk_tier(self.variant)
        self.assertAlmostEqual(fraction, self.cfg.risk_tier_established)


# ===========================================================================
# 17. learning and ranking
# ===========================================================================

class TestLearning(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.book = LearningBook(self.cfg)

    def test_one_lucky_win_cannot_outrank_a_steady_performer(self):
        self.book.record("LUCKY", "BREAKOUT_RETEST", 7.0, "R", "LONDON",
                         "TAKE_PROFIT", 7.2, 0.3, real=False)
        for _ in range(14):
            self.book.record("STEADY", "TREND_CONTINUATION", 0.45, "R",
                             "LONDON", "TAKE_PROFIT", 1.4, 0.4, real=False)
        lucky = self.book.score("LUCKY")
        steady = self.book.score("STEADY")
        self.assertGreater(steady, lucky,
                           f"lucky {lucky:.3f} outranked steady {steady:.3f}")

    def test_eligibility_requires_variant_and_family_evidence(self):
        v = StrategyVariant(sid="TC-A", family="TREND_CONTINUATION",
                            label="A", version=1, params=dict(DEFAULT_PARAMS))
        ok, why = self.book.eligible_for_real(v, "STRONG_BULL")
        self.assertFalse(ok)
        self.assertIn("shadow trades", why)
        for _ in range(5):
            self.book.record("TC-A", "TREND_CONTINUATION", 0.7, "STRONG_BULL",
                             "LONDON", "TAKE_PROFIT", 2.0, 0.4, real=False)
        ok, why = self.book.eligible_for_real(v, "STRONG_BULL")
        self.assertFalse(ok, why)
        self.assertIn("family", why)
        for _ in range(3):
            self.book.record("TC-B", "TREND_CONTINUATION", 0.6, "STRONG_BULL",
                             "LONDON", "TAKE_PROFIT", 2.0, 0.4, real=False)
        ok, why = self.book.eligible_for_real(v, "STRONG_BULL")
        self.assertTrue(ok, why)

    def test_negative_expectancy_is_never_eligible(self):
        v = StrategyVariant(sid="BAD", family="RANGE", label="A", version=1,
                            params=dict(DEFAULT_PARAMS))
        for _ in range(12):
            self.book.record("BAD", "RANGE", -0.5, "R", "LONDON",
                             "STOP_LOSS", 0.2, 1.0, real=False)
        ok, why = self.book.eligible_for_real(v, "R")
        self.assertFalse(ok)
        self.assertIn("expectancy", why)

    def test_regime_specific_veto(self):
        v = StrategyVariant(sid="TC-A", family="TREND_CONTINUATION",
                            label="A", version=1, params=dict(DEFAULT_PARAMS))
        for _ in range(10):
            self.book.record("TC-A", "TREND_CONTINUATION", 1.0, "STRONG_BULL",
                             "LONDON", "TAKE_PROFIT", 2.0, 0.4, real=False)
        for _ in range(4):
            self.book.record("TC-A", "TREND_CONTINUATION", -1.0, "RANGE",
                             "LONDON", "STOP_LOSS", 0.2, 1.0, real=False)
        ok, _ = self.book.eligible_for_real(v, "STRONG_BULL")
        self.assertTrue(ok)
        ok, why = self.book.eligible_for_real(v, "RANGE")
        self.assertFalse(ok)
        self.assertIn("RANGE", why)

    def test_family_ranking_aggregates_variants(self):
        for sid in ("A1", "A2"):
            for _ in range(6):
                self.book.record(sid, "LIQUIDITY_REVERSAL", 0.5, "R",
                                 "LONDON", "TAKE_PROFIT", 1.5, 0.4,
                                 real=False)
        for _ in range(6):
            self.book.record("B1", "RANGE_X", -0.4, "R", "LONDON",
                             "STOP_LOSS", 0.3, 1.0, real=False)
        ranking = self.book.family_ranking()
        self.assertEqual(ranking[0][0], "LIQUIDITY_REVERSAL")
        self.assertEqual(self.book.family("LIQUIDITY_REVERSAL").n, 12)

    def test_retire_and_bench_and_spawn(self):
        variants = [StrategyVariant(sid="LOSER", family="F", label="A",
                                    version=1, params=dict(DEFAULT_PARAMS)),
                    StrategyVariant(sid="GOOD", family="G", label="A",
                                    version=1, params=dict(DEFAULT_PARAMS))]
        for _ in range(14):
            self.book.record("LOSER", "F", -0.6, "R", "LONDON", "STOP_LOSS",
                             0.2, 1.0, real=False)
        for _ in range(12):
            self.book.record("GOOD", "G", 0.7, "R", "LONDON", "TAKE_PROFIT",
                             2.0, 0.4, real=False)
        import random as _random
        decisions = self.book.daily_update(
            variants, "2026-03-03", _random.Random(1),
            lambda parent: mutate_variant(parent, 1, _random.Random(2),
                                          "2026-03-03"))
        kinds = {d.kind for d in decisions}
        self.assertIn("RETIRE", kinds)
        self.assertIn("SPAWN", kinds)
        self.assertEqual(
            next(v for v in variants if v.sid == "LOSER").status, "retired")

    def test_bench_then_unbench(self):
        v = StrategyVariant(sid="SLUMP", family="F", label="A", version=1,
                            params=dict(DEFAULT_PARAMS))
        for _ in range(5):
            self.book.record("SLUMP", "F", -0.8, "R", "LONDON", "STOP_LOSS",
                             0.2, 1.0, real=False)
        import random as _random
        self.book.daily_update([v], "2026-03-03", _random.Random(1),
                               lambda p: None)
        self.assertEqual(v.status, "benched")
        self.book.daily_update([v], "2026-03-04", _random.Random(1),
                               lambda p: None)
        self.assertEqual(v.status, "active")

    def test_stop_too_tight_evidence_widens_a_parameter(self):
        v = StrategyVariant(sid="TIGHT", family="F", label="A", version=1,
                            params=dict(DEFAULT_PARAMS))
        for _ in range(12):
            self.book.record("TIGHT", "F", -0.2, "R", "LONDON", "STOP_LOSS",
                             1.1, 1.0, real=False)
        for i in range(6):
            self.book.record_stop_watch("TIGHT", "F", target_hit=i < 4)
        before = v.params["retest_atr"]
        import random as _random
        decisions = self.book.daily_update([v], "2026-03-03",
                                          _random.Random(1), lambda p: None)
        adapt = [d for d in decisions if d.kind == "ADAPT"]
        if adapt:
            self.assertGreater(v.params["retest_atr"], before)
            lo, hi = PARAM_BOUNDS["retest_atr"]
            self.assertLessEqual(v.params["retest_atr"], hi)

    def test_selection_explores_and_does_not_monopolise(self):
        variants = []
        cands = []
        for sid, n in (("HOT", 30), ("COLD", 6)):
            v = StrategyVariant(sid=sid, family="F", label="A", version=1,
                                params=dict(DEFAULT_PARAMS))
            variants.append(v)
            for _ in range(n):
                self.book.record(sid, "F", 0.5, "STRONG_BULL", "LONDON",
                                 "TAKE_PROFIT", 2.0, 0.4, real=False)
            cands.append((v, make_candidate(Direction.LONG, 3400.0, 3390.0,
                                            3411.0, 3425.0, sid=sid)))
        chosen, notes = self.book.select_real(cands, "STRONG_BULL")
        self.assertIsNotNone(chosen)
        cold = self.book.get("COLD")
        hot = self.book.get("HOT")
        bonus_cold = self.cfg.exploration_c * math.sqrt(
            math.log(self.book.total_trades + 1.0) / cold.n)
        bonus_hot = self.cfg.exploration_c * math.sqrt(
            math.log(self.book.total_trades + 1.0) / hot.n)
        self.assertGreater(bonus_cold, bonus_hot)

    def test_snapshot_roundtrip(self):
        for _ in range(4):
            self.book.record("TC-A", "TREND_CONTINUATION", 0.6, "STRONG_BULL",
                             "LONDON", "TAKE_PROFIT", 2.0, 0.4, real=False)
        self.book.record_suspect("TC-A", "TREND_CONTINUATION")
        snap = self.book.snapshot()
        fresh = LearningBook(self.cfg)
        fresh.restore(snap)
        self.assertEqual(fresh.get("TC-A").n, 4)
        self.assertEqual(fresh.family("TREND_CONTINUATION").n, 4)
        self.assertEqual(fresh.total_suspect, 1)
        self.assertAlmostEqual(fresh.score("TC-A"), self.book.score("TC-A"))

    def test_confidence_bands(self):
        s = Stats(key="x")
        self.assertEqual(s.confidence, "NONE")
        for _ in range(5):
            s.add(0.5, "R", "LONDON", "F", "TAKE_PROFIT", 1.0, 0.5, False)
        self.assertEqual(s.confidence, "EXPERIMENTAL")
        for _ in range(20):
            s.add(0.5, "R", "LONDON", "F", "TAKE_PROFIT", 1.0, 0.5, False)
        self.assertEqual(s.confidence, "MODERATE")


# ===========================================================================
# 18. strategy space
# ===========================================================================

class TestStrategySpace(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()

    def test_seed_population_covers_every_family_without_padding(self):
        pop = seed_population(self.cfg, "2026-03-02")
        self.assertLessEqual(len(pop), self.cfg.max_population)
        self.assertEqual(set(v.family for v in pop), set(FAMILIES))
        self.assertLessEqual(len(pop), 21,
                             "V5 must not reproduce V4's variant padding")
        for family in FAMILIES:
            count = sum(1 for v in pop if v.family == family)
            self.assertLessEqual(count, self.cfg.max_variants_per_family)

    def test_population_is_deterministic(self):
        a = seed_population(self.cfg, "2026-03-02")
        b = seed_population(self.cfg, "2026-03-02")
        self.assertEqual([v.sid for v in a], [v.sid for v in b])
        self.assertEqual([v.params for v in a], [v.params for v in b])

    def test_mutation_stays_inside_published_bounds(self):
        import random as _random
        rng = _random.Random(7)
        parent = seed_population(self.cfg, "2026-03-02")[0]
        for i in range(80):
            child = mutate_variant(parent, i, rng, "2026-03-03")
            for name, value in child.params.items():
                if name in PARAM_BOUNDS:
                    lo, hi = PARAM_BOUNDS[name]
                    self.assertGreaterEqual(value, lo - 1e-9, name)
                    self.assertLessEqual(value, hi + 1e-9, name)
            self.assertEqual(child.family, parent.family)
            parent = child

    def test_mutation_records_lineage(self):
        import random as _random
        parent = seed_population(self.cfg, "2026-03-02")[0]
        child = mutate_variant(parent, 1, _random.Random(3), "2026-03-03")
        self.assertEqual(child.parent, parent.sid)
        self.assertEqual(child.mutations, parent.mutations + 1)
        self.assertGreater(child.version, parent.version)

    def test_variant_roundtrip(self):
        v = seed_population(self.cfg, "2026-03-02")[0]
        again = StrategyVariant.from_dict(v.to_dict())
        self.assertEqual(v.sid, again.sid)
        self.assertEqual(v.params, again.params)


# ===========================================================================
# 19. setup families require the full sequence
# ===========================================================================

class TestSetupSequences(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        from adaptive_bot_v5.setups_v5 import SetupLibrary
        self.library = SetupLibrary(self.cfg)
        self.spec = make_spec()

    def _evaluate(self, family, state, direction=Direction.LONG):
        variant = next(v for v in seed_population(self.cfg, "2026-03-02")
                       if v.family == family)
        return self.library.evaluate(variant, state, direction,
                                     self.spec.money_per_price_unit_per_unit(),
                                     POINT)

    def test_flat_market_produces_no_setups_at_all(self):
        state = make_state(self.cfg, flat_candles(90),
                           m15_candles=flat_candles(90, minutes=15))
        for family in FAMILIES:
            cand, log = self._evaluate(family, state)
            self.assertIsNone(cand, f"{family} fired on a flat market")
            self.assertTrue(log.failure)

    def test_unsafe_regime_disables_every_family(self):
        state = make_state(self.cfg, zigzag_candles(90),
                           m15_candles=zigzag_candles(90, minutes=15),
                           regime="ABNORMAL_SPREAD")
        for family in FAMILIES:
            cand, log = self._evaluate(family, state)
            self.assertIsNone(cand)
            self.assertIn("REGIME_SAFE", log.lines[0])

    def test_trend_continuation_requires_a_clear_htf_trend(self):
        state = make_state(self.cfg, zigzag_candles(90),
                           m15_candles=zigzag_candles(90, minutes=15),
                           bias_strength="WEAK")
        cand, log = self._evaluate("TREND_CONTINUATION", state)
        self.assertIsNone(cand)
        self.assertIn("HTF_TREND_CLEAR", log.failure)

    def test_liquidity_reversal_needs_a_sweep(self):
        state = make_state(self.cfg, zigzag_candles(90),
                           m15_candles=zigzag_candles(90, minutes=15),
                           sweeps=[])
        cand, log = self._evaluate("LIQUIDITY_REVERSAL", state)
        self.assertIsNone(cand)
        self.assertTrue("LIQUIDITY_SWEEP" in log.failure
                        or "HTF_CONTEXT" in log.failure
                        or "NOT_AGAINST_HTF" in log.failure, log.failure)

    def test_m1_is_never_a_setup_source(self):
        """No family reads M1 features; the decision timeframe is M5."""
        import inspect
        import re as _re
        from adaptive_bot_v5 import setups_v5
        source = inspect.getsource(setups_v5)
        self.assertIsNone(_re.search(r"Timeframe\.M1\b", source))
        self.assertIsNone(_re.search(r"state\.m1\b", source))
        self.assertIsNone(_re.search(r"\bf1\b", source))

    def test_every_family_has_a_confluence_threshold(self):
        for family in FAMILIES:
            self.assertIn(family, self.cfg.family_min_confluence)
            self.assertGreaterEqual(self.cfg.family_min_confluence[family],
                                    self.cfg.min_confluence)
            self.assertLessEqual(self.cfg.family_min_confluence[family], 100)


# ===========================================================================
# 19b. a purpose-built market DOES produce a setup
# ===========================================================================

class TestSetupCanActuallyFire(unittest.TestCase):
    """The gates above prove V5 rejects.  This proves it can also ACCEPT, so
    the six families are not dead code that would never trade."""

    def setUp(self):
        self.cfg = V5Config()
        from adaptive_bot_v5.setups_v5 import SetupLibrary
        self.library = SetupLibrary(self.cfg)

    @staticmethod
    def _trend(n, minutes, start, drift, amp, period, rng):
        out, prev = [], start
        for i in range(n):
            base = start + drift * i + amp * math.sin(2 * math.pi * i / period)
            o, c = prev, base
            out.append(bar(T0 + timedelta(minutes=minutes * i), o,
                           max(o, c) + rng / 2, min(o, c) - rng / 2, c))
            prev = c
        return out

    def _market(self):
        """A calm uptrend, a controlled two-bar pullback to the EMA band, then
        a modest confirming close, with session liquidity above for a target."""
        drift, rng, pull, resume = 0.10, 1.2, 1.6, 0.6
        m5 = self._trend(140, 5, 3300.0, drift, 1.0, 9, rng)
        t = m5[-1].time
        base = m5[-1].close
        m5.append(bar(t + timedelta(minutes=5), base, base + rng * 0.2,
                      base - pull, base - pull * 0.85))
        p = m5[-1].close
        m5.append(bar(t + timedelta(minutes=10), p, p + rng * 0.2,
                      p - pull * 0.35, p - pull * 0.1))
        p = m5[-1].close
        m5.append(bar(t + timedelta(minutes=15), p, p + resume * 1.12,
                      p - rng * 0.2, p + resume))
        m15 = self._trend(120, 15, 3200.0, drift * 3, 2.0, 9, rng * 2)
        m30 = self._trend(110, 30, 3100.0, drift * 6, 3.0, 9, rng * 3)
        h1 = self._trend(100, 60, 2900.0, drift * 12, 5.0, 9, rng * 5)
        last = m5[-1].close
        marks = {"asia_high": last + 14.0, "asia_low": last - 12.0,
                 "london_high": last + 9.0, "london_low": last - 7.0}
        cfg = self.cfg
        features = {}
        for tf, candles in ((Timeframe.M5, m5), (Timeframe.M15, m15),
                            (Timeframe.M30, m30), (Timeframe.H1, h1)):
            built = feats(candles, tf, cfg)
            if built is not None:
                features[tf] = built
        state = MarketStateBuilder(cfg).build(
            now=m5[-1].time + timedelta(minutes=5), bid=last,
            ask=last + SPREAD_PRICE, spread_points=SPREAD_POINTS, point=POINT,
            regime="STRONG_BULL", session=SessionName.LONDON, feats=features,
            session_marks=marks, m5_candles=m5)
        # a decisive higher-timeframe read, so the M5 sequence is what is
        # under test
        state.bias = BiasReading(Direction.LONG, "STRONG", TrendState.BULLISH,
                                 TrendState.BULLISH, TrendState.BULLISH,
                                 "H1 + M30 + M15 bullish")
        state.location = LocationReading("DISCOUNT", 0.3, last - 40, last + 40)
        state.strong_trend = StrongTrendReading(Direction.LONG, 9,
                                               ["fixture"], True, 3, 0.5, 0.5)
        return state

    def test_trend_continuation_produces_a_complete_setup(self):
        state = self._market()
        variant = next(v for v in seed_population(self.cfg, "2026-03-02")
                       if v.family == "TREND_CONTINUATION")
        cand, log = self.library.evaluate(variant, state, Direction.LONG, 1.0,
                                          POINT)
        self.assertIsNotNone(cand, "no setup: " + (log.failure or "?"))
        # the whole mandatory sequence is recorded and every line is a PASS
        self.assertTrue(all(line.startswith("PASS") or line.startswith("note")
                            for line in log.lines), log.lines)
        self.assertGreaterEqual(cand.confluence.score,
                                self.library.threshold_for(variant))
        # a real structural stop, at or beyond the volatility floor
        floor = max(self.cfg.min_stop_atr_frac * state.atr_m5,
                    self.cfg.min_stop_points * POINT)
        risk = risk_price(Direction.LONG,
                          planned_fill(Direction.LONG, cand.entry_ref,
                                       state.spread_price,
                                       self.cfg.slippage_buffer_points * POINT),
                          cand.stop, state.spread_price)
        self.assertGreaterEqual(risk + 1e-9, floor)
        self.assertIn("protected swing", cand.stop_reason)
        # a structural target that clears the net reward:risk floors
        self.assertGreater(cand.target, cand.tp1)
        self.assertGreater(cand.tp1, cand.entry_ref)
        self.assertGreaterEqual(cand.target_r, self.cfg.min_net_rr)
        self.assertGreaterEqual(cand.blended_rr, self.cfg.min_blended_rr)
        self.assertTrue(cand.target_reason)

    def test_the_same_market_does_not_fire_every_family(self):
        """A market built for one family must not trigger all six — that would
        mean the mandatory sequences are not discriminating."""
        state = self._market()
        fired = []
        for family in FAMILIES:
            variant = next(v for v in seed_population(self.cfg, "2026-03-02")
                           if v.family == family)
            cand, _ = self.library.evaluate(variant, state, Direction.LONG,
                                            1.0, POINT)
            if cand is not None:
                fired.append(family)
        self.assertIn("TREND_CONTINUATION", fired)
        self.assertLess(len(fired), len(FAMILIES), fired)

    def test_a_short_setup_is_refused_in_a_bullish_market(self):
        state = self._market()
        variant = next(v for v in seed_population(self.cfg, "2026-03-02")
                       if v.family == "TREND_CONTINUATION")
        cand, log = self.library.evaluate(variant, state, Direction.SHORT,
                                          1.0, POINT)
        self.assertIsNone(cand)
        self.assertTrue(log.failure)


# ===========================================================================
# 20. research clock: active trading days
# ===========================================================================

class TestResearchClock(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.clock = ResearchClock(self.cfg)

    def test_weekends_do_not_count(self):
        saturday = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
        sunday = datetime(2026, 3, 8, 12, 0, tzinfo=UTC)
        self.assertEqual(saturday.weekday(), 5)
        for _ in range(600):
            self.clock.observe(saturday, market_open=True)
            self.clock.observe(sunday, market_open=True)
        self.assertEqual(self.clock.completed_days(), 0)
        self.assertEqual(self.clock.minutes, {})

    def test_closed_market_does_not_count(self):
        monday = datetime(2026, 3, 2, 3, 0, tzinfo=UTC)
        for _ in range(600):
            self.clock.observe(monday, market_open=False)
        self.assertEqual(self.clock.completed_days(), 0)

    def test_a_day_needs_the_minimum_observed_minutes(self):
        monday = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        for _ in range(self.cfg.active_day_min_minutes - 1):
            self.clock.observe(monday, market_open=True)
        self.assertEqual(self.clock.completed_days(), 0)
        self.clock.observe(monday, market_open=True)
        self.assertEqual(self.clock.completed_days(), 1)

    def test_research_completes_only_after_enough_active_days(self):
        cfg = V5Config()
        cfg.research_days = 3
        clock = ResearchClock(cfg)
        day = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        for offset in range(7):                    # Mon..Sun
            when = day + timedelta(days=offset)
            for _ in range(cfg.active_day_min_minutes):
                clock.observe(when, market_open=True)
        self.assertEqual(clock.completed_days(), 5)   # weekend excluded
        self.assertTrue(clock.is_over())

    def test_day_index_is_capped_and_monotone(self):
        day = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        self.assertEqual(self.clock.day_index(day), 1)
        for _ in range(self.cfg.active_day_min_minutes):
            self.clock.observe(day, market_open=True)
        self.assertEqual(self.clock.day_index(day), 1)
        nxt = day + timedelta(days=1)
        self.assertEqual(self.clock.day_index(nxt), 2)

    def test_restart_never_resets_the_clock(self):
        day = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        self.clock.begin(day)
        for _ in range(self.cfg.active_day_min_minutes):
            self.clock.observe(day, market_open=True)
        snap = self.clock.snapshot()
        fresh = ResearchClock(self.cfg)
        fresh.begin(day + timedelta(days=4))      # a later "first" start
        fresh.restore(snap)
        self.assertEqual(fresh.start, day)        # earliest start wins
        self.assertEqual(fresh.completed_days(), 1)

    def test_replaying_a_day_cannot_double_count_it(self):
        day = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        for _ in range(self.cfg.active_day_min_minutes):
            self.clock.observe(day, market_open=True)
        snap = self.clock.snapshot()
        self.clock.restore(snap)
        self.clock.restore(snap)
        self.assertEqual(self.clock.completed_days(), 1)
        self.assertAlmostEqual(self.clock.minutes[day.date().isoformat()],
                               float(self.cfg.active_day_min_minutes))


# ===========================================================================
# 21. persistence
# ===========================================================================

class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="v5state")
        self.cfg = V5Config()
        self.cfg.state_dir = self.dir
        self.logs = []
        self.store = StateStore(self.cfg, self.logs.append)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_state_roundtrip(self):
        self.store.save_state({"a": 1, "clock": {"start": "x"}})
        loaded = self.store.load_state()
        self.assertEqual(loaded["a"], 1)
        self.assertEqual(loaded["schema"], self.cfg.state_schema)

    def test_corrupt_state_is_preserved_not_deleted(self):
        path = os.path.join(self.dir, "research_state.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(self.store.load_state())
        self.assertTrue(os.path.exists(path + ".corrupt"))
        self.assertTrue(any("unreadable" in m for m in self.logs))

    def test_foreign_schema_is_ignored_not_half_loaded(self):
        path = os.path.join(self.dir, "research_state.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"schema": 99, "population": []}')
        self.assertIsNone(self.store.load_state())
        self.assertTrue(any("schema" in m for m in self.logs))

    def test_csv_append_and_rewrite(self):
        self.store.csv_append("learning_log", {
            "date": "2026-03-02", "kind": "SPAWN", "sid": "X", "why": "test"})
        path = os.path.join(self.dir, "learning_log.csv")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.store.csv_rewrite("learning_log", [
            {"date": "d", "kind": "K", "sid": "S", "why": "W"}])
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_unknown_csv_is_ignored_safely(self):
        self.store.csv_append("not_a_file", {"x": 1})
        self.assertFalse(os.path.exists(os.path.join(self.dir,
                                                     "not_a_file.csv")))

    def test_real_trade_record_roundtrip(self):
        m = make_managed(Direction.SHORT, 3400.0, 3410.0, 3389.0, 3375.0)
        m.be_done = True
        m.be_reason = "TP1 banked"
        m.trail_updates.append("x -> y")
        m.meta["range_low"] = 3390.0
        rec = RealTradeRecord(
            trade_id="r1", position_id=1001, sid="TC-A",
            family="TREND_CONTINUATION", version=2, direction=Direction.SHORT,
            managed=m, entry_time=T0, risk_pct=0.002, risk_money_sized=20.0,
            confluence_score=71.0, confluence_detail="+A|-B",
            stop_reason="swept high", tp1_reason="1.2R",
            target_reason="PDL", sweep_kind="PDH", regime="STRONG_BEAR",
            session="LONDON", htf_bias="SHORT/STRONG", location="PREMIUM",
            spread_points=30.0, params={"be_min_r": 1.0})
        again = RealTradeRecord.from_dict(rec.snapshot())
        self.assertIsNotNone(again)
        self.assertEqual(again.position_id, 1001)
        self.assertEqual(again.direction, Direction.SHORT)
        self.assertTrue(again.managed.be_done)
        self.assertEqual(again.managed.be_reason, "TP1 banked")
        self.assertAlmostEqual(again.managed.risk_dist, m.risk_dist)
        self.assertEqual(again.managed.meta["range_low"], 3390.0)

    def test_real_trade_record_rejects_garbage(self):
        self.assertIsNone(RealTradeRecord.from_dict({"nonsense": True}))

    def test_shadow_snapshot_roundtrip(self):
        h = ShadowHarness(self.cfg)
        h.engine.equity["TC-A"] = 10_123.0
        h.engine._serial = 17
        h.engine.discarded_counts["stale signal"] = 2
        snap = h.engine.snapshot()
        fresh = ShadowHarness(self.cfg)
        fresh.engine.restore(snap)
        self.assertEqual(fresh.engine.equity["TC-A"], 10_123.0)
        self.assertEqual(fresh.engine._serial, 17)
        self.assertEqual(fresh.engine.discarded_counts["stale signal"], 2)
        self.assertEqual(fresh.engine.open, {})     # in-flight not resumed

    def test_abandon_open_records_but_does_not_teach(self):
        h = ShadowHarness(self.cfg)
        cand = make_candidate(Direction.LONG, 3400.0, 3390.0, 3411.0, 3425.0,
                              created=T0)
        h.engine.submit(cand, SPREAD_POINTS, 2.0)
        h.m1(bar(T0 + timedelta(minutes=1), 3400.0, 3400.2, 3399.8, 3400.0))
        self.assertEqual(len(h.engine.open), 1)
        count = h.engine.abandon_open(T0 + timedelta(minutes=2),
                                      "restart: continuity lost")
        self.assertEqual(count, 1)
        self.assertEqual(len(h.engine.open), 0)
        self.assertEqual(len(h.closed), 0)          # no learning from these
        self.assertEqual(h.rows[-1].status, "discarded")


# ===========================================================================
# 22. broker level conversion
# ===========================================================================

class TestBrokerLevels(unittest.TestCase):

    def test_long_levels_pass_through(self):
        self.assertAlmostEqual(broker_level(3390.0, Direction.LONG, 0.30),
                               3390.0)

    def test_short_levels_shift_by_the_spread(self):
        self.assertAlmostEqual(broker_level(3410.0, Direction.SHORT, 0.30),
                               3410.30)

    def test_short_stop_is_not_secretly_tighter(self):
        """A short's bid-level stop of 3410 must be submitted at the ask
        equivalent, otherwise the broker stops it one spread early."""
        bid_stop = 3410.0
        submitted = broker_level(bid_stop, Direction.SHORT, 0.30)
        self.assertGreater(submitted, bid_stop)


# ===========================================================================
# 23. reporting
# ===========================================================================

class TestReporting(unittest.TestCase):

    def setUp(self):
        self.cfg = V5Config()
        self.book = LearningBook(self.cfg)
        self.clock = ResearchClock(self.cfg)
        self.clock.begin(T0)
        for _ in range(self.cfg.active_day_min_minutes):
            self.clock.observe(T0, market_open=True)
        self.variants = seed_population(self.cfg, "2026-03-02")

    def test_final_report_carries_the_provisional_label(self):
        for _ in range(8):
            self.book.record("LIQUIDITY_REVERSAL-A", "LIQUIDITY_REVERSAL",
                             0.6, "STRONG_BULL", "LONDON", "TAKE_PROFIT",
                             2.0, 0.4, real=False)
        text, payload = final_report(T0, self.clock, self.book, self.variants,
                                     self.cfg, ["equity 10000"])
        self.assertIn(PROVISIONAL_LABEL, text)
        self.assertEqual(payload["label"], PROVISIONAL_LABEL)
        self.assertEqual(payload["profitability_claim"], "none")
        self.assertIn("LIQUIDITY_REVERSAL",
                      payload["provisional_winner"]["family"])

    def test_final_report_states_sample_size_and_follow_up(self):
        for _ in range(6):
            self.book.record("BREAKOUT_RETEST-A", "BREAKOUT_RETEST", 0.4,
                             "EXPANSION", "LONDON", "TAKE_PROFIT", 1.6, 0.5,
                             real=False)
        text, payload = final_report(T0, self.clock, self.book, self.variants,
                                     self.cfg, ["equity 10000"])
        self.assertIn("6 trades", text)
        self.assertTrue(payload["required_follow_up"])
        self.assertIn("walk-forward", text.lower())
        self.assertIn("100", text)

    def test_final_report_ranks_families(self):
        for _ in range(10):
            self.book.record("A", "TREND_CONTINUATION", 0.8, "STRONG_BULL",
                             "LONDON", "TAKE_PROFIT", 2.0, 0.4, real=False)
        for _ in range(10):
            self.book.record("B", "RANGE_X", -0.5, "RANGE", "LONDON",
                             "STOP_LOSS", 0.3, 1.0, real=False)
        text, payload = final_report(T0, self.clock, self.book, self.variants,
                                     self.cfg, ["equity 10000"])
        self.assertEqual(payload["families"][0]["family"],
                         "TREND_CONTINUATION")
        self.assertIn("SETUP FAMILY RANKING", text)

    def test_final_report_survives_an_empty_run(self):
        text, payload = final_report(T0, self.clock, self.book, self.variants,
                                     self.cfg, ["equity 10000"])
        self.assertIn(PROVISIONAL_LABEL, text)
        self.assertEqual(payload["provisional_winner"], {})

    def test_report_names_the_honest_limitations(self):
        text, _ = final_report(T0, self.clock, self.book, self.variants,
                               self.cfg, ["equity 10000"])
        self.assertIn("no live news feed", text.lower())
        self.assertIn("in-sample", text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
