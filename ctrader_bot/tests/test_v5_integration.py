"""
Integration tests for XAUUSD_Adaptive_Bot_V5.

These import and drive the GENERATED SINGLE FILE — XAUUSD_Adaptive_Bot_V5_main.py,
the artefact actually pasted into cTrader — against a mocked cTrader API, so
what is verified here is the deployable file, not just the modular source.

Covered: the demo-only lock, the gold-only lock, a clean start on synthetic
market data, continuous tick processing with heartbeats, a real order placed
through the normal selection path with broker-side SL/TP and risk inside the
0.25% ceiling, the one-real-position rule, settlement from broker history
including a partial close, the daily-guard replay, emergency stop, and full
restart recovery.

Run from the ctrader_bot directory:
    python3 -m unittest tests.test_v5_integration -v
"""

import importlib.util
import math
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT_DIR)

UTC = timezone.utc
SINGLE_FILE = os.path.join(BOT_DIR, "XAUUSD_Adaptive_Bot_V5_main.py")


# ===========================================================================
# a mocked cTrader platform
# ===========================================================================

class NetTime(object):
    """Stands in for .NET DateTime, which exposes capitalised properties."""

    def __init__(self, dt):
        self.Year, self.Month, self.Day = dt.year, dt.month, dt.day
        self.Hour, self.Minute, self.Second = dt.hour, dt.minute, dt.second


class Column(object):
    def __init__(self, values):
        self.values = values

    def __getitem__(self, i):
        return self.values[i]

    def __len__(self):
        return len(self.values)


class FakeBars(object):
    def __init__(self, candles):
        self.set(candles)

    def set(self, candles):
        self._candles = candles
        self.OpenTimes = Column([NetTime(c[0]) for c in candles])
        self.OpenPrices = Column([c[1] for c in candles])
        self.HighPrices = Column([c[2] for c in candles])
        self.LowPrices = Column([c[3] for c in candles])
        self.ClosePrices = Column([c[4] for c in candles])
        self.TickVolumes = Column([c[5] for c in candles])

    @property
    def Count(self):
        return len(self._candles)


class MarketHours(object):
    def __init__(self):
        self.opened = True

    def IsOpened(self):
        return self.opened


class FakeSymbol(object):
    def __init__(self, name="XAUUSD", bid=3400.0, spread=0.30):
        self.name = name
        self.Bid = bid
        self.Ask = bid + spread
        self.Digits = 2
        self.TickSize = 0.01
        self.TickValue = 0.01
        self.PipSize = 0.10
        self.PipValue = 0.10
        self.VolumeInUnitsMin = 1.0
        self.VolumeInUnitsMax = 100000.0
        self.VolumeInUnitsStep = 1.0
        self.MarketHours = MarketHours()


class FakeAccount(object):
    def __init__(self, is_live=False, equity=10000.0):
        self.IsLive = is_live
        self.Equity = equity
        self.Balance = equity
        self.Currency = "USD"


class FakePosition(object):
    def __init__(self, pid, symbol, label, trade_type, entry, units, sl, tp,
                 entry_time):
        self.Id = pid
        self.SymbolName = symbol
        self.Label = label
        self.TradeType = trade_type
        self.EntryPrice = entry
        self.VolumeInUnits = units
        self.StopLoss = sl
        self.TakeProfit = tp
        self.NetProfit = 0.0
        self.EntryTime = NetTime(entry_time)


class FakeHistoricalTrade(object):
    def __init__(self, pid, symbol, label, net, closing_price, closing_time,
                 gross=None, commission=0.0, swap=0.0):
        self.PositionId = pid
        self.SymbolName = symbol
        self.Label = label
        self.NetProfit = net
        self.GrossProfit = net if gross is None else gross
        self.Commission = commission
        self.Swap = swap
        self.ClosingPrice = closing_price
        self.ClosingTime = NetTime(closing_time)


class Collection(object):
    def __init__(self, items=None):
        self.items = list(items or [])

    @property
    def Count(self):
        return len(self.items)

    def __getitem__(self, i):
        return self.items[i]


