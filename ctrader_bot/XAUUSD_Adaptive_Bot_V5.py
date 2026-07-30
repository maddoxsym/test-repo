"""
============================================================================
XAUUSD_Adaptive_Bot_V5 — multi-timeframe CONFLUENCE research bot
                         (GOLD only, DEMO only)
============================================================================

V5 keeps V4's working machinery — broker integration, demo-only and gold-only
locks, restart-proof persistence, capital guards, CSV research trail, shadow
research and strategy ranking — and replaces the part that was wrong: V4
entered on standalone signals (a liquidity sweep plus a reclaim was enough).

V5 requires a MANDATORY multi-timeframe sequence before a setup exists at
all, then scores the remaining evidence with a correlation-aware confluence
engine and still demands a minimum score.  Six genuinely different setup
families are researched, not twenty-one parameter permutations, and the
ranking reports the FAMILY as well as the variant.

Absolute rails (stricter than V4, and there is no live switch anywhere):
  * DEMO-only ("LIVE ACCOUNT BLOCKED" + stop) and GOLD-only allowlist
  * one real position; broker-side SL+TP on every order; stops never widen
  * 0.25% max risk/trade (V4 allowed 0.75%), graduated slowly from 0.10%
  * 4 real trades/day maximum (V4 allowed 12)
  * 1.7% combined daily loss lock; 5% weekly drawdown lock
  * cooldown after 3 consecutive losses; volume always rounded DOWN
  * minimum volatility-based stop distance, so no unrealistically tight stops
  * no martingale, no grid, no averaging down, no recovery sizing
  * restarting never resets the guards or the research clock, and the clock
    counts ACTIVE TRADING DAYS, so weekends cost no research days

The modular source lives in adaptive_bot/ (shared detectors, reused
untouched) and adaptive_bot_v5/.  The deployable single file is
XAUUSD_Adaptive_Bot_V5_main.py (generated; class XAUUSD_Adaptive_Bot_V5).

This is a research/demo instrument.  No profitability is claimed and nothing
here is ready for live or funded trading.
"""

import clr

clr.AddReference("cAlgo.API")

from cAlgo.API import *
from robot_wrapper import *

import os
import random
import sys
from datetime import datetime, timedelta, timezone

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

from adaptive_bot.core.helpers import tf_bucket_start
from adaptive_bot.core.models import (Candle, CTraderSymbolSpec, Direction,
                                      LockReason, Regime, Timeframe, new_id)
from adaptive_bot.filters.news_filter import NewsFilter
from adaptive_bot.filters.session_filter import SessionManager
from adaptive_bot.filters.spread_filter import SpreadFilter
from adaptive_bot.risk.position_sizing import PositionSizer
from adaptive_bot.strategy.regime import MarketRegimeDetector
from adaptive_bot_v5.config_v5 import V5Config, V5ConfigValidator
from adaptive_bot_v5.execution_v5 import (RealTradeRecord, broker_level,
                                          shadow_row)
from adaptive_bot_v5.features_v5 import FeatureBuilder
from adaptive_bot_v5.learning_v5 import LearningBook
from adaptive_bot_v5.management import (CLOSE, MOVE_STOP, PARTIAL,
                                       ManagedTrade, ManagementView,
                                       TradeManager)
from adaptive_bot_v5.market_state_v5 import MarketStateBuilder
from adaptive_bot_v5.persistence_v5 import StateStore
from adaptive_bot_v5.reporting_v5 import (daily_report, family_row,
                                          final_report, variant_row)
from adaptive_bot_v5.research_clock import ResearchClock
from adaptive_bot_v5.risk_v5 import (OrderFacts, RiskEngine, V5Guard,
                                     order_preflight)
from adaptive_bot_v5.setups_v5 import (SetupLibrary, StrategyVariant,
                                       mutate_variant, seed_population)
from adaptive_bot_v5.shadow_v5 import ShadowEngine
from adaptive_bot_v5.trade_plan import risk_price

UTC = timezone.utc
BOT_LABEL = "XAUUSD_Adaptive_Bot_V5"

# M5 is the only DECISION timeframe. M15/M30/H1 supply context, M1 only times
# fills. No family can produce a trade idea from M1.
DECISION_TF = Timeframe.M5
CONTEXT_TFS = (Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H1)


