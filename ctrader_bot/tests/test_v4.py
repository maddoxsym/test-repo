"""V4 research-system test suite (offline, no cTrader).

Run from ctrader_bot/:  python3 -m unittest tests.test_v4 -v
"""

import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adaptive_bot.core.models import (Candle, CTraderSymbolSpec, Direction,
                                      LockReason, SessionName, Timeframe)
from adaptive_bot.risk.position_sizing import PositionSizer
from adaptive_bot_v4.config_v4 import V4Config, V4ConfigValidator
from adaptive_bot_v4.features import FeatureBuilder
from adaptive_bot_v4.learning import LearningBook
from adaptive_bot_v4.persistence import StateStore
from adaptive_bot_v4.reporting import PROVISIONAL_LABEL, final_report
from adaptive_bot_v4.risk_v4 import AdaptiveRisk, V4Guard
from adaptive_bot_v4.shadow import ShadowEngine
from adaptive_bot_v4.strategy_space import (PARAM_BOUNDS, EvalContext,
                                            Signal, evaluate_strategy,
                                            mutate_strategy, seed_population)

UTC = timezone.utc
T0 = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)


def cfgv4(**kw):
    c = V4Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def gold_spec():
    return CTraderSymbolSpec(
        name="XAUUSD", digits=2, tick_size=0.01, tick_value=0.01,
        pip_size=0.01, pip_value=0.01, volume_min=1.0, volume_max=1000.0,
        volume_step=1.0, spread_points=30.0)


def trending(n, start=3300.0, drift=0.5, step_min=5, pullback_every=8):
    out, price = [], start
    for i in range(n):
        if pullback_every and i % pullback_every in (pullback_every - 2,
                                                     pullback_every - 1):
            o, c = price, price - drift * 0.55
        else:
            o, c = price, price + drift
        hi, lo = max(o, c) + 0.35, min(o, c) - 0.35
        out.append(Candle(T0 + timedelta(minutes=step_min * i), o, hi, lo, c,
                          volume=120))
        price = c
    return out


class TestConfigRails(unittest.TestCase):
    def test_defaults_valid(self):
        v = V4ConfigValidator(V4Config())
        self.assertTrue(v.validate(), v.report())

    def test_risk_rails_cannot_loosen(self):
        for kw in ({"max_risk_per_trade": 0.01},
                   {"max_daily_loss": 0.02},
                   {"max_weekly_drawdown": 0.10},
                   {"max_positions": 2},
                   {"min_net_rr": 1.5}):
            v = V4ConfigValidator(cfgv4(**kw))
            self.assertFalse(v.validate(), f"{kw} was accepted!")


class TestAdaptiveRisk(unittest.TestCase):
    def _choose(self, risk, **kw):
        base = dict(equity=10_000.0, day_start_equity=10_000.0,
                    daily_pl_combined=0.0, weekly_dd_frac=0.0,
                    consecutive_losses=0, spread_points=30.0,
                    atr_percentile=0.5, recent_strategy_r=1.0)
        base.update(kw)
        return risk.choose(0.5, 25, **base)

    def test_absolute_cap(self):
        r, _ = self._choose(AdaptiveRisk(V4Config()))
        self.assertLessEqual(r, 0.0075 + 1e-12)
        self.assertGreater(r, 0.0)

    def test_small_sample_gets_experimental_tier(self):
        risk = AdaptiveRisk(V4Config())
        r, _ = risk.choose(0.5, 2, equity=10_000, day_start_equity=10_000,
                           daily_pl_combined=0, weekly_dd_frac=0,
                           consecutive_losses=0, spread_points=30,
                           atr_percentile=0.5, recent_strategy_r=1.0)
        self.assertLessEqual(r, V4Config().risk_tier_experimental[1] + 1e-12)

    def test_losses_reduce_never_increase(self):
        risk = AdaptiveRisk(V4Config())
        r0, _ = self._choose(risk, consecutive_losses=0)
        r2, _ = self._choose(risk, consecutive_losses=2)
        self.assertLess(r2, r0)

    def test_daily_headroom_caps_then_blocks(self):
        risk = AdaptiveRisk(V4Config())
        # down 1.6% combined: only ~0.09% risk may remain (worst case
        # -1.69% stays inside the 1.7% ceiling)
        r, notes = self._choose(risk, daily_pl_combined=-160.0)
        self.assertLessEqual(r, (0.017 - 0.016) * 0.9 + 1e-12, notes)
        self.assertGreater(r, 0.0)
        # down 1.66%: headroom below the minimum useful risk -> no trade
        r2, notes2 = self._choose(risk, daily_pl_combined=-166.0)
        self.assertEqual(r2, 0.0, notes2)

    def test_weekly_headroom_blocks(self):
        risk = AdaptiveRisk(V4Config())
        r, notes = self._choose(risk, weekly_dd_frac=0.0499)
        self.assertEqual(r, 0.0, notes)


