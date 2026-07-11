"""
Offline test suite for the adaptive_bot package (no cTrader required).

Run from the ctrader_bot/ directory:
    python3 -m unittest discover tests -v

These tests exercise the pure strategy/risk/filter logic that the cBot
delegates to.  The cAlgo API surface itself can only be exercised inside
cTrader (see README: backtest first, then demo).
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adaptive_bot.core.config import Config, ConfigValidator
from adaptive_bot.core.helpers import atr_series, resample, tf_bucket_start
from adaptive_bot.core.models import (Candle, CTraderSymbolSpec, Direction,
                                      FVGState, LockReason, ScoreBreakdown,
                                      SessionName, Setup, SetupGrade,
                                      SetupModel, Regime, Timeframe,
                                      TrendState, ZoneKind, new_id)
from adaptive_bot.execution.order_manager import OrderFacts, OrderManager
from adaptive_bot.filters.news_filter import NewsFilter, first_friday
from adaptive_bot.filters.session_filter import SessionManager
from adaptive_bot.filters.spread_filter import SpreadFilter
from adaptive_bot.risk.daily_loss_guard import DailyLossGuard
from adaptive_bot.risk.position_sizing import PositionSizer, SizingResult
from adaptive_bot.risk.trade_limits import TradeLimits
from adaptive_bot.strategy.entry_trigger import M1TriggerDetector
from adaptive_bot.strategy.fair_value_gap import FVGDetector
from adaptive_bot.strategy.liquidity import LiquidityDetector
from adaptive_bot.strategy.market_structure import (StructureAnalyzer,
                                                    SwingDetector)
from adaptive_bot.strategy.regime import MarketRegimeDetector
from adaptive_bot.strategy.setup_scoring import SetupScorer
from adaptive_bot.strategy.strategy_engine import (FIXED_PLAN, ContextBuilder,
                                                   StrategyEngine)
from adaptive_bot.strategy.supply_demand import (OrderBlockDetector,
                                                 SupplyDemandDetector)

UTC = timezone.utc
T0 = datetime(2026, 1, 6, 8, 0, tzinfo=UTC)      # a Tuesday, London session


def mk(ts_minutes, o, h, l, c, vol=100.0):
    return Candle(T0 + timedelta(minutes=ts_minutes), o, h, l, c, vol)


def flat_candles(n, price=3300.0, step_minutes=1):
    """Sideways series with deterministic variation so fractal swings
    (which need strict inequalities) can actually confirm."""
    out = []
    for i in range(n):
        o = price + (0.1 if i % 2 == 0 else -0.1)
        c = price - (0.1 if i % 2 == 0 else -0.1)
        wobble = 0.25 + 0.08 * ((i * 3) % 4)
        out.append(mk(i * step_minutes, o, max(o, c) + wobble,
                      min(o, c) - wobble, c))
    return out


def trending_candles(n, start=3300.0, drift=0.8, step_minutes=5,
                     pullback_every=7):
    """Impulsive up-move with periodic pullbacks (creates HH/HL swings)."""
    out = []
    price = start
    for i in range(n):
        if pullback_every and i % pullback_every in (pullback_every - 2,
                                                     pullback_every - 1):
            o, c = price, price - drift * 0.6
        else:
            o, c = price, price + drift
        hi = max(o, c) + 0.4
        lo = min(o, c) - 0.4
        out.append(mk(i * step_minutes, o, hi, lo, c))
        price = c
    return out


class TestSwings(unittest.TestCase):
    def test_confirmation_delay_no_repaint(self):
        candles = flat_candles(30)
        # spike high at index 10
        spike = candles[10]
        candles[10] = Candle(spike.time, spike.open, spike.high + 5.0,
                             spike.low, spike.close)
        det = SwingDetector(2, 2)
        swings = det.detect(candles)
        highs = [s for s in swings if s.price > 3303]
        self.assertEqual(len(highs), 1)
        self.assertEqual(highs[0].index, 10)
        self.assertEqual(highs[0].confirmed_index, 12)  # 2 candles later

    def test_alternating(self):
        candles = trending_candles(60)
        det = SwingDetector(2, 2)
        alt = SwingDetector.alternating(det.detect(candles))
        for a, b in zip(alt, alt[1:]):
            self.assertNotEqual(a.kind, b.kind)


class TestStructure(unittest.TestCase):
    def test_uptrend_classified_bullish(self):
        candles = trending_candles(80)
        state = StructureAnalyzer(Config()).analyze(candles)
        self.assertEqual(state.trend, TrendState.BULLISH)
        self.assertTrue(any(ev.kind.value == "BOS" for ev in state.events))

    def test_premium_discount(self):
        cfg = Config()
        candles = trending_candles(80)
        analyzer = StructureAnalyzer(cfg)
        state = analyzer.analyze(candles)
        self.assertIsNotNone(state.dealing_range)
        label_low, _ = analyzer.premium_discount(state,
                                                 state.dealing_range.low + 0.01)
        label_high, _ = analyzer.premium_discount(state,
                                                  state.dealing_range.high - 0.01)
        self.assertEqual(label_low, "DISCOUNT")
        self.assertEqual(label_high, "PREMIUM")


class TestLiquidity(unittest.TestCase):
    def test_sweep_requires_close_back_or_displacement(self):
        cfg = Config()
        candles = flat_candles(60, price=3300.0)
        # a clear swing high at index 20
        c20 = candles[20]
        candles[20] = Candle(c20.time, c20.open, 3304.0, c20.low, c20.close)
        det = SwingDetector(cfg.swing_left, cfg.swing_right)
        swings = SwingDetector.alternating(det.detect(candles))
        liq = LiquidityDetector(cfg)
        levels = liq.detect_levels(candles, swings)
        self.assertTrue(levels)
        buy_side = [l for l in levels if l.buy_side]
        self.assertTrue(buy_side)
        # craft a sweep candle: pierces the swing high, closes back inside
        hi_level = max(l.price for l in buy_side)
        sweep = Candle(candles[-1].time + timedelta(minutes=1),
                       3300.0, hi_level + 1.5, 3299.0, 3299.5)
        extended = candles + [sweep]
        levels2 = liq.detect_levels(extended, swings)
        sweeps = liq.update_states(levels2, extended)
        self.assertTrue(any(s.closed_back for s in sweeps))

    def test_equal_highs_cluster(self):
        cfg = Config()
        candles = flat_candles(80, price=3300.0)
        det = SwingDetector(cfg.swing_left, cfg.swing_right)

        # engineer two nearly-equal swing highs
        def spike(i, px):
            c = candles[i]
            candles[i] = Candle(c.time, c.open, px, c.low, c.close)
        spike(20, 3304.00)
        spike(50, 3304.05)
        swings = SwingDetector.alternating(det.detect(candles))
        liq = LiquidityDetector(cfg)
        levels = liq.detect_levels(candles, swings)
        eqh = [l for l in levels if l.kind.value == "EQUAL_HIGHS"]
        self.assertTrue(eqh)


class TestZonesAndFVG(unittest.TestCase):
    def _zone_series(self):
        candles = flat_candles(30, price=3300.0)
        t0 = 30
        # base: three tiny candles, then a big bullish displacement leg-out
        base = [mk(t0, 3300.0, 3300.6, 3299.4, 3300.1),
                mk(t0 + 1, 3300.1, 3300.7, 3299.5, 3300.0),
                mk(t0 + 2, 3300.0, 3300.5, 3299.6, 3300.2)]
        leg_out = [mk(t0 + 3, 3300.2, 3305.5, 3300.1, 3305.2),
                   mk(t0 + 4, 3305.2, 3308.9, 3305.0, 3308.5)]
        after = [mk(t0 + 5 + i, 3308.5 + i * 0.05, 3309.2 + i * 0.05,
                    3308.0 + i * 0.05, 3308.7 + i * 0.05) for i in range(10)]
        return candles + base + leg_out + after

    def test_demand_zone_detected(self):
        cfg = Config()
        candles = self._zone_series()
        zones = SupplyDemandDetector(cfg, Timeframe.M5).detect(candles)
        demand = [z for z in zones if z.kind == ZoneKind.DEMAND]
        self.assertTrue(demand)
        self.assertGreater(demand[-1].displacement_score, 0)

    def test_bullish_fvg_detected_and_fill_tracked(self):
        cfg = Config()
        candles = self._zone_series()
        gaps = FVGDetector(cfg, Timeframe.M5).detect(candles)
        bulls = [g for g in gaps if g.direction == Direction.LONG]
        self.assertTrue(bulls)
        self.assertIn(bulls[0].state,
                      (FVGState.UNFILLED, FVGState.PARTIAL, FVGState.MITIGATED))

    def test_order_block_needs_link(self):
        cfg = Config()
        candles = self._zone_series()
        structure = StructureAnalyzer(cfg).analyze(candles)
        liq = LiquidityDetector(cfg)
        levels = liq.detect_levels(candles, structure.swings)
        sweeps = liq.update_states(levels, candles)
        obs = OrderBlockDetector(cfg, Timeframe.M5).detect(candles, structure,
                                                           sweeps)
        for b in obs:
            self.assertTrue(b.linked_structure is not None or b.linked_sweep)


class TestScoring(unittest.TestCase):
    def test_aligned_beats_countertrend(self):
        cfg = Config()
        scorer = SetupScorer(cfg)
        common = dict(zone=None, sweep=None, structure_event=None,
                      displacement=True, fvg=None, order_block=None,
                      pd_zone="DISCOUNT", session=SessionName.LONDON,
                      news_blocked=False, news_protection_complete=False,
                      rr_tp1=2.5, target_is_liquidity=True)
        aligned = scorer.score(direction=Direction.LONG,
                               htf_bias=TrendState.BULLISH, **common)
        counter = scorer.score(direction=Direction.SHORT,
                               htf_bias=TrendState.BULLISH, **common)
        self.assertGreater(aligned.total, counter.total)
        self.assertEqual(aligned.htf_alignment, 15.0)
        self.assertEqual(counter.htf_alignment, 0.0)

    def test_manual_news_never_full_marks(self):
        cfg = Config()
        scorer = SetupScorer(cfg)
        b = scorer.score(direction=Direction.LONG,
                         htf_bias=TrendState.BULLISH, zone=None, sweep=None,
                         structure_event=None, displacement=False, fvg=None,
                         order_block=None, pd_zone="DISCOUNT",
                         session=SessionName.LONDON, news_blocked=False,
                         news_protection_complete=False, rr_tp1=2.0,
                         target_is_liquidity=False)
        self.assertEqual(b.news_safety, 3.0)


def gold_spec(volume_min=1.0, volume_step=1.0, volume_max=1000.0):
    return CTraderSymbolSpec(
        name="XAUUSD", digits=2, tick_size=0.01, tick_value=0.01,
        pip_size=0.01, pip_value=0.01, volume_min=volume_min,
        volume_max=volume_max, volume_step=volume_step, spread_points=30.0)


class TestPositionSizing(unittest.TestCase):
    def test_volume_from_true_risk(self):
        cfg = Config()
        sizer = PositionSizer(cfg)
        r = sizer.size(gold_spec(), equity=10_000.0, risk_fraction=0.0025,
                       entry=3300.0, stop=3297.50)
        # loss/unit = 2.50 + 0.15 slip + 0.30 spread = 2.95 -> 25/2.95 = 8.47
        self.assertFalse(r.rejected)
        self.assertEqual(r.volume_units, 8.0)
        self.assertLessEqual(r.risk_money, 25.0)

    def test_min_volume_too_risky_rejects(self):
        cfg = Config()
        sizer = PositionSizer(cfg)
        r = sizer.size(gold_spec(volume_min=100.0), equity=10_000.0,
                       risk_fraction=0.0025, entry=3300.0, stop=3297.50)
        self.assertTrue(r.rejected)
        self.assertIn("minimum volume", r.reason)

    def test_rounds_down_to_step(self):
        cfg = Config()
        sizer = PositionSizer(cfg)
        r = sizer.size(gold_spec(volume_step=5.0, volume_min=5.0),
                       equity=100_000.0, risk_fraction=0.0025,
                       entry=3300.0, stop=3297.50)
        self.assertFalse(r.rejected)
        self.assertEqual(r.volume_units % 5.0, 0.0)

    def test_risk_cap_is_hard(self):
        cfg = Config()
        sizer = PositionSizer(cfg)
        r = sizer.size(gold_spec(), equity=10_000.0,
                       risk_fraction=0.05,          # asks for 5%
                       entry=3300.0, stop=3297.50)
        self.assertLessEqual(r.risk_money, 10_000.0 * cfg.max_risk_per_trade
                             + 1e-6)


class TestDailyGuard(unittest.TestCase):
    def _guard(self):
        g = DailyLossGuard(Config())
        g.roll(T0, 10_000.0, 10_000.0)
        return g

    def test_realised_loss_locks_day(self):
        g = self._guard()
        g.register_close(-101.0)          # -1.01%
        self.assertEqual(g.lock_reason(9_899.0), LockReason.DAILY_LOSS)

    def test_combined_floating_loss_locks(self):
        g = self._guard()
        g.register_close(-50.0)
        self.assertEqual(g.lock_reason(9_950.0, unrealised=-60.0),
                         LockReason.DAILY_LOSS)

    def test_trade_cap_locks(self):
        g = self._guard()
        for _ in range(3):
            g.register_open(SessionName.LONDON)
        self.assertEqual(g.lock_reason(10_000.0), LockReason.MAX_TRADES_DAY)

    def test_lock_clears_on_new_day(self):
        g = self._guard()
        g.register_close(-150.0)
        self.assertEqual(g.lock_reason(9_850.0), LockReason.DAILY_LOSS)
        g.roll(T0 + timedelta(days=1), 9_850.0, 9_850.0)
        self.assertEqual(g.lock_reason(9_850.0), LockReason.NONE)

    def test_emergency_stop(self):
        g = self._guard()
        g.emergency = True
        self.assertEqual(g.lock_reason(10_000.0), LockReason.EMERGENCY)

    def test_consecutive_losses(self):
        g = self._guard()
        for _ in range(3):
            g.register_close(-10.0)
        self.assertEqual(g.lock_reason(9_970.0),
                         LockReason.CONSECUTIVE_LOSSES)


class TestTradeLimits(unittest.TestCase):
    def test_one_position_rule(self):
        cfg = Config()
        guard = DailyLossGuard(cfg)
        guard.roll(T0, 10_000.0, 10_000.0)
        limits = TradeLimits(cfg, guard)
        ok, why = limits.can_open(10_000.0, open_positions=1,
                                  open_risk_money=0.0, new_risk_money=25.0)
        self.assertFalse(ok)
        self.assertIn("max positions", why)


class TestSessions(unittest.TestCase):
    def test_session_classification(self):
        sm = SessionManager(Config())
        t = datetime(2026, 1, 6, 3, 0, tzinfo=UTC)
        self.assertEqual(sm.session_at(t), SessionName.ASIA)
        t = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)
        self.assertEqual(sm.session_at(t), SessionName.LONDON)
        t = datetime(2026, 1, 6, 13, 0, tzinfo=UTC)
        self.assertEqual(sm.session_at(t), SessionName.OVERLAP)
        t = datetime(2026, 1, 6, 18, 0, tzinfo=UTC)
        self.assertEqual(sm.session_at(t), SessionName.NEW_YORK)

    def test_weekend_and_rollover_blocked(self):
        sm = SessionManager(Config())
        sat = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
        self.assertFalse(sm.entry_window_check(sat)[0])
        rollover = datetime(2026, 1, 6, 21, 30, tzinfo=UTC)
        self.assertFalse(sm.entry_window_check(rollover)[0])

    def test_disabled_session_blocked(self):
        cfg = Config()
        cfg.london_enabled = False
        cfg.newyork_enabled = False
        sm = SessionManager(cfg)
        t = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)
        ok, why = sm.entry_window_check(t)
        self.assertFalse(ok)
        self.assertIn("disabled", why)

    def test_asian_range_tracked(self):
        sm = SessionManager(Config())
        candles = [Candle(datetime(2026, 1, 6, 2, 0, tzinfo=UTC)
                          + timedelta(minutes=5 * i),
                          3300, 3301 + i, 3299 - i, 3300) for i in range(5)]
        sm.rebuild_from(candles)
        rng = sm.asian_range(datetime(2026, 1, 6).date())
        self.assertIsNotNone(rng)
        self.assertEqual(rng, (3299 - 4, 3301 + 4))


class TestNews(unittest.TestCase):
    def test_first_friday(self):
        self.assertEqual(first_friday(2026, 7), 3)
        self.assertEqual(first_friday(2026, 1), 2)

    def test_nfp_first_friday_blocked(self):
        nf = NewsFilter(Config(), UTC)
        t = datetime(2026, 7, 3, 12, 45, tzinfo=UTC)   # first Friday of July
        blocked, reason = nf.blackout(t)
        self.assertTrue(blocked)
        self.assertIn("NFP", reason)
        t2 = datetime(2026, 7, 3, 14, 0, tzinfo=UTC)   # after the window
        self.assertFalse(nf.blackout(t2)[0])

    def test_configured_fomc_blocked(self):
        cfg = Config()
        cfg.fomc_events = ("2026-07-29T18:00",)
        nf = NewsFilter(cfg, UTC)
        t = datetime(2026, 7, 29, 17, 45, tzinfo=UTC)
        blocked, reason = nf.blackout(t)
        self.assertTrue(blocked)
        self.assertIn("FOMC", reason)

    def test_category_disabled(self):
        cfg = Config()
        cfg.fomc_events = ("2026-07-29T18:00",)
        cfg.block_fomc = False
        nf = NewsFilter(cfg, UTC)
        t = datetime(2026, 7, 29, 17, 45, tzinfo=UTC)
        self.assertFalse(nf.blackout(t)[0])

    def test_manual_window(self):
        cfg = Config()
        cfg.manual_blackouts = ("2026-07-15T13:00/2026-07-15T15:00",)
        nf = NewsFilter(cfg, UTC)
        t = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
        self.assertTrue(nf.blackout(t)[0])

    def test_protection_never_claims_complete(self):
        nf = NewsFilter(Config(), UTC)
        self.assertFalse(nf.protection_complete())


class TestSpread(unittest.TestCase):
    def test_gate(self):
        sf = SpreadFilter(Config())
        self.assertTrue(sf.check(30.0)[0])
        self.assertFalse(sf.check(90.0)[0])
        self.assertFalse(sf.check(0.0)[0])   # unknown fails closed


class TestM1Trigger(unittest.TestCase):
    def _tfa(self, candles):
        return StrategyEngine(Config()).build_tf_analysis(Timeframe.M1,
                                                          candles)

    def test_displacement_close_triggers_long(self):
        candles = flat_candles(60)
        last = candles[-1]
        candles[-1] = Candle(last.time, 3300.0, 3304.6, 3299.9, 3304.5)
        res = M1TriggerDetector(Config()).check(self._tfa(candles),
                                                Direction.LONG)
        self.assertTrue(res.fired)

    def test_no_trigger_on_flat_market(self):
        cfg = Config()
        candles = flat_candles(60)
        res = M1TriggerDetector(cfg).check(self._tfa(candles),
                                           Direction.LONG)
        # flat wobble candles: no displacement, no sweep, no structure event
        if res.fired:
            self.assertIn(res.kind, ("M1_REJECTION",))
        else:
            self.assertEqual(res.kind, "NONE")

    def test_gate_disabled_passes(self):
        cfg = Config()
        cfg.require_m1_trigger = False
        res = M1TriggerDetector(cfg).check(None, Direction.LONG)
        self.assertTrue(res.fired)


def minimal_setup(direction=Direction.LONG, score=85.0):
    return Setup(
        setup_id=new_id("s"), model=SetupModel.TREND_CONTINUATION,
        direction=direction, created_time=T0, signal_price=3300.0,
        entry_price=3300.0,
        stop_price=3297.5 if direction == Direction.LONG else 3302.5,
        tp1=3305.0 if direction == Direction.LONG else 3295.0,
        tp2=3308.0 if direction == Direction.LONG else 3292.0,
        runner_target=None, score=score, grade=SetupGrade.A,
        breakdown=ScoreBreakdown(), tf_plan=FIXED_PLAN,
        regime=Regime.STRONG_BULL, session=SessionName.LONDON,
        htf_bias=TrendState.BULLISH)


def good_facts(**overrides):
    facts = OrderFacts(
        is_demo_account=True, symbol_is_gold=True, market_open=True,
        spread_points=30.0, spread_ok=True, open_positions_count=0,
        has_pending_bot_order=False, news_blocked=False, news_reason="",
        lock=LockReason.NONE, equity=10_000.0, session_allowed=True,
        session_reason="LONDON")
    for k, v in overrides.items():
        setattr(facts, k, v)
    return facts


def good_sizing():
    return SizingResult(volume_units=8.0, risk_money=23.6,
                        intended_risk_money=25.0,
                        risk_fraction_actual=0.00236, stop_points=250.0,
                        cost_estimate=3.6)


class TestOrderManager(unittest.TestCase):
    def test_all_checks_pass(self):
        om = OrderManager(Config())
        pf = om.preflight(T0, minimal_setup(), good_sizing(), good_facts())
        self.assertTrue(pf.ok, pf.reason)

    def test_live_account_fails(self):
        om = OrderManager(Config())
        pf = om.preflight(T0, minimal_setup(), good_sizing(),
                          good_facts(is_demo_account=False))
        self.assertFalse(pf.ok)
        self.assertIn("demo", pf.reason)

    def test_open_position_blocks_duplicate(self):
        om = OrderManager(Config())
        pf = om.preflight(T0, minimal_setup(), good_sizing(),
                          good_facts(open_positions_count=1))
        self.assertFalse(pf.ok)

    def test_wrong_side_stop_fails(self):
        om = OrderManager(Config())
        s = minimal_setup()
        s.stop_price = 3305.0            # stop above entry on a LONG
        pf = om.preflight(T0, s, good_sizing(), good_facts())
        self.assertFalse(pf.ok)

    def test_failure_cooldown(self):
        om = OrderManager(Config())
        om.order_failed(T0, "NO_MONEY")
        pf = om.preflight(T0 + timedelta(minutes=1), minimal_setup(),
                          good_sizing(), good_facts())
        self.assertFalse(pf.ok)
        self.assertIn("cooldown", pf.reason.lower())
        pf2 = om.preflight(T0 + timedelta(minutes=20), minimal_setup(),
                          good_sizing(), good_facts())
        self.assertTrue(pf2.ok, pf2.reason)


class TestNoLookahead(unittest.TestCase):
    def test_resample_emits_only_completed_buckets(self):
        candles = flat_candles(7)        # 7 x M1 starting 08:00
        m5 = resample(candles, Timeframe.M5, completed_only=True)
        # 08:00-08:04 complete; 08:05+ bucket incomplete (only 2 candles)
        self.assertEqual(len(m5), 1)
        self.assertEqual(m5[0].time, tf_bucket_start(candles[0].time,
                                                     Timeframe.M5))


class TestEndToEndPipeline(unittest.TestCase):
    def test_full_context_and_evaluation(self):
        """The whole pipeline runs on synthetic data without touching any
        broker API and without raising."""
        cfg = Config()
        engine = StrategyEngine(cfg)
        sessions = SessionManager(cfg)
        news = NewsFilter(cfg, UTC)
        builder = ContextBuilder(cfg, engine, sessions, news)

        m1 = trending_candles(900, step_minutes=1, drift=0.15,
                              pullback_every=9)
        sessions.rebuild_from(m1)
        now = m1[-1].time + timedelta(minutes=1)
        series = {
            Timeframe.M1: m1,
            Timeframe.M5: resample(m1, Timeframe.M5, now=now),
            Timeframe.M15: resample(m1, Timeframe.M15, now=now),
            Timeframe.H1: resample(m1, Timeframe.H1, now=now),
            Timeframe.H4: resample(m1, Timeframe.H4, now=now),
            Timeframe.D1: resample(m1, Timeframe.D1, now=now),
        }
        ctx = builder.build(series, spread_points=30.0, point=0.01, now=now)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.tf_plan.bias_tf, Timeframe.M15)
        rejections = []
        setup = engine.evaluate(
            ctx, cost_price_units=0.45,
            reject_cb=lambda m, s, r: rejections.append((m, s, r)))
        # a setup may or may not qualify on synthetic data — what matters is
        # that evaluation is well-formed and every rejection has a reason
        if setup is not None:
            self.assertGreaterEqual(setup.score, cfg.min_score)
            self.assertNotEqual(setup.stop_price, setup.entry_price)
            self.assertGreaterEqual(setup.rr_to(setup.tp1), cfg.min_rr * 0.9)
        for _, _, reason in rejections:
            self.assertTrue(reason)


if __name__ == "__main__":
    unittest.main()
