"""Offline test suite -- run with:  python -m unittest discover tests -v"""

import math
import time
import unittest

from oanda_bot.config import (CouncilConfig, LearningConfig, NewsConfig,
                              RiskConfig)
from oanda_bot.council import SessionAnalyst, SpreadWatcher, TradeCouncil
from oanda_bot.indicators import Candle, adx, atr, bollinger, ema, macd, rsi, sma
from oanda_bot.journal import Journal, TradeRecord
from oanda_bot.learning import PerformanceCoach
from oanda_bot.news import CalendarEvent, NewsSentry
from oanda_bot.oanda import FALLBACK_SPECS
from oanda_bot.risk import RiskManager
from oanda_bot.strategies import Signal, Strategy


def make_candles(n=300, start=100.0, step=0.1,
                 ts0=1_767_571_200.0 + 9 * 3600):
    """Simple rising series, one M5 candle each, starting Monday 09:00 UTC
    so that 300 candles later the clock reads Tuesday 10:00 -- inside the
    default trading session."""
    out = []
    price = start
    for i in range(n):
        o = price
        c = price + step
        out.append(Candle(time=ts0 + i * 300, open=o, high=max(o, c) + 0.05,
                          low=min(o, c) - 0.05, close=c, volume=100))
        price = c
    return out


class TestIndicators(unittest.TestCase):
    def test_sma_ema_align(self):
        vals = [float(i) for i in range(1, 51)]
        s, e = sma(vals, 10), ema(vals, 10)
        self.assertIsNone(s[8])
        self.assertAlmostEqual(s[9], 5.5)
        self.assertEqual(len(e), 50)
        self.assertIsNotNone(e[-1])

    def test_rsi_bounds(self):
        up = [float(i) for i in range(60)]           # straight up -> RSI 100
        r = rsi(up, 14)
        self.assertAlmostEqual(r[-1], 100.0)
        down = [60.0 - i for i in range(60)]
        self.assertLess(rsi(down, 14)[-1], 1.0)

    def test_atr_positive(self):
        a = atr(make_candles(100), 14)
        self.assertGreater(a[-1], 0)

    def test_macd_bollinger_adx_run(self):
        candles = make_candles(200)
        closes = [c.close for c in candles]
        line, sig, hist = macd(closes)
        self.assertIsNotNone(hist[-1])
        mid, up, lo = bollinger(closes)
        self.assertLess(lo[-1], up[-1])
        self.assertIsNotNone(adx(candles, 14)[-1])


class TestRisk(unittest.TestCase):
    def test_sizing_math(self):
        rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0,
                                    max_notional_leverage=20.0),
                         Journal(":memory:"))
        spec = FALLBACK_SPECS["XAU_USD"]
        sized = rm.size(direction=1, price=3300.0, atr_value=2.0,
                        balance=10_000.0, spec=spec)
        # risk 100 USD, stop dist 3.0 -> 33 units
        self.assertEqual(sized.units, 33)
        self.assertAlmostEqual(sized.stop_price, 3297.0)
        self.assertLess(abs(sized.risk_usd - 99.0), 1e-6)

    def test_notional_cap_binds(self):
        rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0,
                                    max_notional_leverage=5.0),
                         Journal(":memory:"))
        sized = rm.size(1, 3300.0, 2.0, balance=10_000.0,
                        spec=FALLBACK_SPECS["XAU_USD"])
        # capped at 50,000 USD notional -> floor(15.15) = 15 units
        self.assertEqual(sized.units, 15)
        self.assertLessEqual(sized.notional_usd, 50_000.0)

    def test_min_size_skip(self):
        rm = RiskManager(RiskConfig(risk_per_trade_pct=0.1), Journal(":memory:"))
        sized = rm.size(1, 3300.0, 5.0, balance=1_000.0,
                        spec=FALLBACK_SPECS["XAU_USD"])
        self.assertIsNone(sized)      # 0.13 units < min 1

    def test_daily_loss_gate(self):
        j = Journal(":memory:")
        rm = RiskManager(RiskConfig(daily_loss_limit_pct=3.0), j)
        now = time.time()
        tid = j.record_open(TradeRecord("XAU_USD", 1, 1, 3300, 3295, 3310,
                                        risk_usd=100, opened_at=now))
        j.record_close(tid, 3295.0, -400.0, closed_at=now)
        gate = rm.daily_gate(balance=10_000.0, now=now)
        self.assertIn("loss limit", gate)

    def test_breakeven_only_tightens(self):
        rm = RiskManager(RiskConfig(breakeven_at_r=1.0), Journal(":memory:"))
        # long from 100 stop 99: at 101 (+1R) -> stop moves above entry
        ns = rm.breakeven_stop(1, 100.0, 99.0, 101.0)
        self.assertGreater(ns, 100.0)
        # not yet at +1R -> no move
        self.assertIsNone(rm.breakeven_stop(1, 100.0, 99.0, 100.5))