class TestV4Guard(unittest.TestCase):
    def _guard(self):
        g = V4Guard(V4Config())
        g.roll(T0, 10_000.0, 10_000.0)
        return g

    def test_combined_daily_lock_at_1_7(self):
        g = self._guard()
        g.register_close(-100.0, T0)             # -1.0% realised
        self.assertEqual(g.lock_reason(9_900.0, unrealised=-75.0, now=T0),
                         LockReason.DAILY_LOSS)  # combined -1.75%
        self.assertEqual(g.lock_reason(9_900.0, unrealised=-20.0, now=T0),
                         LockReason.NONE)        # combined -1.2%

    def test_weekly_5pct_lock(self):
        g = self._guard()
        g.week.min_equity = 9_400.0              # 6% below week start
        self.assertEqual(g.lock_reason(9_500.0, now=T0),
                         LockReason.WEEKLY_LOSS)

    def test_cooldown_after_three_losses(self):
        g = self._guard()
        for _ in range(3):
            g.register_close(-10.0, T0)
        self.assertEqual(g.lock_reason(9_970.0, now=T0 + timedelta(minutes=5)),
                         LockReason.CONSECUTIVE_LOSSES)
        after = T0 + timedelta(hours=V4Config().loss_cooldown_hours,
                               minutes=1)
        self.assertEqual(g.lock_reason(9_970.0, now=after), LockReason.NONE)
        self.assertEqual(g.consecutive_losses, 0)  # streak reset after pause

    def test_restart_restore_is_never_less_restrictive(self):
        g = self._guard()
        g.register_close(-50.0, T0)
        snap = g.snapshot()
        g2 = V4Guard(V4Config())
        g2.roll(T0, 10_000.0, 10_000.0)
        g2.register_close(-120.0, T0)            # broker replay found more
        g2.restore(snap, T0)
        self.assertEqual(g2.day.realised, -120.0)   # kept the worse figure
        g3 = V4Guard(V4Config())
        g3.roll(T0, 10_000.0, 10_000.0)          # replay found nothing
        g3.restore(snap, T0)
        self.assertEqual(g3.day.realised, -50.0)    # state prevented reset

    def test_new_day_clears(self):
        g = self._guard()
        g.register_close(-170.0, T0)
        self.assertEqual(g.lock_reason(9_830.0, now=T0),
                         LockReason.DAILY_LOSS)
        g.roll(T0 + timedelta(days=1), 9_830.0, 9_830.0)
        self.assertEqual(g.lock_reason(9_830.0, now=T0 + timedelta(days=1)),
                         LockReason.NONE)


class TestSizingV4(unittest.TestCase):
    def test_risk_capped_at_075(self):
        sizer = PositionSizer(V4Config())
        r = sizer.size(gold_spec(), equity=10_000.0, risk_fraction=0.05,
                       entry=3300.0, stop=3297.0)
        self.assertFalse(r.rejected)
        self.assertLessEqual(r.risk_money, 75.0 + 1e-9)

    def test_min_volume_rejects(self):
        spec = gold_spec()
        spec.volume_min = 500.0
        r = PositionSizer(V4Config()).size(spec, 10_000.0, 0.0025,
                                           3300.0, 3298.0)
        self.assertTrue(r.rejected)