class XAUUSD_Adaptive_Bot_V5(object):

    # ------------------------------------------------------------- lifecycle
    def on_start(self):
        self._fatal = False
        self._real = None
        self._last_m1_open = None
        self._last_m5_bucket = None
        self._features = {}
        self._state = None                  # MarketState
        self._regime = Regime.UNSAFE.value
        self._last_heartbeat = None
        self._last_state_save = None
        self._last_day_reported = ""
        self._lock_logged = None
        self._candidates = []               # (variant, SetupCandidate) this bar
        self._real_trades_today = 0
        self._real_trades_day = ""
        self._shadow_closed_today = 0
        self._mutation_serial = 0

        self.cfg = V5Config()
        validator = V5ConfigValidator(self.cfg)
        if not validator.validate():
            for line in validator.report().splitlines():
                api.Print(line)
            api.Print("configuration invalid — stopping")
            self._fatal = True
            api.Stop()
            return
        for line in validator.report().splitlines():
            if line:
                api.Print(line)

        # ---- DEMO-ONLY LOCK (no live switch exists anywhere) ---------------
        if bool(api.Account.IsLive):
            api.Print("LIVE ACCOUNT BLOCKED")
            api.Print("V5 is a demo-only research bot. Connect the Skilling "
                      "DEMO account and restart.")
            self._fatal = True
            api.Stop()
            return
        api.Print("account check: DEMO account confirmed")

        # ---- GOLD-ONLY LOCK ------------------------------------------------
        self._symbol_name = str(api.SymbolName)
        if not self._symbol_is_gold(self._symbol_name):
            api.Print(f"SYMBOL BLOCKED: '{self._symbol_name}' is not an "
                      f"approved gold symbol. EURUSD and every other non-gold "
                      f"symbol is refused.")
            self._fatal = True
            api.Stop()
            return
        api.Print(f"symbol check: {self._symbol_name} accepted as GOLD")

        self.spec = self._build_spec()
        ok, why = self.spec.valid()
        if not ok:
            api.Print(f"SYMBOL SPEC UNVERIFIABLE: {why} — stopping")
            self._fatal = True
            api.Stop()
            return
        mpu = self.spec.money_per_price_unit_per_unit()
        api.Print(f"symbol spec: tick {self.spec.tick_size} | 1 unit per 1.0 "
                  f"move = {mpu:.4f} {str(api.Account.Currency)} | volume "
                  f"min/step/max {self.spec.volume_min}/{self.spec.volume_step}"
                  f"/{self.spec.volume_max} units")
        if not (0.1 <= mpu <= 10.0):
            api.Print("per-unit value implausible for gold — refusing to "
                      "trade until verified")
            self._fatal = True
            api.Stop()
            return

        # ---- research stack -------------------------------------------------
        self.store = StateStore(self.cfg, lambda m: api.Print(m))
        self.sessions = SessionManager(self.cfg)
        self.news = NewsFilter(self.cfg, UTC)
        self.spread_filter = SpreadFilter(self.cfg)
        self.features_builder = FeatureBuilder(self.cfg)
        self.states = MarketStateBuilder(self.cfg)
        self.library = SetupLibrary(self.cfg)
        self.regime_detector = MarketRegimeDetector(self.cfg)
        self.sizer = PositionSizer(self.cfg)
        self.guard = V5Guard(self.cfg)
        self.risk = RiskEngine(self.cfg)
        self.manager = TradeManager(self.cfg)
        self.book = LearningBook(self.cfg, lambda m: api.Print(m))
        self.clock = ResearchClock(self.cfg)
        self.shadow = ShadowEngine(
            self.cfg, lambda m: self._debug(m),
            on_close=self._on_shadow_close,
            record_row=lambda t: self.store.csv_append("shadow_trades",
                                                       shadow_row(t)),
            record_suspect=self._on_shadow_suspect,
            record_management=self._on_shadow_management,
            on_watch_done=self._on_stop_watch)
        self.rng = random.Random(self.cfg.population_seed)

        # ---- news honesty ---------------------------------------------------
        for bad in self.news.malformed_entries():
            api.Print(f"NEWS CONFIG: ignoring malformed entry: {bad}")
        if self.cfg.news_enabled and not (self.cfg.fomc_events
                                          or self.cfg.cpi_events):
            api.Print("*** NEWS DATES MISSING *** only the NFP first-Friday "
                      "rule is active. Add FOMC/CPI dates to the config, or "
                      "set require_news_calendar = True to block entries "
                      "entirely.")
        api.Print("NEWS PROTECTION: schedule-based and manual only. There is "
                  "NO live news feed in this bot and none is claimed.")

        # ---- market data -----------------------------------------------------
        self._tf_map = {
            Timeframe.M1: TimeFrame.Minute,
            Timeframe.M5: TimeFrame.Minute5,
            Timeframe.M15: TimeFrame.Minute15,
            Timeframe.M30: TimeFrame.Minute30,
            Timeframe.H1: TimeFrame.Hour,
        }
        self._windows = {
            Timeframe.M1: self.cfg.window_m1,
            Timeframe.M5: self.cfg.window_m5,
            Timeframe.M15: self.cfg.window_m15,
            Timeframe.M30: self.cfg.window_m30,
            Timeframe.H1: self.cfg.window_h1,
        }
        self._bars = {tf: api.MarketData.GetBars(ctf)
                      for tf, ctf in self._tf_map.items()}

        now = self._now_utc()
        equity = float(api.Account.Equity)
        balance = float(api.Account.Balance)

        # ---- restart-proof state ---------------------------------------------
        state = self.store.load_state() or {}
        if state.get("clock"):
            self.clock.restore(state["clock"])
        if self.clock.begin(now):
            api.Print(f"RESEARCH CLOCK STARTED: {now:%Y-%m-%d %H:%M} UTC — "
                      f"{self.cfg.research_days} ACTIVE trading days "
                      f"(weekends and closed days do not count)")
        else:
            api.Print(f"research clock restored: {self.clock.describe(now)}")

        pop = state.get("population")
        if pop:
            restored = [StrategyVariant.from_dict(d) for d in pop]
            self.population = [v for v in restored if v is not None]
            api.Print(f"restored {len(self.population)} strategy variants "
                      f"from persisted state")
        else:
            self.population = seed_population(self.cfg,
                                              now.date().isoformat())
            api.Print(f"seeded {len(self.population)} variants across "
                      f"{len(set(v.family for v in self.population))} "
                      f"confluence setup families")
        if state.get("learning"):
            self.book.restore(state["learning"])
            api.Print(f"restored learning statistics: "
                      f"{self.book.total_trades} completed trades across "
                      f"{len(self.book.variants)} variants")
        if state.get("shadow"):
            self.shadow.restore(state["shadow"])
        self._mutation_serial = int(state.get("counters", {})
                                    .get("mutation_serial", 0))
        self._last_day_reported = str(state.get("last_day_reported", ""))
        self._real_trades_today = int(state.get("real_trades_today", 0))
        self._real_trades_day = str(state.get("real_trades_day", ""))
        if self._real_trades_day != now.date().isoformat():
            self._real_trades_today = 0
            self._real_trades_day = now.date().isoformat()

        # guards: replay broker history first, then merge persisted state.
        # Limits can only become MORE restrictive, never less.
        self.guard.roll(now, equity, balance)
        self._replay_today_history(now)
        if state.get("guard"):
            self.guard.restore(state["guard"], now)
        api.Print(f"daily guard: {self.guard.describe(equity)}")
        if self.guard.cooldown_until:
            api.Print(f"post-loss cooldown active until "
                      f"{self.guard.cooldown_until:%Y-%m-%d %H:%M} UTC "
                      f"(restarting does not clear it)")

        m5 = self._completed_candles(Timeframe.M5)
        self.sessions.rebuild_from(m5)

        self._adopt_open_position(state.get("open_real"), now)

        m1 = self._completed_candles(Timeframe.M1)
        if m1:
            self._last_m1_open = m1[-1].time
            self._last_m5_bucket = tf_bucket_start(m1[-1].time, Timeframe.M5)

        api.Print(f"{BOT_LABEL} started | DEMO | {self.clock.describe(now)} | "
                  f"{len([v for v in self.population if v.status == 'active'])}"
                  f" active variants | risk {self.cfg.risk_tier_probe:.2%}.."
                  f"{self.cfg.max_risk_per_trade:.2%}/trade, "
                  f"{self.cfg.max_real_trades_per_day} real trades/day max, "
                  f"daily {self.cfg.max_daily_loss:.1%} combined, weekly "
                  f"{self.cfg.max_weekly_drawdown:.0%} | min confluence "
                  f"{self.cfg.min_confluence:.0f}/100 | state: "
                  f"{self.store.directory or 'NOT PERSISTED — FIX THIS'}")
        api.Print("no trading at startup — the first decisions come on the "
                  "next completed M5 candle")
        self._save_state(now)

    def on_tick(self):
        if self._fatal:
            return
        try:
            self._tick()
        except Exception as exc:
            api.Print(f"ERROR in tick processing: {exc!r}")

    def on_stop(self):
        if self._fatal:
            return
        now = self._now_utc()
        self._save_state(now)
        api.Print(f"{BOT_LABEL} stopping. "
                  f"{self.guard.describe(float(api.Account.Equity))}")
        if self._real is not None:
            api.Print(f"NOTE: real position {self._real.position_id} stays "
                      f"protected by its broker-side stop loss and take "
                      f"profit.")
        if self.store.directory:
            api.Print(f"research files: {self.store.directory}")

    # ------------------------------------------------------------------ tick
    def _tick(self):
        now = self._now_utc()
        spread_points = self._spread_points()
        equity = float(api.Account.Equity)
        balance = float(api.Account.Balance)
        market_open = self._market_open()

        m1 = self._completed_candles(Timeframe.M1)
        if not m1:
            return
        last_m1 = m1[-1]
        new_m1 = (self._last_m1_open is None
                  or last_m1.time > self._last_m1_open)

        if new_m1:
            self._last_m1_open = last_m1.time
            self.sessions.update_ranges(last_m1)
            self.clock.observe(now, market_open)
            new_day, _ = self.guard.roll(now, equity, balance)
            if self._real_trades_day != now.date().isoformat():
                self._real_trades_day = now.date().isoformat()
                self._real_trades_today = 0
            self._daily_pipeline(now, equity)
            self.shadow.expire_pending(now)
            self._reconcile_real(now)
            self._update_real_excursions(last_m1, spread_points)
            self.shadow.on_m1(last_m1, spread_points, self.spec.point,
                              self.spec, now)

        bucket = tf_bucket_start(last_m1.time, Timeframe.M5)
        new_m5 = new_m1 and (self._last_m5_bucket is None
                             or bucket > self._last_m5_bucket)
        if new_m5:
            self._last_m5_bucket = bucket
            self._on_m5_close(now, spread_points, equity, market_open)

        self._heartbeat(now, spread_points, equity)
        if self._last_state_save is None \
                or (now - self._last_state_save) >= timedelta(minutes=5):
            self._save_state(now)

    # ------------------------------------------------------------ M5 decision
    def _on_m5_close(self, now, spread_points, equity, market_open):
        cfg = self.cfg
        # ---- rebuild the multi-timeframe picture ---------------------------
        feats = {}
        m5c = self._completed_candles(Timeframe.M5)
        marks = self.sessions.marks_for(now.date())
        for tf in CONTEXT_TFS:
            candles = self._completed_candles(tf)
            if len(candles) < cfg.min_candles_required:
                continue
            built = self.features_builder.build(
                tf, candles, marks if tf in (Timeframe.M5, Timeframe.M15)
                else None)
            if built is not None:
                feats[tf] = built
        self._features = feats
        m5 = feats.get(Timeframe.M5)
        if m5 is None:
            self._debug("M5 features not ready — waiting for more history")
            return

        news_blocked, news_reason = self.news.blackout(now)
        reading = self.regime_detector.classify(m5.candles, m5.structure,
                                               spread_points, news_blocked)
        self._regime = reading.regime.value

        prev_day = self._previous_day_extremes(m5c)
        state = self.states.build(
            now=now, bid=float(api.Symbol.Bid), ask=float(api.Symbol.Ask),
            spread_points=spread_points, point=self.spec.point,
            regime=self._regime, session=self.sessions.session_at(now),
            feats=feats, session_marks=marks, m5_candles=m5c,
            prev_day=prev_day)
        self._state = state

        view = ManagementView(
            now=now, state=state, spec=self.spec,
            weekend_flat=(self.sessions.near_weekend_flat(now)
                          and not cfg.weekend_hold_allowed),
            emergency=self._emergency_present(),
            research_over=self.clock.is_over())

        # ---- manage what is already open -----------------------------------
        self._manage_real(view)
        self.shadow.on_m5_close(view, {v.sid: v.params
                                       for v in self.population})

        # ---- look for new setups -------------------------------------------
        self._candidates = []
        gates_ok, gates_why = self._research_gates(now, spread_points,
                                                   market_open)
        if not gates_ok:
            self._debug(f"no setup search this bar: {gates_why}")
            return
        if not state.ready():
            self._debug("multi-timeframe context incomplete — no setups")
            return

        mpu = self.spec.money_per_price_unit_per_unit()
        for variant in self.population:
            if variant.status != "active":
                continue
            for direction in (Direction.LONG, Direction.SHORT):
                cand, log = self.library.evaluate(variant, state, direction,
                                                  mpu, self.spec.point)
                if cand is None:
                    if cfg.debug_logging and log.failure:
                        self._debug(f"{variant.sid} {direction.value}: "
                                    f"{log.failure}")
                    continue
                api.Print(f"SETUP [{variant.sid} v{variant.version}] "
                          f"{direction.value} confluence "
                          f"{cand.confluence.score:.0f}/100 | entry ref "
                          f"{cand.entry_ref:.2f} SL {cand.stop:.2f} "
                          f"({cand.stop_reason}) TP1 {cand.tp1:.2f} "
                          f"target {cand.target:.2f} ({cand.target_reason}) "
                          f"{cand.target_r:.2f}R net")
                for line in cand.confluence.explain():
                    self._debug("  " + line)
                if self.shadow.submit(cand, spread_points, state.atr_m5):
                    self._candidates.append((variant, cand))
                else:
                    self._debug(f"{variant.sid}: already has an open or "
                                f"pending virtual trade")

        self._consider_real(now, equity, spread_points, state, news_blocked,
                            news_reason)

    def _research_gates(self, now, spread_points, market_open):
        """Conditions under which setups are even searched for (shadow
        research included — an unsafe market teaches nothing useful)."""
        if now.weekday() >= 5:
            return False, "weekend"
        if not market_open:
            return False, "market closed"
        blocked, why = self.news.blackout(now)
        if blocked:
            return False, f"news blackout: {why}"
        ok, why = self.spread_filter.check(spread_points)
        if not ok:
            return False, why
        if self.clock.is_over():
            return False, ("research period complete — managing the open "
                           "position only")
        return True, ""

    # ------------------------------------------------------------ real entry
    def _consider_real(self, now, equity, spread_points, state, news_blocked,
                       news_reason):
        cfg = self.cfg
        if not self._candidates:
            return
        if self._real is not None:
            self._debug("one real position at a time — not considering a new "
                        "entry while one is open")
            return
        floating = self._floating_pnl()
        session_obj = self.sessions.session_at(now)
        lock = self.guard.lock_reason(equity, floating, session_obj, now)
        if lock != LockReason.NONE:
            if lock.value != self._lock_logged:
                api.Print(f"no real entries — lock: {lock.value} | "
                          f"{self.guard.describe(equity)}")
                self._lock_logged = lock.value
            return
        self._lock_logged = None
        allowed, window_why = self.sessions.entry_window_check(now)
        if not allowed:
            self._debug(f"real entry window closed: {window_why}")
            return
        if cfg.require_news_calendar and not (cfg.fomc_events
                                              or cfg.cpi_events):
            news_blocked, news_reason = True, (
                "require_news_calendar = True but no dates are configured")
        if news_blocked:
            self._reject("news", None, now, spread_points, news_reason)
            return

        chosen, notes = self.book.select_real(self._candidates, self._regime)
        for note in notes:
            self._debug(f"selection: {note}")
        if chosen is None:
            self._debug("no variant is eligible for a real trade yet "
                        "(insufficient evidence or non-positive expectancy)")
            return
        variant, cand = chosen

        tier_fraction, tier_reason = self.book.risk_tier(variant)
        stats = self.book.get(variant.sid, variant.family)
        day = self.guard.day
        decision = self.risk.choose(
            tier_fraction, tier_reason, equity=equity,
            day_start_equity=day.start_equity if day else equity,
            daily_pl_combined=(day.realised if day else 0.0)
            + min(0.0, floating),
            weekly_dd_frac=self.guard.weekly_dd_pct(equity),
            consecutive_losses=self.guard.consecutive_losses,
            spread_points=spread_points,
            atr_percentile=state.m5.atr_percentile if state.m5 else 0.5,
            recent_strategy_r=sum(stats.recent[-3:]),
            confluence_score=cand.confluence.score,
            confluence_threshold=self.library.threshold_for(variant))
        for note in decision.notes:
            self._debug(f"risk: {note}")
        if decision.blocked:
            self._reject("risk", cand, now, spread_points,
                         "; ".join(decision.notes[-2:]))
            return

        # ---- size against the live spec -------------------------------------
        entry = float(api.Symbol.Ask) if cand.direction == Direction.LONG \
            else float(api.Symbol.Bid)
        self.spec.spread_points = spread_points
        sizing = self.sizer.size(self.spec, equity, decision.fraction, entry,
                                 cand.stop)
        stop_distance = risk_price(cand.direction, entry, cand.stop,
                                   state.spread_price)
        facts = OrderFacts(
            is_demo_account=not bool(api.Account.IsLive),
            symbol_is_gold=self._symbol_is_gold(self._symbol_name),
            market_open=self._market_open(), spread_points=spread_points,
            spread_ok=self.spread_filter.check(spread_points)[0],
            positions_on_symbol=self._symbol_positions_count(),
            has_pending_bot_order=self._has_pending_order(),
            news_blocked=news_blocked, news_reason=news_reason, lock=lock,
            equity=equity, session_allowed=allowed,
            session_reason=window_why, research_over=self.clock.is_over(),
            cooldown_active=self.guard.cooldown_active(now),
            real_trades_today=self._real_trades_today,
            emergency=self._emergency_present())
        pf = order_preflight(cfg, cand, entry, sizing.volume_units,
                             sizing.risk_money, sizing.rejected,
                             sizing.reason, stop_distance, state.atr_m5,
                             self.spec.point,
                             self.library.threshold_for(variant), facts)
        if cfg.debug_logging or not pf.ok:
            for line in pf.checks:
                api.Print(f"  order-safety {line}")
        if not pf.ok:
            self._reject("preflight", cand, now, spread_points, pf.reason)
            return

        # ---- place the order with broker-side SL and TP ---------------------
        trade_type = TradeType.Buy if cand.direction == Direction.LONG \
            else TradeType.Sell
        sl_broker = broker_level(cand.stop, cand.direction, state.spread_price)
        tp_broker = broker_level(cand.target, cand.direction,
                                 state.spread_price)
        sl_pips = abs(entry - sl_broker) / self.spec.pip_size
        tp_pips = abs(tp_broker - entry) / self.spec.pip_size
        result = api.ExecuteMarketOrder(trade_type, self._symbol_name,
                                        sizing.volume_units, BOT_LABEL,
                                        sl_pips, tp_pips)
        if not bool(result.IsSuccessful) or result.Position is None:
            err = str(result.Error) if result.Error is not None else "unknown"
            api.Print(f"ORDER FAILED: {err}")
            self._reject("broker", cand, now, spread_points,
                         f"order rejected: {err}")
            return
        pos = result.Position
        fill = float(pos.EntryPrice)
        sl_price = round(sl_broker, self.spec.digits)
        tp_price = round(tp_broker, self.spec.digits)
        try:
            api.ModifyPosition(pos, sl_price, tp_price)
        except Exception as exc:
            api.Print(f"note: SL/TP refinement failed ({exc!r}); the "
                      f"pip-based protection submitted with the order "
                      f"remains in force")

        risk_dist = risk_price(cand.direction, fill, cand.stop,
                               state.spread_price)
        if risk_dist <= 0:
            api.Print("WARNING: non-positive risk distance after the fill — "
                      "closing for safety")
            try:
                api.ClosePosition(pos)
            except Exception as exc:
                api.Print(f"protective close failed: {exc!r}")
            return
        mpu = self.spec.money_per_price_unit_per_unit()
        managed = ManagedTrade(
            trade_id=new_id("real"), sid=variant.sid, family=variant.family,
            direction=cand.direction, entry=fill, initial_stop=cand.stop,
            stop=cand.stop, tp1=cand.tp1, target=cand.target,
            units_initial=sizing.volume_units, units=sizing.volume_units,
            risk_dist=risk_dist,
            risk_money=sizing.volume_units * risk_dist * mpu,
            entry_time=now)
        for note in cand.notes:
            if note.startswith("RANGE="):
                try:
                    lo, hi = note[6:].split(":")
                    managed.meta["range_low"] = float(lo)
                    managed.meta["range_high"] = float(hi)
                except ValueError:
                    pass
        self._real = RealTradeRecord(
            trade_id=managed.trade_id, position_id=int(pos.Id),
            sid=variant.sid, family=variant.family, version=variant.version,
            direction=cand.direction, managed=managed, entry_time=now,
            risk_pct=sizing.risk_fraction_actual,
            risk_money_sized=sizing.risk_money,
            confluence_score=cand.confluence.score,
            confluence_detail=cand.confluence.as_csv_field(),
            stop_reason=cand.stop_reason, tp1_reason=cand.tp1_reason,
            target_reason=cand.target_reason, sweep_kind=cand.sweep_kind,
            regime=cand.regime, session=session_obj.value,
            htf_bias=cand.htf_bias, location=cand.location,
            spread_points=spread_points, params=dict(variant.params))
        self.guard.register_open(session_obj)
        self._real_trades_today += 1
        api.Print(f"REAL ORDER FILLED [{variant.sid} v{variant.version} "
                  f"{variant.family}]: {cand.direction.value} "
                  f"{sizing.volume_units:g} units @ {fill:.2f} | SL "
                  f"{sl_price:.2f} TP {tp_price:.2f} | risk "
                  f"{sizing.risk_money:.2f} "
                  f"({sizing.risk_fraction_actual:.3%}) | confluence "
                  f"{cand.confluence.score:.0f} | trade "
                  f"{self._real_trades_today}/{cfg.max_real_trades_per_day} "
                  f"today | {tier_reason}")
        api.Print(f"  plan: TP1 {cand.tp1:.2f} ({cand.tp1_reason}) then "
                  f"runner to {cand.target:.2f} ({cand.target_reason}); "
                  f"invalidation: {cand.invalidation}")
        self._save_state(now)

    # ----------------------------------------------------------- real upkeep
    def _manage_real(self, view):
        r = self._real
        if r is None:
            return
        pos = self._find_position(r.position_id)
        if pos is None:
            return
        actions = self.manager.step(r.managed, view, r.params)
        for act in actions:
            if act.kind == PARTIAL:
                self._real_partial(r, pos, act, view)
            elif act.kind == MOVE_STOP:
                self._real_move_stop(r, pos, act, view)
            elif act.kind == CLOSE:
                self._real_close(r, pos, act)
                return

    def _real_partial(self, r, pos, act, view):
        volume = self.spec.round_volume_down(act.units)
        if volume <= 0 or volume >= r.managed.units:
            self._debug(f"partial skipped after rounding: {act.units:g} -> "
                        f"{volume:g} units")
            return
        try:
            res = api.ClosePosition(pos, volume)
        except Exception as exc:
            api.Print(f"partial close failed: {exc!r}")
            return
        if not bool(res.IsSuccessful):
            api.Print(f"partial close rejected: "
                      f"{str(res.Error) if res.Error is not None else '?'}")
            return
        m = r.managed
        m.units -= volume
        m.partial_done = True
        m.partial_units += volume
        m.partial_price = act.price
        m.partial_reason = act.reason
        api.Print(f"REAL PARTIAL [{r.sid}]: closed {volume:g} units @ "
                  f"~{act.price:.2f} — {act.reason}")
        self._management_row("real", r.trade_id, r.sid, r.family, act, m,
                             view.now)

    def _real_move_stop(self, r, pos, act, view):
        m = r.managed
        new_bid_level = m.tighten(act.price)
        if new_bid_level is None:
            return
        broker = broker_level(new_bid_level, r.direction,
                              view.state.spread_price)
        price = round(broker, self.spec.digits)
        try:
            api.ModifyPosition(pos, price, pos.TakeProfit)
        except Exception as exc:
            api.Print(f"stop move failed: {exc!r}")
            return
        m.stop = new_bid_level
        if "trailing" in act.reason:
            m.trail_active = True
            m.trail_updates.append(f"{view.now.isoformat()} -> "
                                   f"{new_bid_level:.2f}: {act.reason}")
        elif "breakeven" in act.reason:
            m.be_done = True
            m.be_reason = act.reason
        api.Print(f"REAL STOP TIGHTENED [{r.sid}]: {price:.2f} "
                  f"(stage {m.stop_stage()}) — {act.reason}")
        self._management_row("real", r.trade_id, r.sid, r.family, act, m,
                             view.now)

    def _real_close(self, r, pos, act):
        try:
            api.ClosePosition(pos)
        except Exception as exc:
            api.Print(f"close failed: {exc!r}")
            return
        r.managed.early_exit_reason = act.reason
        r.exit_label = act.label
        api.Print(f"REAL CLOSE [{r.sid}] {act.label}: {act.reason}")

    def _reconcile_real(self, now):
        """Settle a real position the broker has closed (SL, TP, or our own
        close).  Every history entry for the position is summed, so a partial
        close is not lost."""
        r = self._real
        if r is None:
            return
        if self._find_position(r.position_id) is not None:
            return
        gross, commission, swap, net, exit_price, exit_time = \
            self._history_totals(r.position_id, now)
        m = r.managed
        r.gross_money = gross
        r.commission_money = abs(commission) + abs(swap)
        r.net_money = net
        r.exit_price = exit_price
        r.exit_time = exit_time
        if m.risk_money > 0:
            r.gross_r = gross / m.risk_money
            r.net_r = net / m.risk_money
        if not r.exit_label:
            r.exit_label = self._infer_exit_label(r, exit_price)
        self.guard.register_close(net, now)
        self.book.record(
            r.sid, r.family, r.net_r, r.regime, r.session, r.exit_label,
            m.mfe_r, m.mae_r, real=True, partial=m.partial_done,
            be=m.be_done, trail=m.trail_active,
            early=bool(m.early_exit_reason))
        self.store.csv_append("real_trades", r.to_row(net))
        api.Print(f"REAL TRADE CLOSED [{r.sid} {r.family}] {r.exit_label}: "
                  f"{net:+.2f} ({r.net_r:+.2f}R) | MFE {m.mfe_r:.2f}R MAE "
                  f"{m.mae_r:.2f}R | "
                  f"{self.guard.describe(float(api.Account.Equity))}")
        self._real = None
        self._save_state(now)

    def _history_totals(self, position_id, now):
        gross = commission = swap = net = 0.0
        exit_price = 0.0
        exit_time = now
        found = False
        for i in range(int(api.History.Count) - 1, -1, -1):
            h = api.History[i]
            try:
                if int(h.PositionId) != position_id:
                    continue
            except Exception:
                continue
            found = True
            net += float(h.NetProfit)
            try:
                gross += float(h.GrossProfit)
            except Exception:
                gross += float(h.NetProfit)
            try:
                commission += float(h.Commission)
            except Exception:
                pass
            try:
                swap += float(h.Swap)
            except Exception:
                pass
            try:
                exit_price = float(h.ClosingPrice)
                exit_time = self._to_utc(h.ClosingTime)
            except Exception:
                pass
        if not found:
            api.Print(f"WARNING: no history found for position "
                      f"{position_id}; settled as flat")
        return gross, commission, swap, net, exit_price, exit_time

    def _infer_exit_label(self, r, exit_price):
        m = r.managed
        if exit_price <= 0:
            return "BROKER_CLOSED"
        tol = 5 * self.spec.tick_size + r.spread_points * self.spec.point
        if abs(exit_price - broker_level(m.target, r.direction,
                                         r.spread_points * self.spec.point)) \
                <= tol:
            return "TAKE_PROFIT"
        if abs(exit_price - broker_level(m.stop, r.direction,
                                         r.spread_points * self.spec.point)) \
                <= tol:
            return m.exit_label_for_stop()
        return "MANAGED_EXIT"

    def _update_real_excursions(self, candle, spread_points):
        r = self._real
        if r is None:
            return
        m = r.managed
        if m.risk_dist <= 0:
            return
        spread_price = spread_points * self.spec.point
        if r.direction == Direction.LONG:
            best, worst = candle.high, candle.low
        else:
            best = candle.low + spread_price
            worst = candle.high + spread_price
        d = m.d
        m.mfe_r = max(m.mfe_r, max(0.0, (best - m.entry) * d / m.risk_dist))
        m.mae_r = max(m.mae_r, max(0.0, (m.entry - worst) * d / m.risk_dist))
        m.bars_open += 1

    # -------------------------------------------------------------- research
    def _daily_pipeline(self, now, equity):
        today = now.date().isoformat()
        if self._last_day_reported == today:
            return
        previous = self._last_day_reported
        self._last_day_reported = today
        api.Print(f"=== new trading day {today} — {self.clock.describe(now)} "
                  f"===")

        decisions = self.book.daily_update(
            self.population, today, self.rng, self._spawn)
        for d in decisions:
            api.Print(f"LEARNING [{d.kind}] {d.sid}: {d.why}")
            self.store.csv_append("learning_log", {
                "date": today, "kind": d.kind, "sid": d.sid,
                "param": d.param, "old": f"{d.old:g}" if d.param else "",
                "new": f"{d.new:g}" if d.param else "", "why": d.why})
            if d.kind == "ADAPT":
                variant = next((v for v in self.population
                                if v.sid == d.sid), None)
                self.store.csv_append("parameter_updates", {
                    "date": today, "sid": d.sid,
                    "family": variant.family if variant else "",
                    "version": variant.version if variant else "",
                    "param": d.param, "old": f"{d.old:g}",
                    "new": f"{d.new:g}", "evidence": d.why})

        self._write_rankings(today)
        account = [
            f"equity {equity:.2f} | {self.guard.describe(equity)}",
            f"weekly drawdown {self.guard.weekly_dd_pct(equity):.2%} "
            f"(limit {self.cfg.max_weekly_drawdown:.0%})",
            f"real trades today {self._real_trades_today}/"
            f"{self.cfg.max_real_trades_per_day}",
        ]
        report = daily_report(now, self.clock, self.book, self.population,
                              account, self.cfg, decisions)
        self.store.write_text(f"daily_report_{today}.txt", report)
        for line in report.splitlines():
            api.Print(line)

        if previous:
            self.store.csv_append("daily_summary", {
                "date": previous,
                "research_day": self.clock.day_index(now),
                "active_minutes": f"{self.clock.minutes.get(previous, 0.0):.0f}",
                "start_equity": f"{self.guard.day.start_equity:.2f}"
                if self.guard.day else "",
                "end_equity": f"{equity:.2f}",
                "realised_pl": f"{self.guard.day.realised:+.2f}"
                if self.guard.day else "",
                "realised_pct": f"{(self.guard.day.realised / self.guard.day.start_equity if (self.guard.day and self.guard.day.start_equity) else 0):+.4%}",
                "real_trades": self._real_trades_today,
                "shadow_trades_closed": self._shadow_closed_today,
                "shadow_setups_discarded": sum(
                    self.shadow.discarded_counts.values()),
                "wins_real": self.guard.day.wins if self.guard.day else 0,
                "losses_real": self.guard.day.losses if self.guard.day else 0,
                "daily_combined_pct":
                    f"{self.guard.daily_combined_pct(self._floating_pnl()):+.4%}",
                "weekly_dd_pct": f"{self.guard.weekly_dd_pct(equity):.4%}",
                "active_variants": sum(1 for v in self.population
                                       if v.status == "active"),
                "benched": sum(1 for v in self.population
                               if v.status == "benched"),
                "retired": sum(1 for v in self.population
                               if v.status == "retired"),
                "best_variant": self._best_variant_name(),
                "best_variant_score": f"{self._best_variant_score():+.4f}",
                "best_family": self._best_family_name(),
                "best_family_score": f"{self._best_family_score():+.4f}",
                "regime_mix": self._regime,
                "lock_events": " | ".join(self.guard.lock_events[-3:])})
        self._shadow_closed_today = 0

        self.store.csv_append("equity_history", {
            "time": now.isoformat(), "equity": f"{equity:.2f}",
            "balance": f"{float(api.Account.Balance):.2f}",
            "floating": f"{self._floating_pnl():+.2f}",
            "daily_combined_pct":
                f"{self.guard.daily_combined_pct(self._floating_pnl()):+.4%}",
            "weekly_dd_pct": f"{self.guard.weekly_dd_pct(equity):.4%}",
            "open_real": 1 if self._real is not None else 0,
            "open_shadow": len(self.shadow.open)})

        if self.clock.is_over() and not self.clock.final_report_done:
            self._write_final_report(now, equity)
        self._save_state(now)

    def _spawn(self, parent):
        if len(self.population) >= self.cfg.max_population:
            return None
        self._mutation_serial += 1
        return mutate_variant(parent, self._mutation_serial, self.rng,
                              self._now_utc().date().isoformat())

    def _write_rankings(self, today):
        rows = []
        for i, (variant, score) in enumerate(
                self.book.ranking(self.population), 1):
            rows.append(variant_row(today, i, variant,
                                    self.book.get(variant.sid, variant.family),
                                    score, self.cfg))
        self.store.csv_rewrite("strategy_rankings", rows)
        fam_rows = []
        for i, (name, score, stats) in enumerate(self.book.family_ranking(), 1):
            fam_rows.append(family_row(today, i, name, score, stats, self.cfg))
        self.store.csv_rewrite("family_rankings", fam_rows)

    def _write_final_report(self, now, equity):
        notes = [
            f"research ran from "
            f"{self.clock.start:%Y-%m-%d} to {now:%Y-%m-%d} and required "
            f"{self.cfg.research_days} ACTIVE trading days "
            f"({self.clock.completed_days()} completed; weekends and closed "
            f"days were not counted)",
            f"minimum confluence threshold {self.cfg.min_confluence:.0f}/100; "
            f"per-family thresholds "
            + ", ".join(f"{k} {v}" for k, v
                        in sorted(self.cfg.family_min_confluence.items())),
            f"risk graduated from {self.cfg.risk_tier_probe:.2%} to a hard "
            f"ceiling of {self.cfg.max_risk_per_trade:.2%} per trade, "
            f"{self.cfg.max_real_trades_per_day} real trades per day maximum",
            f"{sum(self.shadow.discarded_counts.values())} setups were "
            f"discarded before filling (stale signal, gapped entry, or the "
            f"reward:risk no longer held at the fill)",
        ]
        account = [f"final equity {equity:.2f} | "
                   f"{self.guard.describe(equity)}"]
        text, payload = final_report(now, self.clock, self.book,
                                     self.population, self.cfg, account,
                                     notes)
        self.store.write_text("final_report.txt", text)
        self.store.write_json("final_report.json", payload)
        self.clock.final_report_done = True
        for line in text.splitlines():
            api.Print(line)
        api.Print("RESEARCH PERIOD COMPLETE — no new entries. Any open "
                  "position is still managed to its exit.")

    # ------------------------------------------------------------- callbacks
    def _on_shadow_close(self, t):
        m = t.managed
        self.book.record(
            t.sid, t.family, t.net_r, t.regime, t.session, t.exit_label,
            t.mfe_r, t.mae_r, real=False,
            partial=bool(m and m.partial_done),
            be=bool(m and m.be_done), trail=bool(m and m.trail_active),
            early=bool(m and m.early_exit_reason))
        self._shadow_closed_today += 1
        self._debug(f"shadow closed [{t.sid}] {t.exit_label} "
                    f"{t.net_r:+.2f}R (MFE {t.mfe_r:.2f} MAE {t.mae_r:.2f})")

    def _on_shadow_suspect(self, t):
        self.book.record_suspect(t.sid, t.family)
        self.store.csv_append("suspect_trades", {
            "time": (t.exit_time or self._now_utc()).isoformat(),
            "trade_id": t.trade_id, "sid": t.sid, "family": t.family,
            "exit_label": t.exit_label, "gross_r": f"{t.gross_r:+.4f}",
            "net_r": f"{t.net_r:+.4f}", "mfe_r": f"{t.mfe_r:.4f}",
            "mae_r": f"{t.mae_r:.4f}", "suspect_reason": t.suspect_reason})

    def _on_shadow_management(self, t, act):
        self._management_row("shadow", t.trade_id, t.sid, t.family, act,
                             t.managed, self._now_utc())

    def _on_stop_watch(self, t):
        self.book.record_stop_watch(t.sid, t.family, t.watch_target_hit)
        self.store.csv_append("stop_watch", {
            "time": (t.exit_time or self._now_utc()).isoformat(),
            "trade_id": t.trade_id, "sid": t.sid, "family": t.family,
            "net_r": f"{t.net_r:+.3f}",
            "target_hit_after_stop": "yes" if t.watch_target_hit else "no",
            "bars_watched": self.cfg.shadow_post_watch_bars
            - max(0, t.watch_bars_left)})

    def _management_row(self, book, trade_id, sid, family, act, managed, now):
        self.store.csv_append("management_events", {
            "time": now.isoformat(), "book": book, "trade_id": trade_id,
            "sid": sid, "family": family, "action": act.kind,
            "price": f"{act.price:.2f}", "units": f"{act.units:g}",
            "stop_stage": managed.stop_stage() if managed else "",
            "reason": act.reason})

    def _reject(self, stage, cand, now, spread_points, reason):
        api.Print(f"SETUP REJECTED [{stage}]"
                  + (f" {cand.sid} {cand.direction.value}" if cand else "")
                  + f": {reason}")
        self.store.csv_append("rejected_setups", {
            "time": now.isoformat(), "stage": stage,
            "sid": cand.sid if cand else "-",
            "family": cand.family if cand else "-",
            "direction": cand.direction.value if cand else "-",
            "regime": self._regime,
            "session": self.sessions.session_at(now).value,
            "htf_bias": cand.htf_bias if cand else "",
            "spread_points": f"{spread_points:.0f}",
            "confluence_score": f"{cand.confluence.score:.0f}" if cand else "",
            "reason": reason,
            "sequence": " | ".join(cand.sequence[-4:]) if cand else ""})

    # -------------------------------------------------------------- heartbeat
    def _heartbeat(self, now, spread_points, equity):
        if self._last_heartbeat is not None \
                and (now - self._last_heartbeat) < timedelta(
                    minutes=self.cfg.heartbeat_minutes):
            return
        self._last_heartbeat = now
        news_blocked, news_reason = self.news.blackout(now)
        lock = self.guard.lock_reason(equity, self._floating_pnl(),
                                      self.sessions.session_at(now), now)
        bias = "n/a"
        if self._state is not None:
            bias = (f"{self._state.bias.direction.value}/"
                    f"{self._state.bias.strength}"
                    if self._state.bias.direction else "NONE")
        if self._real is not None:
            m = self._real.managed
            real_status = (f"{self._real.direction.value} {m.units:g}u @ "
                           f"{m.entry:.2f} SL {m.stop:.2f}")
            stage = m.stop_stage()
        else:
            real_status = "flat"
            stage = "-"
        top = []
        for variant, score in self.book.ranking(
                [v for v in self.population if v.status == "active"])[:3]:
            stats = self.book.get(variant.sid, variant.family)
            if stats.n == 0:
                continue
            top.append(f"{variant.sid}({stats.n}t {stats.expectancy:+.2f}R "
                       f"{stats.confidence})")
        top_text = ", ".join(top) or "no completed trades yet"
        line = (f"HEARTBEAT {self.clock.describe(now)} | {now:%H:%M} UTC | "
                f"session {self.sessions.session_at(now).value} | regime "
                f"{self._regime} | bias {bias} | spread "
                f"{spread_points:.0f}pt | news "
                f"{'BLOCKED' if news_blocked else 'clear'} | lock "
                f"{lock.value} | day "
                f"{self.guard.daily_combined_pct(self._floating_pnl()):+.2%} "
                f"weekDD {self.guard.weekly_dd_pct(equity):.2%} | shadows open "
                f"{len(self.shadow.open)} pending {len(self.shadow.pending)} | "
                f"real {real_status} | stop stage {stage} | top: {top_text}")
        api.Print(line)
        self.store.csv_append("heartbeat", {
            "time": now.isoformat(), "research_day": self.clock.day_index(now),
            "session": self.sessions.session_at(now).value,
            "regime": self._regime, "htf_bias": bias,
            "spread": f"{spread_points:.0f}",
            "news": news_reason if news_blocked else "clear",
            "shadow_open": len(self.shadow.open),
            "shadow_pending": len(self.shadow.pending),
            "real_status": real_status, "stop_stage": stage,
            "daily_pct":
                f"{self.guard.daily_combined_pct(self._floating_pnl()):+.4%}",
            "weekly_dd_pct": f"{self.guard.weekly_dd_pct(equity):.4%}",
            "top_strategies": top_text})

    # -------------------------------------------------------- restart support
    def _replay_today_history(self, now):
        """Rebuild today's realised P/L from the broker's own history so a
        mid-day restart cannot bypass the daily lock.  Positions are grouped
        by id, so a partially closed trade counts once."""
        today = now.date()
        seen = set()
        replayed = 0
        for i in range(int(api.History.Count)):
            h = api.History[i]
            try:
                if str(h.Label) != BOT_LABEL \
                        or str(h.SymbolName) != self._symbol_name:
                    continue
                closing = self._to_utc(h.ClosingTime)
                pid = int(h.PositionId)
            except Exception:
                continue
            if closing.date() != today or pid in seen:
                continue
            seen.add(pid)
            total = 0.0
            for j in range(int(api.History.Count)):
                hh = api.History[j]
                try:
                    if int(hh.PositionId) == pid:
                        total += float(hh.NetProfit)
                except Exception:
                    continue
            self.guard.register_close(total, now)
            if self.guard.day:
                self.guard.day.trades_opened += 1
            replayed += 1
        if replayed:
            self._real_trades_today = max(self._real_trades_today, replayed)
            api.Print(f"restart recovery: replayed {replayed} of today's "
                      f"closed V5 positions into the daily guard")

    def _adopt_open_position(self, saved, now):
        for i in range(int(api.Positions.Count)):
            pos = api.Positions[i]
            if str(pos.SymbolName) != self._symbol_name \
                    or str(pos.Label) != BOT_LABEL:
                continue
            sl = float(pos.StopLoss) if pos.StopLoss is not None else 0.0
            if sl <= 0:
                api.Print("adopted position has NO stop loss — closing it for "
                          "safety")
                try:
                    api.ClosePosition(pos)
                except Exception as exc:
                    api.Print(f"protective close failed: {exc!r}")
                continue
            restored = RealTradeRecord.from_dict(saved) if saved else None
            if restored is not None \
                    and restored.position_id == int(pos.Id):
                self._real = restored
                api.Print(f"restart recovery: resumed tracking of real "
                          f"position {int(pos.Id)} [{restored.sid} "
                          f"{restored.family}] at stop stage "
                          f"{restored.managed.stop_stage()}")
            else:
                direction = Direction.LONG if str(pos.TradeType) == "Buy" \
                    else Direction.SHORT
                entry = float(pos.EntryPrice)
                units = float(pos.VolumeInUnits)
                tp = float(pos.TakeProfit) if pos.TakeProfit is not None \
                    else 0.0
                spread_price = self._spread_points() * self.spec.point
                stop_bid = sl if direction == Direction.LONG \
                    else sl - spread_price
                target_bid = tp if direction == Direction.LONG \
                    else (tp - spread_price if tp > 0 else 0.0)
                risk_dist = abs(entry - stop_bid)
                mpu = self.spec.money_per_price_unit_per_unit()
                managed = ManagedTrade(
                    trade_id=new_id("adopted"), sid="ADOPTED",
                    family="ADOPTED", direction=direction, entry=entry,
                    initial_stop=stop_bid, stop=stop_bid,
                    tp1=target_bid or entry, target=target_bid or entry,
                    units_initial=units, units=units,
                    risk_dist=max(risk_dist, 1e-9),
                    risk_money=max(units * risk_dist * mpu, 1e-9),
                    entry_time=self._to_utc(pos.EntryTime))
                self._real = RealTradeRecord(
                    trade_id=managed.trade_id, position_id=int(pos.Id),
                    sid="ADOPTED", family="ADOPTED", version=0,
                    direction=direction, managed=managed,
                    entry_time=managed.entry_time or now, risk_pct=0.0,
                    risk_money_sized=managed.risk_money,
                    confluence_score=0.0, confluence_detail="",
                    stop_reason="adopted from the platform",
                    tp1_reason="", target_reason="", sweep_kind="",
                    regime=self._regime, session="OFF_HOURS",
                    htf_bias="", location="",
                    spread_points=self._spread_points(), adopted=True)
                api.Print(f"restart recovery: adopted unmatched position "
                          f"{int(pos.Id)} — it keeps its broker SL/TP and is "
                          f"managed conservatively (no partials, no "
                          f"trailing), and it is NOT recorded as research "
                          f"evidence")
            break

    def _save_state(self, now):
        self._last_state_save = now
        self.store.save_state({
            "clock": self.clock.snapshot(),
            "population": [v.to_dict() for v in self.population],
            "learning": self.book.snapshot(),
            "shadow": self.shadow.snapshot(),
            "guard": self.guard.snapshot(),
            "open_real": self._real.snapshot() if self._real else None,
            "counters": {"mutation_serial": self._mutation_serial},
            "last_day_reported": self._last_day_reported,
            "real_trades_today": self._real_trades_today,
            "real_trades_day": self._real_trades_day,
            "saved_at": now.isoformat()})

    # -------------------------------------------------------------- utilities
    def _best_variant_name(self):
        ranked = self.book.ranking(self.population)
        return ranked[0][0].sid if ranked else ""

    def _best_variant_score(self):
        ranked = self.book.ranking(self.population)
        return ranked[0][1] if ranked else 0.0

    def _best_family_name(self):
        fam = self.book.family_ranking()
        return fam[0][0] if fam else ""

    def _best_family_score(self):
        fam = self.book.family_ranking()
        return fam[0][1] if fam else 0.0

    def _previous_day_extremes(self, m5_candles):
        """Previous completed calendar day's high/low from M5 candles."""
        if not m5_candles:
            return None
        today = m5_candles[-1].time.date()
        prev_day = None
        for candle in reversed(m5_candles):
            day = candle.time.date()
            if day < today:
                prev_day = day
                break
        if prev_day is None:
            return None
        highs = [c.high for c in m5_candles if c.time.date() == prev_day]
        lows = [c.low for c in m5_candles if c.time.date() == prev_day]
        if not highs or not lows:
            return None
        return (max(highs), min(lows))

    def _symbol_is_gold(self, name):
        norm = "".join(ch for ch in name.upper() if ch.isalnum())
        allowed = {"".join(ch for ch in a.upper() if ch.isalnum())
                   for a in self.cfg.allowed_gold_symbols}
        return norm in allowed

    def _build_spec(self):
        sym = api.Symbol
        return CTraderSymbolSpec(
            name=self._symbol_name, digits=int(sym.Digits),
            tick_size=float(sym.TickSize), tick_value=float(sym.TickValue),
            pip_size=float(sym.PipSize), pip_value=float(sym.PipValue),
            volume_min=float(sym.VolumeInUnitsMin),
            volume_max=float(sym.VolumeInUnitsMax),
            volume_step=float(sym.VolumeInUnitsStep),
            spread_points=self._spread_points_raw(sym))

    def _spread_points_raw(self, sym):
        tick = float(sym.TickSize)
        return ((float(sym.Ask) - float(sym.Bid)) / tick) if tick > 0 else 0.0

    def _spread_points(self):
        return self._spread_points_raw(api.Symbol)

    def _market_open(self):
        try:
            return bool(api.Symbol.MarketHours.IsOpened())
        except Exception:
            return True

    def _to_utc(self, net_dt):
        t = datetime(int(net_dt.Year), int(net_dt.Month), int(net_dt.Day),
                     int(net_dt.Hour), int(net_dt.Minute),
                     int(net_dt.Second), tzinfo=UTC)
        return t - timedelta(hours=self.cfg.server_utc_offset_hours)

    def _now_utc(self):
        return self._to_utc(api.Server.Time)

    def _completed_candles(self, tf):
        bars = self._bars[tf]
        count = int(bars.Count)
        if count < 2:
            return []
        end = count - 1                     # exclude the forming bar
        start = max(0, end - self._windows[tf])
        out = []
        for i in range(start, end):
            out.append(Candle(
                time=self._to_utc(bars.OpenTimes[i]),
                open=float(bars.OpenPrices[i]),
                high=float(bars.HighPrices[i]),
                low=float(bars.LowPrices[i]),
                close=float(bars.ClosePrices[i]),
                volume=float(bars.TickVolumes[i])))
        return out

    def _symbol_positions_count(self):
        n = 0
        for i in range(int(api.Positions.Count)):
            if str(api.Positions[i].SymbolName) == self._symbol_name:
                n += 1
        return n

    def _find_position(self, position_id):
        for i in range(int(api.Positions.Count)):
            p = api.Positions[i]
            if str(p.SymbolName) == self._symbol_name \
                    and str(p.Label) == BOT_LABEL \
                    and int(p.Id) == position_id:
                return p
        return None

    def _has_pending_order(self):
        try:
            for i in range(int(api.PendingOrders.Count)):
                o = api.PendingOrders[i]
                if str(o.SymbolName) == self._symbol_name \
                        and str(o.Label) == BOT_LABEL:
                    return True
        except Exception:
            return False
        return False

    def _floating_pnl(self):
        total = 0.0
        for i in range(int(api.Positions.Count)):
            p = api.Positions[i]
            if str(p.SymbolName) == self._symbol_name \
                    and str(p.Label) == BOT_LABEL:
                total += float(p.NetProfit)
        return total

    def _emergency_present(self):
        name = self.cfg.emergency_stop_file
        candidates = (self.store.directory,
                      os.path.join(os.path.expanduser("~"), "Documents",
                                   "XAUUSD_Adaptive_Bot_V5"),
                      os.getcwd())
        for base in candidates:
            if base and os.path.exists(os.path.join(base, name)):
                return True
        return self.cfg.emergency_stop

    def _debug(self, msg):
        if self.cfg.debug_logging:
            api.Print(f"[v5] {msg}")