class TestJournalAndLearning(unittest.TestCase):
    def _closed_trade(self, j, r_sign=1.0, confirmations=("a", "b")):
        tid = j.record_open(TradeRecord(
            "EUR_USD", 1, 1000, 1.08, 1.075, 1.09, risk_usd=50.0,
            regime="trending_up", confirmations=list(confirmations)))
        return j.record_close(tid, 1.09, 75.0 * r_sign)

    def test_weights_move_with_outcomes(self):
        j = Journal(":memory:")
        coach = PerformanceCoach(LearningConfig(), j)
        t = self._closed_trade(j, r_sign=1.0)
        coach.learn_from_close(t, base_threshold=1.2)
        self.assertGreater(coach.weight("a"), 1.0)
        t2 = self._closed_trade(j, r_sign=-1.0)
        w_before = coach.weight("a")
        coach.learn_from_close(t2, base_threshold=1.2)
        self.assertLess(coach.weight("a"), w_before)

    def test_combo_veto_after_losses(self):
        j = Journal(":memory:")
        cfg = LearningConfig(combo_min_trades=5)
        coach = PerformanceCoach(cfg, j)
        for _ in range(6):
            t = self._closed_trade(j, r_sign=-1.0)
            coach.learn_from_close(t, base_threshold=1.2)
        verdict = coach.combo_verdict(["a", "b"], "trending_up")
        self.assertTrue(verdict.veto)

    def test_daily_pnl(self):
        j = Journal(":memory:")
        self._closed_trade(j, r_sign=1.0)
        self.assertAlmostEqual(j.daily_pnl(), 75.0)


class _FakeStrategy(Strategy):
    def __init__(self, name, direction, confidence=0.9):
        self.name = name
        self.preferred_regimes = ("trending_up", "ranging")
        self._sig = Signal(name, direction, confidence, "fake",
                           self.preferred_regimes)

    def evaluate(self, m5, h1):
        return self._sig


class TestCouncil(unittest.TestCase):
    def _council(self, strategies):
        j = Journal(":memory:")
        coach = PerformanceCoach(LearningConfig(), j)
        news = NewsSentry(NewsConfig(enabled=False))
        return TradeCouncil(CouncilConfig(), coach, news, strategies)

    def test_two_confirmations_approve(self):
        council = self._council([_FakeStrategy("s1", 1), _FakeStrategy("s2", 1)])
        candles = make_candles(300)
        # Tuesday 10:00 UTC, inside session
        now = candles[-1].time
        d = council.evaluate("XAU_USD", candles, candles, spread=0.01,
                             now_ts=now, check_news=False)
        self.assertTrue(d.approved, d.explain())
        self.assertEqual(sorted(d.confirmations), ["s1", "s2"])

    def test_single_vote_is_not_enough(self):
        council = self._council([_FakeStrategy("s1", 1)])
        candles = make_candles(300)
        d = council.evaluate("XAU_USD", candles, candles, 0.01,
                             candles[-1].time, check_news=False)
        self.assertFalse(d.approved)

    def test_conflicting_votes_cancel(self):
        council = self._council([
            _FakeStrategy("s1", 1), _FakeStrategy("s2", -1),
            _FakeStrategy("s3", 1)])
        candles = make_candles(300)
        d = council.evaluate("XAU_USD", candles, candles, 0.01,
                             candles[-1].time, check_news=False)
        # net score of one 0.9-conf vote (regime-discounted) < threshold 1.2
        self.assertFalse(d.approved)

    def test_wide_spread_vetoes(self):
        council = self._council([_FakeStrategy("s1", 1), _FakeStrategy("s2", 1)])
        candles = make_candles(300)
        d = council.evaluate("XAU_USD", candles, candles, spread=50.0,
                             now_ts=candles[-1].time, check_news=False)
        self.assertFalse(d.approved)
        self.assertTrue(any("spread" in v for v in d.vetoes))


class TestSessionAnalyst(unittest.TestCase):
    def test_weekend_blocked(self):
        sa = SessionAnalyst(CouncilConfig())
        # 2026-01-10 is a Saturday
        import datetime as dt
        sat = dt.datetime(2026, 1, 10, 12, 0,
                          tzinfo=dt.timezone.utc).timestamp()
        self.assertIsNotNone(sa.check(sat))

    def test_session_hours(self):
        sa = SessionAnalyst(CouncilConfig())
        import datetime as dt
        tue_10 = dt.datetime(2026, 1, 6, 10, 0,
                             tzinfo=dt.timezone.utc).timestamp()
        tue_23 = dt.datetime(2026, 1, 6, 23, 0,
                             tzinfo=dt.timezone.utc).timestamp()
        self.assertIsNone(sa.check(tue_10))
        self.assertIsNotNone(sa.check(tue_23))


class TestNewsSentry(unittest.TestCase):
    def test_blocks_inside_window(self):
        sentry = NewsSentry(NewsConfig())
        now = time.time()
        sentry._events = [CalendarEvent("NFP", "USD", "High", now + 600)]
        sentry._last_success = now
        sentry._last_fetch = now
        self.assertTrue(sentry.check("XAU_USD", now).blocked)
        self.assertTrue(sentry.check("EUR_USD", now).blocked)

    def test_irrelevant_currency_ignored(self):
        sentry = NewsSentry(NewsConfig())
        now = time.time()
        sentry._events = [CalendarEvent("BoJ", "JPY", "High", now + 600)]
        sentry._last_success = now
        sentry._last_fetch = now
        self.assertFalse(sentry.check("EUR_USD", now).blocked)

    def test_fail_closed_when_stale(self):
        sentry = NewsSentry(NewsConfig(fail_closed=True))
        now = time.time()
        sentry._last_fetch = now          # pretend we just tried and failed
        self.assertTrue(sentry.check("XAU_USD", now).blocked)


if __name__ == "__main__":
    unittest.main()