class TestStrategySpace(unittest.TestCase):
    def test_seed_is_deterministic_and_diverse(self):
        a = seed_population("2026-01-06")
        b = seed_population("2026-01-06")
        self.assertEqual([s.sid for s in a], [s.sid for s in b])
        archetypes = {s.archetype for s in a}
        self.assertGreaterEqual(len(archetypes), 8)
        self.assertGreaterEqual(len(a), 16)
        for s in a:
            self.assertTrue(s.regimes)
            self.assertTrue(s.sessions)
            self.assertIn(s.mgmt_mode, ("FULL_TP", "BE_1R", "PARTIAL_RUNNER",
                                        "ATR_TRAIL", "STRUCT_TRAIL"))

    def _ctx(self, regime="STRONG_BULL", session="LONDON"):
        return EvalContext(regime=regime, session=session,
                           spread_points=30.0, point=0.01,
                           now=T0 + timedelta(hours=30), cost=0.45,
                           min_net_rr=2.0)

    def test_evaluation_deterministic(self):
        cfg = V4Config()
        fb = FeatureBuilder(cfg)
        candles = trending(260)
        f = fb.build(Timeframe.M5, candles)
        self.assertIsNotNone(f)
        pop = seed_population("2026-01-06")
        ctx = self._ctx()
        for s in pop:
            if s.tf != "M5":
                continue
            a = evaluate_strategy(s, f, ctx)
            b = evaluate_strategy(s, f, ctx)
            if a is None:
                self.assertIsNone(b)
            else:
                self.assertEqual((a.direction, a.stop, a.target),
                                 (b.direction, b.stop, b.target))
                self.assertGreaterEqual(a.rr(), 2.0 * 0.9)

    def test_regime_whitelist_enforced(self):
        pop = seed_population("2026-01-06")
        fade = next(s for s in pop if s.archetype == "RANGE_FADE")
        cfg = V4Config()
        f = FeatureBuilder(cfg).build(Timeframe.M5, trending(260))
        self.assertIsNone(evaluate_strategy(fade, f,
                                            self._ctx(regime="STRONG_BULL")))

    def test_mutation_bounded_and_versioned(self):
        pop = seed_population("2026-01-06")
        rng = random.Random(7)
        for parent in pop:
            child = mutate_strategy(parent, rng, 1, "2026-01-07")
            self.assertEqual(child.version, parent.version + 1)
            self.assertEqual(child.archetype, parent.archetype)
            bounds = PARAM_BOUNDS[child.archetype]
            for k, v in child.params.items():
                if k in bounds:
                    lo, hi = bounds[k]
                    self.assertTrue(lo - 1e-9 <= v <= hi + 1e-9,
                                    f"{child.sid} {k}={v} outside [{lo},{hi}]")


class _Sink:
    def __init__(self):
        self.rows = []
        self.closed = []
        self.watch = []

    def log(self, m):
        pass