class OrderResult(object):
    def __init__(self, ok, position=None, error=None):
        self.IsSuccessful = ok
        self.Position = position
        self.Error = error


class Server(object):
    def __init__(self, now):
        self.Time = NetTime(now)


class MarketData(object):
    def __init__(self, bars_by_tf):
        self.bars = bars_by_tf

    def GetBars(self, tf):
        return self.bars[tf]


def zigzag(n, start=3300.0, drift=0.02, amp=0.6, period=90, t0=None,
           minutes=1, rng=0.35):
    """A trending M1 series with clean alternating swings."""
    t0 = t0 or datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
    out = []
    prev = start
    for i in range(n):
        base = start + drift * i + amp * math.sin(2 * math.pi * i / period)
        o, c = prev, base
        out.append((t0 + timedelta(minutes=minutes * i), o,
                    max(o, c) + rng / 2, min(o, c) - rng / 2, c, 100.0))
        prev = c
    return out


def resample_tuples(m1, minutes):
    """Aggregate the master M1 tuples into a slower timeframe."""
    buckets = {}
    order = []
    for t, o, h, l, c, v in m1:
        total = t.hour * 60 + t.minute
        start = t.replace(hour=(total // minutes * minutes) // 60,
                          minute=(total // minutes * minutes) % 60)
        key = (t.date(), start.hour, start.minute)
        if key not in buckets:
            buckets[key] = [start, o, h, l, c, v]
            order.append(key)
        else:
            b = buckets[key]
            b[2] = max(b[2], h)
            b[3] = min(b[3], l)
            b[4] = c
            b[5] += v
    return [tuple(buckets[k]) for k in order]


class FakePlatform(object):
    """Everything the cBot touches, with a cursor over the master series."""

    TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}

    def __init__(self, total_minutes=5400, warmup=4800, symbol="XAUUSD",
                 is_live=False, equity=10000.0):
        self.master = zigzag(total_minutes)
        self.cursor = warmup
        self.symbol = FakeSymbol(symbol, bid=self.master[warmup - 1][4])
        self.SymbolName = symbol
        self.Account = FakeAccount(is_live, equity)
        self.positions = []
        self.history = []
        self.PendingOrders = Collection([])
        self.prints = []
        self.stopped = False
        self.next_pid = 1000
        self._bars = {}
        self._rebuild()
        self.Server = Server(self.master[self.cursor - 1][0])
        self.MarketData = MarketData(self._bars)
        self.closed_calls = []
        self.modify_calls = []

    # -- plumbing ---------------------------------------------------------
    def _rebuild(self):
        window = self.master[:self.cursor]
        for name, minutes in self.TF_MINUTES.items():
            series = window if minutes == 1 else resample_tuples(window,
                                                                 minutes)
            if name in self._bars:
                self._bars[name].set(series)
            else:
                self._bars[name] = FakeBars(series)

    def advance(self, minutes=1):
        for _ in range(minutes):
            if self.cursor >= len(self.master):
                return False
            self.cursor += 1
            self._rebuild()
            last = self.master[self.cursor - 1]
            self.symbol.Bid = last[4]
            self.symbol.Ask = last[4] + 0.30
            self.Server = Server(last[0])
        return True

    @property
    def Symbol(self):
        return self.symbol

    @property
    def Positions(self):
        return Collection(self.positions)

    @property
    def History(self):
        return Collection(self.history)

    def now_utc(self):
        return self.master[self.cursor - 1][0]

    # -- api surface -------------------------------------------------------
    def Print(self, msg):
        self.prints.append(str(msg))

    def Stop(self):
        self.stopped = True

    def ExecuteMarketOrder(self, trade_type, symbol, volume, label, sl_pips,
                           tp_pips):
        if volume <= 0:
            return OrderResult(False, error="invalid volume")
        self.next_pid += 1
        is_buy = str(trade_type) == "Buy"
        entry = self.symbol.Ask if is_buy else self.symbol.Bid
        sl = entry - sl_pips * self.symbol.PipSize if is_buy \
            else entry + sl_pips * self.symbol.PipSize
        tp = entry + tp_pips * self.symbol.PipSize if is_buy \
            else entry - tp_pips * self.symbol.PipSize
        pos = FakePosition(self.next_pid, symbol, label,
                           "Buy" if is_buy else "Sell", entry, volume, sl, tp,
                           self.now_utc())
        self.positions.append(pos)
        return OrderResult(True, pos)

    def ModifyPosition(self, position, sl, tp):
        position.StopLoss = sl
        position.TakeProfit = tp
        self.modify_calls.append((position.Id, sl, tp))
        return OrderResult(True, position)

    def ClosePosition(self, position, volume=None):
        self.closed_calls.append((position.Id, volume))
        if volume is not None and volume < position.VolumeInUnits:
            position.VolumeInUnits -= volume
            self.history.append(FakeHistoricalTrade(
                position.Id, position.SymbolName, position.Label, 5.0,
                self.symbol.Bid, self.now_utc()))
            return OrderResult(True, position)
        self.positions = [p for p in self.positions if p.Id != position.Id]
        self.history.append(FakeHistoricalTrade(
            position.Id, position.SymbolName, position.Label, -7.5,
            self.symbol.Bid, self.now_utc()))
        return OrderResult(True, None)

    def settle(self, position, net, closing_price=None):
        """Simulate the broker closing a position (SL/TP hit)."""
        self.positions = [p for p in self.positions if p.Id != position.Id]
        self.history.append(FakeHistoricalTrade(
            position.Id, position.SymbolName, position.Label, net,
            closing_price if closing_price is not None else self.symbol.Bid,
            self.now_utc()))


def load_bot_module(platform):
    """Import the generated single file with a mocked cTrader environment."""
    clr_mod = types.ModuleType("clr")
    clr_mod.AddReference = lambda name: None
    api_mod = types.ModuleType("cAlgo.API")

    class TimeFrame(object):
        Minute = "M1"
        Minute5 = "M5"
        Minute15 = "M15"
        Minute30 = "M30"
        Hour = "H1"
        Hour4 = "H4"
        Daily = "D1"

    class TradeType(object):
        Buy = "Buy"
        Sell = "Sell"

    api_mod.TimeFrame = TimeFrame
    api_mod.TradeType = TradeType
    calgo_pkg = types.ModuleType("cAlgo")
    calgo_pkg.API = api_mod
    wrapper = types.ModuleType("robot_wrapper")
    wrapper.api = platform

    saved = {name: sys.modules.get(name) for name in
             ("clr", "cAlgo", "cAlgo.API", "robot_wrapper")}
    sys.modules["clr"] = clr_mod
    sys.modules["cAlgo"] = calgo_pkg
    sys.modules["cAlgo.API"] = api_mod
    sys.modules["robot_wrapper"] = wrapper
    try:
        spec = importlib.util.spec_from_file_location(
            "v5_single_file", SINGLE_FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    module.api = platform
    return module


def start_bot(module, state_dir):
    """Start a cBot instance whose state directory is the test's temp dir.

    The config is patched BEFORE on_start so no test ever reads or writes the
    real ~/Documents/XAUUSD_Adaptive_Bot_V5 directory."""
    original = module.V5Config

    def patched():
        cfg = original()
        cfg.state_dir = state_dir
        return cfg

    module.V5Config = patched
    try:
        bot = module.XAUUSD_Adaptive_Bot_V5()
        bot.on_start()
    finally:
        module.V5Config = original
    return bot


class BotFixture(object):
    """A started bot, its platform and its state directory."""

    def __init__(self, state_dir, symbol="XAUUSD", is_live=False,
                 equity=10000.0, start_minutes=4800):
        self.state_dir = state_dir
        self.platform = FakePlatform(symbol=symbol, is_live=is_live,
                                     equity=equity, warmup=start_minutes)
        self.module = load_bot_module(self.platform)
        self.bot = start_bot(self.module, state_dir)

    def restart(self, state_dir=None):
        """A fresh instance over the same platform and state directory."""
        module = load_bot_module(self.platform)
        return start_bot(module, state_dir or self.state_dir)


# ===========================================================================
# tests
# ===========================================================================

class TestSafetyLocks(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="v5int")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_live_account_is_refused(self):
        fx = BotFixture(self.dir, is_live=True)
        self.assertTrue(fx.bot._fatal)
        self.assertTrue(fx.platform.stopped)
        self.assertTrue(any("LIVE ACCOUNT BLOCKED" in p
                            for p in fx.platform.prints))

    def test_non_gold_symbol_is_refused(self):
        fx = BotFixture(self.dir, symbol="EURUSD")
        self.assertTrue(fx.bot._fatal)
        self.assertTrue(fx.platform.stopped)
        self.assertTrue(any("SYMBOL BLOCKED" in p
                            for p in fx.platform.prints))

    def test_gold_variants_are_accepted(self):
        for name in ("XAUUSD", "GOLD", "XAUUSD.PRO"):
            d = tempfile.mkdtemp(prefix="v5sym")
            try:
                fx = BotFixture(d, symbol=name)
                self.assertFalse(fx.bot._fatal, name)
            finally:
                shutil.rmtree(d, ignore_errors=True)

    def test_no_live_switch_exists_in_the_deployable_file(self):
        with open(SINGLE_FILE, encoding="utf-8") as fh:
            source = fh.read()
        lowered = source.lower()
        for forbidden in ("allow_live", "enable_live", "live_trading",
                          "is_live = false", "force_live"):
            self.assertNotIn(forbidden, lowered, forbidden)
        self.assertIn("LIVE ACCOUNT BLOCKED", source)


class TestStartupAndTicks(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="v5int")
        self.fx = BotFixture(self.dir)
        self.bot = self.fx.bot
        self.platform = self.fx.platform

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_starts_cleanly(self):
        self.assertFalse(self.bot._fatal)
        self.assertFalse(self.platform.stopped)
        self.assertTrue(any("DEMO account confirmed" in p
                            for p in self.platform.prints))
        self.assertTrue(any("accepted as GOLD" in p
                            for p in self.platform.prints))
        self.assertTrue(any("RESEARCH CLOCK STARTED" in p
                            for p in self.platform.prints))
        self.assertIsNotNone(self.bot.clock.start)

    def test_news_honesty_is_printed(self):
        self.assertTrue(any("NO live news feed" in p
                            for p in self.platform.prints))
        self.assertTrue(any("NEWS DATES MISSING" in p
                            for p in self.platform.prints))

    def test_population_covers_the_six_families(self):
        families = set(v.family for v in self.bot.population)
        self.assertEqual(len(families), 6)
        self.assertLessEqual(len(self.bot.population), 24)

    def test_ticks_run_without_error_and_heartbeat_fires(self):
        before = len(self.platform.prints)
        for _ in range(240):
            self.platform.advance(1)
            self.bot.on_tick()
        errors = [p for p in self.platform.prints[before:]
                  if "ERROR in tick processing" in p]
        self.assertEqual(errors, [], errors[:3])
        beats = [p for p in self.platform.prints if p.startswith("HEARTBEAT")]
        self.assertGreaterEqual(len(beats), 240 // self.bot.cfg.heartbeat_minutes
                                - 2, f"only {len(beats)} heartbeats")
        # the heartbeat must carry the fields the spec asks for
        sample = beats[-1]
        for field in ("day ", "session ", "regime ", "bias ", "spread ",
                      "news ", "shadows open ", "real ", "stop stage ",
                      "top:"):
            self.assertIn(field, sample, f"heartbeat missing {field!r}")

    def test_research_clock_accrues_active_minutes(self):
        for _ in range(120):
            self.platform.advance(1)
            self.bot.on_tick()
        total = sum(self.bot.clock.minutes.values())
        self.assertGreater(total, 100)

    def test_market_closed_does_not_accrue_research_time(self):
        self.platform.symbol.MarketHours.opened = False
        for _ in range(90):
            self.platform.advance(1)
            self.bot.on_tick()
        self.assertEqual(sum(self.bot.clock.minutes.values()), 0.0)

    def test_state_file_is_written(self):
        for _ in range(30):
            self.platform.advance(1)
            self.bot.on_tick()
        self.bot._save_state(self.bot._now_utc())
        self.assertTrue(os.path.exists(os.path.join(self.dir,
                                                    "research_state.json")))


class TestRealOrderPath(unittest.TestCase):
    """Drives a real order through the normal selection path."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="v5real")
        self.fx = BotFixture(self.dir)
        self.bot = self.fx.bot
        self.platform = self.fx.platform
        self.module = self.fx.module
        for _ in range(60):                    # build the market picture
            self.platform.advance(1)
            self.bot.on_tick()
        self.assertIsNotNone(self.bot._state, "market state never built")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _eligible_candidate(self, direction=None):
        module = self.module
        direction = direction or module.Direction.LONG
        variant = next(v for v in self.bot.population if v.status == "active")
        # give the variant and its family enough shadow evidence
        for _ in range(6):
            self.bot.book.record(variant.sid, variant.family, 0.9,
                                 self.bot._regime, "LONDON", "TAKE_PROFIT",
                                 2.2, 0.4, real=False)
        for _ in range(4):
            self.bot.book.record(variant.sid + "-sib", variant.family, 0.7,
                                 self.bot._regime, "LONDON", "TAKE_PROFIT",
                                 2.0, 0.5, real=False)
        state = self.bot._state
        atr = max(state.atr_m5, 1.0)
        bid = float(self.platform.symbol.Bid)
        risk = max(1.10 * atr, 1.20)
        if direction == module.Direction.LONG:
            stop, tp1, target = bid - risk, bid + 1.3 * risk, bid + 3.0 * risk
        else:
            stop, tp1, target = bid + risk, bid - 1.3 * risk, bid - 3.0 * risk
        conf = module.ConfluenceEngine._aggregate([
            module.ConfluenceFactor("FIXTURE", "HTF_DIRECTION", 20.0, True,
                                    "fixture"),
            module.ConfluenceFactor("FIX2", "LTF_STRUCTURE", 25.0, True,
                                    "fixture"),
            module.ConfluenceFactor("FIX3", "LOCATION", 22.0, True,
                                    "fixture"),
            module.ConfluenceFactor("FIX4", "LIQUIDITY", 20.0, True,
                                    "fixture")])
        cand = module.SetupCandidate(
            family=variant.family, sid=variant.sid, version=variant.version,
            direction=direction, created=self.bot._now_utc(), entry_ref=bid,
            stop=stop, stop_reason="fixture invalidation", tp1=tp1,
            tp1_reason="1.3R", target=target, target_reason="fixture pool",
            tp1_r=1.3, target_r=3.0, blended_rr=2.2, confluence=conf,
            sequence=["PASS ALL"], invalidation="fixture",
            regime=self.bot._regime, session="LONDON",
            htf_bias="LONG/STRONG", location="DISCOUNT")
        return variant, cand

    def _place(self, direction=None):
        variant, cand = self._eligible_candidate(direction)
        self.bot._candidates = [(variant, cand)]
        now = self.bot._now_utc().replace(hour=10, minute=0)
        self.bot._consider_real(now, float(self.platform.Account.Equity),
                               30.0, self.bot._state, False, "clear")
        return variant, cand

    def test_order_is_placed_with_broker_side_protection(self):
        self._place()
        self.assertIsNotNone(self.bot._real, [p for p in self.platform.prints
                                             if "order-safety FAIL" in p])
        pos = self.platform.positions[-1]
        self.assertGreater(pos.StopLoss, 0.0)
        self.assertGreater(pos.TakeProfit, 0.0)
        self.assertTrue(any("REAL ORDER FILLED" in p
                            for p in self.platform.prints))

    def test_risk_stays_inside_the_quarter_percent_ceiling(self):
        self._place()
        r = self.bot._real
        self.assertLessEqual(r.risk_pct, self.bot.cfg.max_risk_per_trade
                             + 1e-9)
        self.assertGreater(r.risk_pct, 0.0)

    def test_probe_tier_is_used_for_a_new_strategy(self):
        self._place()
        self.assertLessEqual(self.bot._real.risk_pct,
                             self.bot.cfg.risk_tier_early + 1e-9)

    def test_short_order_places_the_stop_above_the_bid_level(self):
        module = self.module
        self._place(module.Direction.SHORT)
        self.assertIsNotNone(self.bot._real)
        r = self.bot._real
        pos = self.platform.positions[-1]
        # a short's broker stop must sit at least one spread above the bid
        # level the strategy chose
        self.assertGreater(pos.StopLoss, r.managed.stop)

    def test_only_one_real_position_at_a_time(self):
        self._place()
        self.assertEqual(len(self.platform.positions), 1)
        first_id = self.bot._real.position_id
        self._place()
        self.assertEqual(len(self.platform.positions), 1)
        self.assertEqual(self.bot._real.position_id, first_id)
        self.assertTrue(any("one real position at a time" in p
                            for p in self.platform.prints))

    def test_daily_real_trade_cap_blocks_further_entries(self):
        self._place()
        pos = self.platform.positions[-1]
        self.platform.settle(pos, -5.0)
        self.bot._reconcile_real(self.bot._now_utc())
        self.bot._real_trades_today = self.bot.cfg.max_real_trades_per_day
        self.bot.guard.day.trades_opened = self.bot.cfg.max_real_trades_per_day
        self._place()
        self.assertIsNone(self.bot._real)

    def test_settlement_records_the_trade_everywhere(self):
        variant, _ = self._place()
        pos = self.platform.positions[-1]
        before = self.bot.book.get(variant.sid).n
        self.platform.settle(pos, -12.0)
        self.bot._reconcile_real(self.bot._now_utc())
        self.assertIsNone(self.bot._real)
        self.assertEqual(self.bot.book.get(variant.sid).n, before + 1)
        self.assertEqual(self.bot.book.get(variant.sid).n_real, 1)
        self.assertLess(self.bot.guard.day.realised, 0.0)
        self.assertTrue(os.path.exists(os.path.join(self.dir,
                                                    "real_trades.csv")))
        self.assertTrue(any("REAL TRADE CLOSED" in p
                            for p in self.platform.prints))

    def test_partial_close_history_is_summed_not_lost(self):
        self._place()
        pos = self.platform.positions[-1]
        # a partial close leaves one history row, then the rest closes
        self.platform.history.append(self.module.__dict__ and
                                     FakeHistoricalTrade(
                                         pos.Id, pos.SymbolName, pos.Label,
                                         8.0, self.platform.symbol.Bid,
                                         self.bot._now_utc()))
        self.platform.settle(pos, -3.0)
        self.bot._reconcile_real(self.bot._now_utc())
        # 8.0 + (-3.0) = 5.0 net
        self.assertAlmostEqual(self.bot.guard.day.realised, 5.0, places=6)

    def test_emergency_stop_closes_the_position(self):
        self._place()
        with open(os.path.join(self.dir, "EMERGENCY_STOP.txt"), "w") as fh:
            fh.write("stop")
        view = self.module.ManagementView(
            now=self.bot._now_utc(), state=self.bot._state, spec=self.bot.spec,
            emergency=True)
        self.bot._manage_real(view)
        self.assertEqual(len(self.platform.positions), 0)
        self.assertTrue(any("EMERGENCY_STOP" in p
                            for p in self.platform.prints))

    def test_stop_is_only_ever_tightened_on_the_broker(self):
        self._place()
        r = self.bot._real
        pos = self.platform.positions[-1]
        original = pos.StopLoss
        act = self.module.ManagementAction(
            self.module.MOVE_STOP, "trailing test", r.managed.stop - 5.0)
        view = self.module.ManagementView(now=self.bot._now_utc(),
                                          state=self.bot._state,
                                          spec=self.bot.spec)
        self.bot._real_move_stop(r, pos, act, view)
        self.assertEqual(pos.StopLoss, original)   # a widening move is ignored


class TestRestartRecovery(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="v5restart")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_clock_population_learning_and_guard_all_survive(self):
        fx = BotFixture(self.dir)
        bot = fx.bot
        for _ in range(120):
            fx.platform.advance(1)
            bot.on_tick()
        variant = bot.population[0]
        for _ in range(7):
            bot.book.record(variant.sid, variant.family, 0.8, "STRONG_BULL",
                            "LONDON", "TAKE_PROFIT", 2.0, 0.4, real=False)
        bot.guard.register_close(-40.0, bot._now_utc())
        bot.shadow.equity[variant.sid] = 10_222.0
        start = bot.clock.start
        minutes = sum(bot.clock.minutes.values())
        bot._save_state(bot._now_utc())

        restarted = fx.restart(self.dir)
        self.assertFalse(restarted._fatal)
        self.assertEqual(restarted.clock.start, start)
        self.assertGreaterEqual(sum(restarted.clock.minutes.values()), minutes)
        self.assertEqual(len(restarted.population), len(bot.population))
        self.assertEqual(restarted.book.get(variant.sid).n, 7)
        self.assertAlmostEqual(restarted.shadow.equity[variant.sid], 10_222.0)
        self.assertLessEqual(restarted.guard.day.realised, -40.0)
        self.assertTrue(any("research clock restored" in p
                            for p in fx.platform.prints))

    def test_cooldown_survives_a_restart(self):
        fx = BotFixture(self.dir)
        bot = fx.bot
        for _ in range(10):
            fx.platform.advance(1)
            bot.on_tick()
        now = bot._now_utc()
        for _ in range(3):
            bot.guard.register_close(-15.0, now)
        self.assertIsNotNone(bot.guard.cooldown_until)
        bot._save_state(now)
        restarted = fx.restart(self.dir)
        self.assertIsNotNone(restarted.guard.cooldown_until)
        lock = restarted.guard.lock_reason(
            float(fx.platform.Account.Equity), 0.0, None,
            restarted._now_utc())
        self.assertEqual(lock.value, "CONSECUTIVE_LOSSES")

    def test_open_position_is_re_adopted_with_its_management_state(self):
        fx = BotFixture(self.dir)
        bot = fx.bot
        for _ in range(60):
            fx.platform.advance(1)
            bot.on_tick()
        module = fx.module
        variant = bot.population[0]
        bid = float(fx.platform.symbol.Bid)
        risk = max(1.2 * bot._state.atr_m5, 1.5)
        managed = module.ManagedTrade(
            trade_id="r-restart", sid=variant.sid, family=variant.family,
            direction=module.Direction.LONG, entry=bid,
            initial_stop=bid - risk, stop=bid - risk, tp1=bid + risk,
            target=bid + 3 * risk, units_initial=5.0, units=5.0,
            risk_dist=risk, risk_money=5.0 * risk, entry_time=bot._now_utc())
        managed.be_done = True
        managed.be_reason = "TP1 banked"
        pos = FakePosition(9999, "XAUUSD", "XAUUSD_Adaptive_Bot_V5", "Buy",
                           bid, 5.0, bid - risk, bid + 3 * risk,
                           bot._now_utc())
        fx.platform.positions.append(pos)
        bot._real = module.RealTradeRecord(
            trade_id="r-restart", position_id=9999, sid=variant.sid,
            family=variant.family, version=1,
            direction=module.Direction.LONG, managed=managed,
            entry_time=bot._now_utc(), risk_pct=0.001,
            risk_money_sized=5.0 * risk, confluence_score=70.0,
            confluence_detail="", stop_reason="fixture", tp1_reason="",
            target_reason="", sweep_kind="", regime="R", session="LONDON",
            htf_bias="", location="", spread_points=30.0)
        bot._save_state(bot._now_utc())

        restarted = fx.restart(self.dir)
        self.assertIsNotNone(restarted._real)
        self.assertEqual(restarted._real.position_id, 9999)
        self.assertTrue(restarted._real.managed.be_done)
        self.assertEqual(restarted._real.managed.be_reason, "TP1 banked")
        self.assertTrue(any("resumed tracking of real position" in p
                            for p in fx.platform.prints))

    def test_position_without_a_stop_is_closed_on_adoption(self):
        fx = BotFixture(self.dir)
        bot = fx.bot
        pos = FakePosition(8888, "XAUUSD", "XAUUSD_Adaptive_Bot_V5", "Buy",
                           3400.0, 5.0, 0.0, 0.0, bot._now_utc())
        fx.platform.positions.append(pos)
        restarted = fx.restart(self.dir)
        self.assertTrue(any("NO stop loss" in p for p in fx.platform.prints))
        self.assertNotIn(8888, [p.Id for p in fx.platform.positions])

    def test_broker_history_replay_feeds_the_daily_guard(self):
        fx = BotFixture(self.dir)
        bot = fx.bot
        now = bot._now_utc()
        fx.platform.history.append(FakeHistoricalTrade(
            7001, "XAUUSD", "XAUUSD_Adaptive_Bot_V5", -25.0, 3399.0, now))
        restarted = fx.restart(self.dir)
        self.assertLessEqual(restarted.guard.day.realised, -25.0)
        self.assertTrue(any("replayed" in p for p in fx.platform.prints))


class TestDailyPipeline(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="v5daily")
        self.fx = BotFixture(self.dir)
        self.bot = self.fx.bot

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_rankings_and_report_are_written(self):
        variant = self.bot.population[0]
        for _ in range(10):
            self.bot.book.record(variant.sid, variant.family, 0.7,
                                 "STRONG_BULL", "LONDON", "TAKE_PROFIT", 2.0,
                                 0.4, real=False)
        now = self.bot._now_utc()
        self.bot.guard.roll(now, 10000.0, 10000.0)
        self.bot._last_day_reported = ""
        self.bot._daily_pipeline(now, 10000.0)
        for name in ("strategy_rankings.csv", "family_rankings.csv",
                     "equity_history.csv"):
            self.assertTrue(os.path.exists(os.path.join(self.dir, name)),
                            name)
        reports = [f for f in os.listdir(self.dir)
                   if f.startswith("daily_report_")]
        self.assertTrue(reports)

    def test_final_report_is_written_when_research_completes(self):
        cfg = self.bot.cfg
        day = self.bot._now_utc()
        for offset in range(cfg.research_days * 2 + 10):
            when = day + timedelta(days=offset)
            if when.weekday() >= 5:
                continue
            for _ in range(cfg.active_day_min_minutes):
                self.bot.clock.observe(when, market_open=True)
        self.assertTrue(self.bot.clock.is_over())
        variant = self.bot.population[0]
        for _ in range(9):
            self.bot.book.record(variant.sid, variant.family, 0.6,
                                 "STRONG_BULL", "LONDON", "TAKE_PROFIT", 2.0,
                                 0.4, real=False)
        self.bot.guard.roll(day, 10000.0, 10000.0)
        self.bot._last_day_reported = ""
        self.bot._daily_pipeline(day, 10000.0)
        path = os.path.join(self.dir, "final_report.txt")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("PROVISIONAL WINNER", text)
        self.assertIn("NOT AUTOMATICALLY READY FOR LIVE TRADING", text)
        self.assertTrue(os.path.exists(os.path.join(self.dir,
                                                    "final_report.json")))

    def test_research_over_stops_new_setup_search(self):
        for offset in range(60):
            when = self.bot._now_utc() + timedelta(days=offset)
            if when.weekday() >= 5:
                continue
            for _ in range(self.bot.cfg.active_day_min_minutes):
                self.bot.clock.observe(when, market_open=True)
        ok, why = self.bot._research_gates(self.bot._now_utc(), 30.0, True)
        self.assertFalse(ok)
        self.assertIn("research period complete", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
