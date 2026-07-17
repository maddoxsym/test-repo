"""
============================================================================
XAUUSD_Adaptive_Bot_V4 — 14-day autonomous DEMO research & paper trading
                          (GOLD only, DEMO only)
============================================================================

V4 runs a controlled 14-day research experiment on a Skilling DEMO account:
a population of exactly-specified strategy configurations (8 archetypes x
timeframes x bounded parameters) trades in parallel SHADOW portfolios on
completed candles, a statistical learning system ranks them risk-adjusted,
and at most ONE real DEMO position at a time is taken by the currently
best-evidenced strategy. Everything — research clock, strategies, stats,
rankings, guards — is persisted and survives restarts.

Absolute rails (cannot be configured away):
  * DEMO-only ("LIVE ACCOUNT BLOCKED" + stop; no live switch exists)
  * GOLD-only symbol allowlist
  * one real position; broker-side SL+TP on every order; stops never widen
  * 0.75% max risk/trade; 1.7% max combined daily loss; 5% weekly max;
    cooldown after 3 consecutive losses; volume rounded down
  * no martingale, no grid, no averaging down, no loss-chasing
  * restarting never resets limits or the research clock

The modular source lives in adaptive_bot/ (V3 detectors, reused untouched)
and adaptive_bot_v4/ (research system). Deployable single file:
XAUUSD_Adaptive_Bot_V4_main.py (generated; class XAUUSD_Adaptive_Bot_V4).
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
from adaptive_bot_v4.config_v4 import V4Config, V4ConfigValidator
from adaptive_bot_v4.features import FeatureBuilder
from adaptive_bot_v4.learning import LearningBook
from adaptive_bot_v4.persistence import StateStore
from adaptive_bot_v4.reporting import daily_report, final_report
from adaptive_bot_v4.risk_v4 import (AdaptiveRisk, V4Guard, V4OrderFacts,
                                     order_preflight)
from adaptive_bot_v4.shadow import ShadowEngine
from adaptive_bot_v4.strategy_space import (EvalContext, StrategyConfig,
                                            evaluate_strategy,
                                            seed_population)

UTC = timezone.utc
BOT_LABEL = "XAUUSD_Adaptive_Bot_V4"

DECISION_TFS = (Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H1)


class XAUUSD_Adaptive_Bot_V4(object):

    # ------------------------------------------------------------ lifecycle
    def on_start(self):
        self._fatal = False
        self._real = None                # dict record of the open real trade
        self._last_m1_open = None
        self._last_bucket = {tf: None for tf in DECISION_TFS}
        self._features = {}              # tf -> TFFeatures (latest)
        self._regime = Regime.UNSAFE.value
        self._last_heartbeat = None
        self._last_state_save = None
        self._fresh_signals = []         # (StrategyConfig, Signal) this bar
        self._lock_logged = None

        self.cfg = V4Config()
        validator = V4ConfigValidator(self.cfg)
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
            api.Print("V4 is a demo-only research bot. Connect the Skilling "
                      "DEMO account and restart.")
            self._fatal = True
            api.Stop()
            return
        api.Print("account check: DEMO account confirmed")

        # ---- GOLD-ONLY LOCK --------------------------------------------------
        self._symbol_name = str(api.SymbolName)
        if not self._symbol_is_gold(self._symbol_name):
            api.Print(f"SYMBOL BLOCKED: '{self._symbol_name}' is not an "
                      f"approved gold symbol. EURUSD and all non-gold "
                      f"symbols are refused.")
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
        api.Print(f"symbol spec: tick {self.spec.tick_size} | 1 unit per "
                  f"1.0 move = {mpu:.4f} {str(api.Account.Currency)} | "
                  f"volume min/step/max {self.spec.volume_min}/"
                  f"{self.spec.volume_step}/{self.spec.volume_max} units")
        if not (0.1 <= mpu <= 10.0):
            api.Print("per-unit value implausible for gold — refusing to "
                      "trade until verified")
            self._fatal = True
            api.Stop()
            return

        # ---- research stack ----------------------------------------------------
        self.store = StateStore(self.cfg, lambda m: api.Print(m))
        self.sessions = SessionManager(self.cfg)
        self.news = NewsFilter(self.cfg, UTC)
        self.spread_filter = SpreadFilter(self.cfg)
        self.features_builder = FeatureBuilder(self.cfg)
        self.regime_detector = MarketRegimeDetector(self.cfg)
        self.sizer = PositionSizer(self.cfg)
        self.guard = V4Guard(self.cfg)
        self.risk = AdaptiveRisk(self.cfg)
        self.book = LearningBook(self.cfg, lambda m: api.Print(m))
        self.shadow = ShadowEngine(
            self.cfg, lambda m: api.Print(m),
            on_close=self._on_shadow_close,
            record_row=self._shadow_row,
            on_watch_done=self.book.record_shadow_postmortem)
        self.rng = random.Random(self.cfg.population_seed)

        # ---- news honesty ------------------------------------------------------
        for bad in self.news.malformed_entries():
            api.Print(f"NEWS CONFIG: ignoring malformed entry: {bad}")
        if self.cfg.news_enabled and not (self.cfg.fomc_events
                                          or self.cfg.cpi_events):
            api.Print("*** NEWS DATES MISSING *** only the NFP first-Friday "
                      "rule is active. Add FOMC/CPI dates to config or set "
                      "require_news_calendar=True to block entries entirely.")
        api.Print("NEWS PROTECTION: schedule-based/manual only — no live "
                  "feed exists inside cTrader Python and none is claimed.")

        # ---- market data --------------------------------------------------------
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

        # ---- restart-proof research state ---------------------------------------
        state = self.store.load_state() or {}
        rs = state.get("research_start", "")
        if rs:
            try:
                self.research_start = datetime.fromisoformat(rs)
            except ValueError:
                self.research_start = now
        else:
            self.research_start = now
            api.Print(f"RESEARCH CLOCK STARTED: {now:%Y-%m-%d %H:%M} UTC "
                      f"(+{self.cfg.research_days} days)")
        self.final_report_done = bool(state.get("final_report_done", False))
        pop = state.get("population")
        if pop:
            self.population = [StrategyConfig.from_dict(d) for d in pop]
            api.Print(f"restored {len(self.population)} strategies from "
                      f"persisted state")
        else:
            self.population = seed_population(now.date().isoformat())
            api.Print(f"seeded {len(self.population)} initial strategies "
                      f"across 8 archetypes")
        if state.get("learning"):
            self.book.restore(state["learning"])
        if state.get("shadow"):
            self.shadow.restore(state["shadow"])
        self._mutation_serial = int(state.get("counters", {})
                                    .get("mutation_serial", 0))
        self._last_day_reported = state.get("last_day_reported", "")

        # guards: broker-history replay first, then persisted state merge —
        # limits can only get MORE restrictive, never less
        self.guard.roll(now, equity, balance)
        self._replay_today_history(now)
        if state.get("guard"):
            self.guard.restore(state["guard"], now)
        api.Print(f"daily guard: {self.guard.describe(equity)}")

        m5 = self._completed_candles(Timeframe.M5)
        self.sessions.rebuild_from(m5)

        self._adopt_open_position(state.get("open_real"), now)

        m1 = self._completed_candles(Timeframe.M1)
        if m1:
            self._last_m1_open = m1[-1].time
            for tf in DECISION_TFS:
                self._last_bucket[tf] = tf_bucket_start(m1[-1].time, tf)

        day_idx = self._day_index(now)
        api.Print(f"{BOT_LABEL} started | DEMO | day {day_idx}/"
                  f"{self.cfg.research_days} of research | "
                  f"{len(self.population)} strategies | max risk "
                  f"{self.cfg.max_risk_per_trade:.2%}/trade, daily "
                  f"{self.cfg.max_daily_loss:.1%} combined, weekly "
                  f"{self.cfg.max_weekly_drawdown:.0%} | state: "
                  f"{self.store.directory or 'NOT PERSISTED — FIX THIS'}")
        api.Print("no trading at startup — first decisions on the next "
                  "completed decision-timeframe candle")
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
            api.Print(f"NOTE: real position {self._real['position_id']} "
                      f"stays protected by broker-side SL/TP.")
        if self.store.directory:
            api.Print(f"research files: {self.store.directory}")

    # ------------------------------------------------------------ main tick
    def _tick(self):
        m1_bars = self._bars[Timeframe.M1]
        if int(m1_bars.Count) < 3:
            return
        last_open = self._to_utc(m1_bars.OpenTimes[int(m1_bars.Count) - 2])
        if self._last_m1_open is not None and last_open == self._last_m1_open:
            return
        self._last_m1_open = last_open

        m1 = self._completed_candles(Timeframe.M1)
        if not m1:
            return
        candle = m1[-1]
        now = candle.time + timedelta(minutes=1)
        equity = float(api.Account.Equity)
        balance = float(api.Account.Balance)
        spread_points = self._spread_points()

        self.sessions.update_ranges(candle)
        new_day, _ = self.guard.roll(now, equity, balance)
        if new_day:
            self._daily_pipeline(now, equity)
        self.guard.emergency = self._emergency_present()

        self._reconcile_real(now)
        self._update_real_excursions(candle)
        self._manage_real(candle, now, spread_points)

        # shadow books advance on every completed M1 candle
        mgmt_lookup = {p.sid: p.mgmt for p in self.population}
        tf_atr = {tf.value: (self._features.get(tf).atr_now
                             if self._features.get(tf) else 0.0)
                  for tf in DECISION_TFS}
        self.shadow.on_m1(candle, spread_points, self.spec.point, now,
                          mgmt_lookup, tf_atr)

        # decision timeframes: features + strategy evaluation on bar close
        self._fresh_signals = []
        for tf in DECISION_TFS:
            bucket = tf_bucket_start(candle.time, tf)
            if bucket == self._last_bucket[tf]:
                continue
            self._last_bucket[tf] = bucket
            self._on_tf_close(tf, now, spread_points)

        # a single real DEMO position, chosen by evidence
        if self._fresh_signals and self._real is None:
            self._consider_real(now, equity, spread_points)

        self._heartbeat(now, spread_points, equity)
        if self._last_state_save is None or \
                (now - self._last_state_save) >= timedelta(minutes=15):
            self._save_state(now)

    # ------------------------------------------------------------ tf close
    def _on_tf_close(self, tf, now, spread_points):
        candles = self._completed_candles(tf)
        marks = self.sessions.marks_for(now.date()) \
            if tf in (Timeframe.M5, Timeframe.M15) else None
        f = self.features_builder.build(tf, candles, marks)
        self._features[tf] = f
        if f is None:
            return
        if tf == Timeframe.M15:
            news_blocked, _ = self.news.blackout(now)
            reading = self.regime_detector.classify(
                f.candles, f.structure, spread_points, news_blocked)
            if reading.regime.value != self._regime:
                api.Print(f"regime: {self._regime} -> "
                          f"{reading.regime.value} ({reading.reason})")
            self._regime = reading.regime.value

        if self._research_over(now):
            return                        # no new research entries
        session = self.sessions.session_at(now).value
        ctx = EvalContext(
            regime=self._regime, session=session,
            spread_points=spread_points, point=self.spec.point, now=now,
            cost=self._cost_price_units(spread_points),
            min_net_rr=self.cfg.min_net_rr,
            asian_range=self.sessions.asian_range(now.date()),
            minutes_into_london=self._minutes_into(now,
                                                   self.cfg.london_start),
            minutes_into_ny=self._minutes_into(now, self.cfg.ny_start))

        shadow_ok, shadow_why = self._shadow_gates(now, spread_points)
        for p in self.population:
            if p.tf != tf.value or p.status != "active":
                continue
            try:
                sig = evaluate_strategy(p, f, ctx)
            except Exception as exc:
                api.Print(f"strategy {p.sid} crashed in evaluation: {exc!r}")
                continue
            if sig is None:
                continue
            self._fresh_signals.append((p, sig))
            if shadow_ok:
                if self.shadow.submit(p, sig, spread_points,
                                      self.spec.point, self._regime,
                                      session):
                    self._debug(f"shadow open queued: {p.sid} "
                                f"{sig.direction.value} stop {sig.stop:.2f} "
                                f"tgt {sig.target:.2f} ({sig.reason})")
            else:
                self._reject_row(now, "shadow-gate", p.sid, spread_points,
                                 shadow_why)

    def _shadow_gates(self, now, spread_points):
        if now.weekday() >= 5:
            return False, "weekend"
        if not bool(api.Symbol.MarketHours.IsOpened()):
            return False, "market closed"
        blocked, why = self.news.blackout(now)
        if blocked:
            return False, f"news blackout: {why}"
        ok, why = self.spread_filter.check(spread_points)
        if not ok:
            return False, why
        return True, ""

    # ------------------------------------------------------------ real entry
    def _consider_real(self, now, equity, spread_points):
        cfg = self.cfg
        if self._research_over(now):
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
        news_blocked, news_reason = self.news.blackout(now)
        if cfg.require_news_calendar and not (cfg.fomc_events
                                              or cfg.cpi_events):
            news_blocked, news_reason = True, \
                "require_news_calendar=True but no dates configured"
        if news_blocked:
            self._reject_row(now, "news", "-", spread_points, news_reason)
            return
        spread_ok, spread_why = self.spread_filter.check(spread_points)
        if not spread_ok:
            self._reject_row(now, "spread", "-", spread_points, spread_why)
            return

        chosen = self.book.select_real(self._fresh_signals, self._regime)
        if chosen is None:
            self._debug("no strategy is eligible for a real trade yet "
                        "(insufficient shadow evidence or negative "
                        "expectancy)")
            return
        scfg, sig = chosen
        stats = self.book.get(scfg.sid)
        day = self.guard.day
        daily_pl = (day.realised if day else 0.0) + min(0.0, floating)
        weekly_dd = 0.0
        if self.guard.week and self.guard.week.start_equity > 0:
            weekly_dd = max(0.0, (self.guard.week.start_equity
                                  - min(self.guard.week.min_equity, equity))
                            / self.guard.week.start_equity)
        f15 = self._features.get(Timeframe.M15)
        risk_frac, notes = self.risk.choose(
            self.book.score(scfg), stats.n, equity=equity,
            day_start_equity=day.start_equity if day else equity,
            daily_pl_combined=daily_pl, weekly_dd_frac=weekly_dd,
            consecutive_losses=self.guard.consecutive_losses,
            spread_points=spread_points,
            atr_percentile=f15.atr_percentile if f15 else 0.5,
            recent_strategy_r=sum(stats.recent[-3:]))
        for n in notes:
            self._debug(f"risk: {n}")
        if risk_frac <= 0:
            self._reject_row(now, "risk", scfg.sid, spread_points,
                             "; ".join(notes[-2:]))
            return

        entry = float(api.Symbol.Ask) if sig.direction == Direction.LONG \
            else float(api.Symbol.Bid)
        self.spec.spread_points = spread_points
        sizing = self.sizer.size(self.spec, equity, risk_frac, entry,
                                 sig.stop)
        facts = V4OrderFacts(
            is_demo_account=not bool(api.Account.IsLive),
            symbol_is_gold=self._symbol_is_gold(self._symbol_name),
            market_open=bool(api.Symbol.MarketHours.IsOpened()),
            spread_points=spread_points, spread_ok=spread_ok,
            positions_on_symbol=self._symbol_positions_count(),
            has_pending_bot_order=self._has_pending_order(),
            news_blocked=news_blocked, news_reason=news_reason,
            lock=lock, equity=equity, session_allowed=allowed,
            session_reason=window_why,
            research_over=self._research_over(now))
        pf = order_preflight(cfg, now, sig, entry, sizing.volume_units,
                             sizing.risk_money, sizing.rejected,
                             sizing.reason, facts,
                             cooldown_active=self.guard.cooldown_until
                             is not None and now < self.guard.cooldown_until)
        if cfg.debug_logging or not pf.ok:
            for line in pf.checks:
                api.Print(f"  order-safety {line}")
        if not pf.ok:
            self._reject_row(now, "preflight", scfg.sid, spread_points,
                             pf.reason)
            return

        trade_type = TradeType.Buy if sig.direction == Direction.LONG \
            else TradeType.Sell
        sl_pips = abs(entry - sig.stop) / self.spec.pip_size
        tp_pips = abs(sig.target - entry) / self.spec.pip_size
        result = api.ExecuteMarketOrder(trade_type, self._symbol_name,
                                        sizing.volume_units, BOT_LABEL,
                                        sl_pips, tp_pips)
        if not bool(result.IsSuccessful) or result.Position is None:
            err = str(result.Error) if result.Error is not None else "unknown"
            api.Print(f"ORDER FAILED: {err}")
            self._reject_row(now, "broker", scfg.sid, spread_points,
                             f"order rejected: {err}")
            return
        pos = result.Position
        fill = float(pos.EntryPrice)
        sl_price = round(sig.stop, self.spec.digits)
        tp_price = round(sig.target, self.spec.digits)
        try:
            api.ModifyPosition(pos, sl_price, tp_price)
        except Exception as exc:
            api.Print(f"note: SL/TP refine failed ({exc!r}); pip-based "
                      f"protection from fill remains")
        self._real = {
            "trade_id": new_id("real"), "position_id": int(pos.Id),
            "strategy_id": scfg.sid, "direction": sig.direction.value,
            "entry": fill, "stop": sl_price, "initial_stop": sl_price,
            "target": tp_price, "units": sizing.volume_units,
            "risk_money": sizing.risk_money,
            "risk_pct": sizing.risk_fraction_actual,
            "entry_time": now.isoformat(), "regime": self._regime,
            "session": session_obj.value, "reason": sig.reason,
            "spread_points": spread_points, "mfe_r": 0.0, "mae_r": 0.0,
            "bars_open": 0, "be_done": False, "partial_done": False,
            "mgmt": dict(scfg.mgmt), "mgmt_mode": scfg.mgmt_mode,
            "tf": scfg.tf,
        }
        self.guard.register_open(session_obj)
        api.Print(f"REAL ORDER FILLED [{scfg.sid} v{scfg.version}]: "
                  f"{sig.direction.value} {sizing.volume_units} units @ "
                  f"{fill:.2f} SL {sl_price:.2f} TP {tp_price:.2f} risk "
                  f"{sizing.risk_money:.2f} ({sizing.risk_fraction_actual:.2%}) "
                  f"| {sig.reason}")
        self._save_state(now)

    # ------------------------------------------------------------ real upkeep
    def _reconcile_real(self, now):
        if self._real is None:
            return
        r = self._real
        if self._find_position(r["position_id"]) is not None:
            return
        h = self._find_history(r["position_id"])
        profit = float(h.NetProfit) if h is not None else 0.0
        exit_price = float(h.ClosingPrice) if h is not None else 0.0
        exit_time = self._to_utc(h.ClosingTime) if h is not None else now
        r_mult = profit / r["risk_money"] if r["risk_money"] > 0 else 0.0
        self.guard.register_close(profit, now)
        self.book.record_real(
            r["strategy_id"], r_mult, r["regime"], r["session"],
            exit_time.strftime("%a"), r["mfe_r"], r["mae_r"],
            r["bars_open"])
        self.store.csv_append("real_trades", {
            "trade_id": r["trade_id"], "position_id": r["position_id"],
            "strategy_id": r["strategy_id"], "direction": r["direction"],
            "entry_time": r["entry_time"],
            "exit_time": exit_time.isoformat(), "entry": f"{r['entry']:.2f}",
            "stop": f"{r['initial_stop']:.2f}",
            "target": f"{r['target']:.2f}",
            "exit_price": f"{exit_price:.2f}",
            "exit_reason": self._infer_exit(r, exit_price),
            "units": r["units"], "risk_pct": f"{r['risk_pct']:.4%}",
            "risk_money": f"{r['risk_money']:.2f}",
            "profit": f"{profit:.2f}", "r_multiple": f"{r_mult:.2f}",
            "mfe_r": f"{r['mfe_r']:.2f}", "mae_r": f"{r['mae_r']:.2f}",
            "bars_open": r["bars_open"],
            "spread_points": f"{r['spread_points']:.0f}",
            "regime": r["regime"], "session": r["session"],
            "reason": r["reason"]})
        api.Print(f"REAL TRADE CLOSED [{r['strategy_id']}]: "
                  f"{profit:+.2f} ({r_mult:+.2f}R) | "
                  f"{self.guard.describe(float(api.Account.Equity))}")
        self._real = None
        self._save_state(now)

    def _infer_exit(self, r, exit_price):
        if exit_price <= 0:
            return "BROKER_CLOSED"
        tol = 3 * self.spec.tick_size + self.spec.spread_points * self.spec.point
        if abs(exit_price - r["stop"]) <= tol:
            return "STOP_LOSS"
        if abs(exit_price - r["target"]) <= tol:
            return "TAKE_PROFIT"
        return "MANAGED_EXIT"

    def _update_real_excursions(self, candle):
        r = self._real
        if r is None:
            return
        risk = abs(r["entry"] - r["initial_stop"])
        if risk <= 0:
            return
        d = 1 if r["direction"] == "LONG" else -1
        fav = ((candle.high - r["entry"]) if d > 0
               else (r["entry"] - candle.low)) / risk
        adv = ((r["entry"] - candle.low) if d > 0
               else (candle.high - r["entry"])) / risk
        r["mfe_r"] = max(r["mfe_r"], fav)
        r["mae_r"] = max(r["mae_r"], adv)
        r["bars_open"] += 1

    def _manage_real(self, candle, now, spread_points):
        r = self._real
        if r is None:
            return
        pos = self._find_position(r["position_id"])
        if pos is None:
            return
        d = 1 if r["direction"] == "LONG" else -1
        risk = abs(r["entry"] - r["initial_stop"])
        if risk <= 0:
            return
        r_now = (candle.close - r["entry"]) * d / risk
        mode = r["mgmt_mode"]
        mgmt = r["mgmt"]
        # weekend flat
        if self.sessions.near_weekend_flat(now) \
                and not self.cfg.weekend_hold_allowed:
            try:
                api.ClosePosition(pos)
                api.Print("real position closed: pre-weekend flat")
            except Exception as exc:
                api.Print(f"weekend close failed: {exc!r}")
            return
        new_stop = None
        if mode in ("BE_1R", "PARTIAL_RUNNER", "ATR_TRAIL", "STRUCT_TRAIL") \
                and not r["be_done"] and r_now >= mgmt.get("be_r", 1.2):
            be = r["entry"] + d * (spread_points
                                   + self.cfg.slippage_buffer_points) \
                * self.spec.point
            if (be - r["stop"]) * d > 0:
                new_stop = be
            r["be_done"] = True
        if mode == "PARTIAL_RUNNER" and not r["partial_done"] \
                and r_now >= mgmt.get("partial_r", 1.8):
            vol = self.spec.round_volume_down(
                r["units"] * mgmt.get("partial_frac", 0.5))
            if vol >= self.spec.volume_min and vol < r["units"]:
                try:
                    res = api.ClosePosition(pos, vol)
                    if bool(res.IsSuccessful):
                        r["units"] -= vol
                        r["partial_done"] = True
                        api.Print(f"real partial close {vol} units at "
                                  f"+{r_now:.2f}R")
                except Exception as exc:
                    api.Print(f"partial close failed: {exc!r}")
        if mode in ("ATR_TRAIL", "STRUCT_TRAIL") and r_now >= 1.0:
            f = self._features.get(Timeframe(r["tf"])) \
                if r["tf"] in ("M5", "M15", "M30", "H1") else None
            atr = f.atr_now if f else 0.0
            if atr > 0:
                trail = candle.close - d * mgmt.get("trail_atr", 2.0) * atr
                if new_stop is None or (trail - new_stop) * d > 0:
                    if (trail - r["stop"]) * d > 0:
                        new_stop = trail
        if new_stop is not None and (new_stop - r["stop"]) * d > 0:
            price = round(new_stop, self.spec.digits)
            try:
                api.ModifyPosition(pos, price, pos.TakeProfit)
                r["stop"] = price
                api.Print(f"real stop tightened to {price:.2f} "
                          f"(stops never widen)")
            except Exception as exc:
                api.Print(f"stop move failed: {exc!r}")

    # ------------------------------------------------------------ research ops
    def _research_over(self, now) -> bool:
        return (now - self.research_start) >= timedelta(
            days=self.cfg.research_days)

    def _day_index(self, now) -> int:
        return min(self.cfg.research_days,
                   (now - self.research_start).days + 1)

    def _daily_pipeline(self, now, equity):
        today = now.date().isoformat()
        if self._last_day_reported == today:
            return
        self._last_day_reported = today
        day_idx = self._day_index(now)
        api.Print(f"=== new trading day: research day {day_idx}/"
                  f"{self.cfg.research_days} ===")
        acct = [f"equity {equity:.2f} | {self.guard.describe(equity)}"]
        rep = daily_report(now, day_idx, self.cfg.research_days, self.book,
                           self.population, acct)
        self.store.write_text(f"daily_report_{today}.txt", rep)
        ranked = self.book.ranking(self.population)
        best = ranked[0] if ranked else None
        self.store.csv_append("daily_summary", {
            "date": today, "day_index": day_idx,
            "start_equity": f"{equity:.2f}", "end_equity": "",
            "realised_pl": f"{self.guard.day.realised:.2f}"
            if self.guard.day else "0",
            "realised_pct": "", "trades_real": self.guard.day.trades_opened
            if self.guard.day else 0,
            "trades_shadow": sum(self.book.get(p.sid).n
                                 for p in self.population),
            "wins_real": self.guard.day.wins if self.guard.day else 0,
            "losses_real": self.guard.day.losses if self.guard.day else 0,
            "max_daily_dd_pct": "", "weekly_dd_pct": "",
            "active_strategies": sum(1 for p in self.population
                                     if p.status == "active"),
            "benched": sum(1 for p in self.population
                           if p.status == "benched"),
            "retired": sum(1 for p in self.population
                           if p.status == "retired"),
            "best_strategy": best[0].sid if best else "",
            "best_score": f"{best[1]:+.3f}" if best else "",
            "regime_mix": self._regime, "lock_events": ""})

        if not self._research_over(now):
            self.population, decisions = self.book.daily_update(
                self.population, self.rng, today)
            for d in decisions:
                self.store.csv_append("learning_log", d)
                if d["kind"] == "ADAPT":
                    self.store.csv_append("parameter_updates", {
                        "date": d["date"], "sid": d["sid"], "version": "",
                        "param": "", "old": "", "new": "",
                        "evidence": d["why"]})
        elif not self.final_report_done:
            self._write_final_report(now, equity)
        self._save_state(now)

    def _write_final_report(self, now, equity):
        api.Print("=== 14-DAY RESEARCH COMPLETE — writing final report ===")
        acct = {
            "final_equity": f"{equity:.2f}",
            "week_realised": f"{self.guard.week.realised:.2f}"
            if self.guard.week else "0",
            "real_trades_total": sum(self.book.get(p.sid).n_real
                                     for p in self.population),
        }
        notes = [f"research ran {self.research_start:%Y-%m-%d} -> "
                 f"{now:%Y-%m-%d}; full trails in equity_history.csv, "
                 f"daily_summary.csv, shadow_trades.csv, real_trades.csv"]
        text, js = final_report(now, self.research_start, self.book,
                                self.population, acct, notes)
        self.store.write_text("final_report.txt", text)
        self.store.write_json("final_report.json", js)
        for line in text.splitlines()[:12]:
            api.Print(line)
        api.Print(f"full report: "
                  f"{os.path.join(self.store.directory or '', 'final_report.txt')}")
        self.final_report_done = True

    # ------------------------------------------------------------ callbacks
    def _on_shadow_close(self, t):
        self.book.record_shadow(t)
        self._debug(f"shadow closed: {t.strategy_id} {t.exit_reason} "
                    f"{t.r_multiple:+.2f}R (MFE {t.mfe_r:.2f}/MAE "
                    f"{t.mae_r:.2f})")

    def _shadow_row(self, t):
        self.store.csv_append("shadow_trades", {
            "trade_id": t.trade_id, "strategy_id": t.strategy_id,
            "tf": t.tf, "direction": t.direction.value,
            "signal_time": t.signal_time.isoformat(),
            "entry_time": t.entry_time.isoformat() if t.entry_time else "",
            "exit_time": t.exit_time.isoformat() if t.exit_time else "",
            "entry": f"{t.entry:.2f}", "stop": f"{t.initial_stop:.2f}",
            "target": f"{t.target:.2f}", "exit_price": f"{t.exit_price:.2f}",
            "exit_reason": t.exit_reason, "units": f"{t.units:.2f}",
            "risk_pct": f"{t.risk_pct:.4%}",
            "risk_money": f"{t.risk_money:.2f}", "profit": f"{t.profit:.2f}",
            "r_multiple": f"{t.r_multiple:.2f}", "mfe_r": f"{t.mfe_r:.2f}",
            "mae_r": f"{t.mae_r:.2f}", "bars_open": t.bars_open,
            "spread_points": f"{t.spread_points:.0f}", "regime": t.regime,
            "session": t.session, "mgmt_mode": t.mgmt_mode,
            "reason": t.reason})

    def _reject_row(self, now, stage, sid, spread_points, reason):
        self._debug(f"rejected [{stage}] {sid}: {reason}")
        self.store.csv_append("rejections", {
            "time": now.isoformat(), "stage": stage, "strategy_id": sid,
            "regime": self._regime,
            "session": self.sessions.session_at(now).value,
            "spread_points": f"{spread_points:.0f}", "reason": reason})

    # ------------------------------------------------------------ heartbeat
    def _heartbeat(self, now, spread_points, equity):
        if self._last_heartbeat is not None and \
                (now - self._last_heartbeat) < timedelta(
                    minutes=self.cfg.heartbeat_minutes):
            return
        self._last_heartbeat = now
        floating = self._floating_pnl()
        lock = self.guard.lock_reason(equity, floating,
                                      self.sessions.session_at(now), now)
        day = self.guard.day
        daily_pl = ((day.realised + min(0.0, floating))
                    / day.start_equity * 100) if day and day.start_equity \
            else 0.0
        weekly_dd = 0.0
        if self.guard.week and self.guard.week.start_equity > 0:
            weekly_dd = max(0.0, (self.guard.week.start_equity
                                  - min(self.guard.week.min_equity, equity))
                            / self.guard.week.start_equity * 100)
        ranked = self.book.ranking(self.population)[:3]
        top = ", ".join(f"{p.sid}({sc:+.2f})" for p, sc in ranked)
        news_blocked, news_why = self.news.blackout(now)
        api.Print(
            f"HEARTBEAT day {self._day_index(now)}/{self.cfg.research_days} "
            f"| {now:%H:%M} UTC | bid {float(api.Symbol.Bid):.2f} spread "
            f"{spread_points:.0f}pt | regime {self._regime} | session "
            f"{self.sessions.session_at(now).value} | lock "
            f"{lock.value} | dayPL {daily_pl:+.2f}% weekDD {weekly_dd:.2f}% "
            f"| news {'BLOCKED: ' + news_why if news_blocked else 'clear'} "
            f"| shadows open {len(self.shadow.open)} pending "
            f"{len(self.shadow.pending)} | real "
            f"{'OPEN ' + self._real['strategy_id'] if self._real else 'flat'} "
            f"| top: {top}")
        self.store.csv_append("equity_history", {
            "time": now.isoformat(), "equity": f"{equity:.2f}",
            "balance": f"{float(api.Account.Balance):.2f}",
            "floating": f"{floating:.2f}",
            "daily_pl_pct": f"{daily_pl:.3f}",
            "weekly_dd_pct": f"{weekly_dd:.3f}"})

    # ------------------------------------------------------------ recovery
    def _replay_today_history(self, now):
        today = now.date()
        replayed = 0
        for i in range(int(api.History.Count)):
            h = api.History[i]
            try:
                if str(h.Label) != BOT_LABEL or \
                        str(h.SymbolName) != self._symbol_name:
                    continue
                closing = self._to_utc(h.ClosingTime)
            except Exception:
                continue
            if closing.date() != today:
                continue
            self.guard.register_close(float(h.NetProfit), now)
            if self.guard.day:
                self.guard.day.trades_opened += 1
            replayed += 1
        if replayed:
            api.Print(f"restart recovery: replayed {replayed} of today's "
                      f"closed V4 trades into the daily guard")

    def _adopt_open_position(self, saved, now):
        for i in range(int(api.Positions.Count)):
            pos = api.Positions[i]
            if str(pos.SymbolName) != self._symbol_name or \
                    str(pos.Label) != BOT_LABEL:
                continue
            sl = float(pos.StopLoss) if pos.StopLoss is not None else 0.0
            if sl <= 0:
                api.Print("adopted position has NO stop loss — closing for "
                          "safety")
                try:
                    api.ClosePosition(pos)
                except Exception as exc:
                    api.Print(f"protective close failed: {exc!r}")
                continue
            if saved and int(saved.get("position_id", -1)) == int(pos.Id):
                self._real = saved
                api.Print(f"restart recovery: resumed tracking of real "
                          f"position {int(pos.Id)} "
                          f"[{saved.get('strategy_id')}]")
            else:
                tp = float(pos.TakeProfit) if pos.TakeProfit is not None \
                    else 0.0
                entry = float(pos.EntryPrice)
                units = float(pos.VolumeInUnits)
                risk = abs(entry - sl) * units \
                    * self.spec.money_per_price_unit_per_unit()
                self._real = {
                    "trade_id": new_id("adopted"),
                    "position_id": int(pos.Id), "strategy_id": "ADOPTED",
                    "direction": "LONG" if str(pos.TradeType) == "Buy"
                    else "SHORT", "entry": entry, "stop": sl,
                    "initial_stop": sl, "target": tp, "units": units,
                    "risk_money": max(risk, 1e-9), "risk_pct": 0.0,
                    "entry_time": self._to_utc(pos.EntryTime).isoformat(),
                    "regime": self._regime, "session": "OFF_HOURS",
                    "reason": "adopted after restart",
                    "spread_points": 0.0, "mfe_r": 0.0, "mae_r": 0.0,
                    "bars_open": 0, "be_done": False, "partial_done": False,
                    "mgmt": {"mode": 0.0}, "mgmt_mode": "FULL_TP",
                    "tf": "M5"}
                api.Print(f"restart recovery: adopted unmatched position "
                          f"{int(pos.Id)} (managed as FULL_TP)")
            break

    def _save_state(self, now):
        self._last_state_save = now
        self.store.save_state({
            "research_start": self.research_start.isoformat(),
            "final_report_done": self.final_report_done,
            "population": [p.to_dict() for p in self.population],
            "learning": self.book.snapshot(),
            "shadow": self.shadow.snapshot(),
            "guard": self.guard.snapshot(),
            "open_real": self._real,
            "counters": {"mutation_serial": self._mutation_serial},
            "last_day_reported": self._last_day_reported,
            "saved_at": now.isoformat()})

    # ------------------------------------------------------------- utilities
    def _minutes_into(self, now, start_hour):
        delta = (now.hour - start_hour) * 60 + now.minute
        return delta if 0 <= delta <= 120 else None

    def _cost_price_units(self, spread_points):
        mpu = self.spec.money_per_price_unit_per_unit()
        return (spread_points + self.cfg.slippage_buffer_points) \
            * self.spec.point \
            + (2 * self.cfg.commission_per_unit / mpu if mpu > 0 else 0.0)

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
        end = count - 1                    # exclude the forming bar
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
                    and str(p.Label) == BOT_LABEL and int(p.Id) == position_id:
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

    def _find_history(self, position_id):
        for i in range(int(api.History.Count) - 1, -1, -1):
            h = api.History[i]
            try:
                if int(h.PositionId) == position_id:
                    return h
            except Exception:
                continue
        return None

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
                                   "XAUUSD_Adaptive_Bot_V4"),
                      os.getcwd())
        for base in candidates:
            if base and os.path.exists(os.path.join(base, name)):
                return True
        return self.cfg.emergency_stop

    def _debug(self, msg):
        if self.cfg.debug_logging:
            api.Print(f"[v4] {msg}")