class TestShadowEngine(unittest.TestCase):
    def _engine(self, sink):
        return ShadowEngine(V4Config(), sink.log, sink.closed.append,
                            sink.rows.append, sink.watch.append)

    def _signal(self, scfg, entry=3300.0, stop=3297.0, target=3306.5):
        return Signal(strategy_id=scfg.sid, tf=scfg.tf,
                      direction=Direction.LONG, entry_ref=entry, stop=stop,
                      target=target, reason="test", confluence=3, created=T0)

    def test_fill_is_next_candle_open_no_lookahead(self):
        sink = _Sink()
        eng = self._engine(sink)
        scfg = seed_population("2026-01-06")[0]
        self.assertTrue(eng.submit(scfg, self._signal(scfg), 30.0, 0.01,
                                   "STRONG_BULL", "LONDON"))
        # same-time candle must NOT fill (signal bar itself)
        c_same = Candle(T0, 3300.0, 3301.0, 3299.5, 3300.5)
        eng.on_m1(c_same, 30.0, 0.01, T0 + timedelta(minutes=1), {}, {})
        self.assertEqual(len(eng.open), 0)
        c_next = Candle(T0 + timedelta(minutes=1), 3300.8, 3301.5, 3300.2,
                        3301.0)
        eng.on_m1(c_next, 30.0, 0.01, T0 + timedelta(minutes=2), {}, {})
        self.assertEqual(len(eng.open), 1)
        t = eng.open[scfg.sid]
        self.assertEqual(t.entry, 3300.8 + 30 * 0.01 + 10 * 0.01)
        self.assertGreater(t.entry_time, t.signal_time)

    def test_stop_first_when_both_hit(self):
        sink = _Sink()
        eng = self._engine(sink)
        scfg = seed_population("2026-01-06")[0]
        eng.submit(scfg, self._signal(scfg), 30.0, 0.01, "RANGE", "LONDON")
        eng.on_m1(Candle(T0 + timedelta(minutes=1), 3300.0, 3300.5, 3299.5,
                         3300.2), 30.0, 0.01, T0 + timedelta(minutes=2),
                  {}, {})
        # one candle spans BOTH stop and target -> stop must win
        eng.on_m1(Candle(T0 + timedelta(minutes=2), 3300.0, 3310.0, 3290.0,
                         3305.0), 30.0, 0.01, T0 + timedelta(minutes=3),
                  {}, {})
        self.assertEqual(len(sink.closed), 1)
        self.assertEqual(sink.closed[0].exit_reason, "STOP_LOSS")
        self.assertLess(sink.closed[0].r_multiple, 0)

    def test_post_stop_watch_records_target_hit(self):
        sink = _Sink()
        eng = self._engine(sink)
        scfg = seed_population("2026-01-06")[0]
        eng.submit(scfg, self._signal(scfg), 30.0, 0.01, "RANGE", "LONDON")
        eng.on_m1(Candle(T0 + timedelta(minutes=1), 3300.0, 3300.5, 3299.5,
                         3300.2), 30.0, 0.01, T0 + timedelta(minutes=2),
                  {}, {})
        eng.on_m1(Candle(T0 + timedelta(minutes=2), 3300.0, 3300.2, 3296.5,
                         3297.0), 30.0, 0.01, T0 + timedelta(minutes=3),
                  {}, {})                      # stop-out
        self.assertEqual(len(eng.watching), 1)
        eng.on_m1(Candle(T0 + timedelta(minutes=3), 3297.0, 3307.0, 3296.9,
                         3306.9), 30.0, 0.01, T0 + timedelta(minutes=4),
                  {}, {})                      # target reached after stop
        self.assertEqual(len(sink.watch), 1)
        self.assertTrue(sink.watch[0].watch_target_hit)

    def test_one_virtual_position_per_strategy(self):
        sink = _Sink()
        eng = self._engine(sink)
        scfg = seed_population("2026-01-06")[0]
        self.assertTrue(eng.submit(scfg, self._signal(scfg), 30, 0.01,
                                   "RANGE", "LONDON"))
        self.assertFalse(eng.submit(scfg, self._signal(scfg), 30, 0.01,
                                    "RANGE", "LONDON"))


class TestLearning(unittest.TestCase):
    def _book(self):
        return LearningBook(V4Config(), lambda m: None)

    def _feed(self, book, sid, rs, regime="RANGE"):
        for i, r in enumerate(rs):
            book.get(sid).record(r=r, regime=regime, session="LONDON",
                                 dow="Tue", mfe=max(r, 0.2), mae=0.3,
                                 bars=30, is_real=False)

    def test_one_lucky_win_does_not_dominate(self):
        book = self._book()
        self._feed(book, "LUCKY", [2.5])
        self._feed(book, "STEADY", [0.8, 1.2, -0.5, 1.0, 0.9, 1.1, -0.4,
                                    0.7, 1.3, 0.6])
        pop = seed_population("2026-01-06")[:2]
        pop[0].sid, pop[1].sid = "LUCKY", "STEADY"
        ranked = book.ranking(pop)
        self.assertEqual(ranked[0][0].sid, "STEADY")

    def test_real_eligibility_needs_evidence(self):
        book = self._book()
        pop = seed_population("2026-01-06")
        s = pop[0]
        self.assertFalse(book.eligible_for_real(s, "RANGE"))   # no trades
        self._feed(book, s.sid, [0.5, -0.4, 0.9, 0.7, 1.1])
        self.assertTrue(book.eligible_for_real(s, "RANGE"))
        self._feed(book, s.sid, [-1.0] * 12)                   # now negative
        self.assertFalse(book.eligible_for_real(s, "RANGE"))

    def test_daily_update_retires_and_spawns(self):
        cfg = V4Config()
        book = LearningBook(cfg, lambda m: None)
        pop = seed_population("2026-01-06")
        loser, winner = pop[0], pop[1]
        self._feed(book, loser.sid, [-1.0] * 12)
        self._feed(book, winner.sid, [1.0, 0.8, 1.2, -0.5, 0.9, 1.1, 0.7])
        n_before = len(pop)
        pop2, decisions = book.daily_update(pop, random.Random(1),
                                            "2026-01-08")
        kinds = {d["kind"] for d in decisions}
        self.assertIn("RETIRE", kinds)
        self.assertIn("SPAWN", kinds)
        self.assertEqual(next(p for p in pop2 if p.sid == loser.sid).status,
                         "retired")
        self.assertGreater(len(pop2), n_before)

    def test_adapt_widens_tight_stops_with_evidence(self):
        cfg = V4Config()
        book = LearningBook(cfg, lambda m: None)
        pop = seed_population("2026-01-06")
        s = next(p for p in pop if "buffer_atr" in p.params)
        st = book.get(s.sid)
        self._feed(book, s.sid, [0.5, -1.0, -1.0, 0.7, -1.0, 0.9, -1.0, 0.4])
        st.stop_watch, st.stop_tight = 10, 5          # 50% too tight
        old = s.params["buffer_atr"]
        old_version = s.version
        book.daily_update(pop, random.Random(2), "2026-01-08")
        self.assertGreater(s.params["buffer_atr"], old)
        self.assertEqual(s.version, old_version + 1)

    def test_learning_state_roundtrip(self):
        book = self._book()
        self._feed(book, "X", [1.0, -0.5, 0.8])
        snap = json.loads(json.dumps(book.snapshot()))   # via JSON like disk
        book2 = self._book()
        book2.restore(snap)
        self.assertEqual(book2.get("X").n, 3)
        self.assertAlmostEqual(book2.get("X").sum_r, 1.3)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_state_roundtrip_and_research_clock(self):
        cfg = cfgv4(state_dir=self.tmp)
        store = StateStore(cfg, lambda m: None)
        start = T0 - timedelta(days=3)
        store.save_state({"research_start": start.isoformat(),
                          "population": [s.to_dict() for s in
                                         seed_population("2026-01-03")],
                          "final_report_done": False})
        store2 = StateStore(cfg, lambda m: None)
        state = store2.load_state()
        self.assertEqual(state["research_start"], start.isoformat())
        self.assertGreaterEqual(len(state["population"]), 16)
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "research_state.json")))

    def test_corrupt_state_preserved_not_deleted(self):
        cfg = cfgv4(state_dir=self.tmp)
        store = StateStore(cfg, lambda m: None)
        path = os.path.join(self.tmp, "research_state.json")
        with open(path, "w") as fh:
            fh.write("{ not json")
        self.assertIsNone(store.load_state())
        self.assertTrue(os.path.exists(path + ".corrupt"))

    def test_csv_append_headers_once(self):
        cfg = cfgv4(state_dir=self.tmp)
        store = StateStore(cfg, lambda m: None)
        row = {"time": "t", "stage": "s", "strategy_id": "x",
               "regime": "RANGE", "session": "LONDON",
               "spread_points": "30", "reason": "r"}
        store.csv_append("rejections", row)
        store.csv_append("rejections", row)
        with open(os.path.join(self.tmp, "rejections.csv")) as fh:
            lines = fh.read().strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("time,"))


class TestReporting(unittest.TestCase):
    def test_final_report_labels_and_confidence(self):
        book = LearningBook(V4Config(), lambda m: None)
        pop = seed_population("2026-01-06")
        for r in (1.0, -0.5, 0.8, 1.2, -0.4, 0.9):
            book.get(pop[0].sid).record(r=r, regime="RANGE",
                                        session="LONDON", dow="Tue",
                                        mfe=1.0, mae=0.3, bars=25,
                                        is_real=False)
        text, js = final_report(T0 + timedelta(days=14), T0, book, pop,
                                {"final_equity": "10100.00"}, [])
        self.assertIn(PROVISIONAL_LABEL, text)
        self.assertIn("confidence", text.lower())
        self.assertIn("out-of-sample", text)
        self.assertEqual(js["label"], PROVISIONAL_LABEL)
        self.assertIsNotNone(js["winner"])


if __name__ == "__main__":
    unittest.main()
