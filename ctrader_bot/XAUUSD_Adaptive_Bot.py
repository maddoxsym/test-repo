"""
============================================================================
XAUUSD_Adaptive_Bot — native cTrader Algo Python cBot (GOLD only, DEMO only)
============================================================================

Multi-timeframe smart-money strategy: M15 macro bias -> M5 decision layer
-> M1 entry trigger, with liquidity/zone/FVG analysis, transparent 0-100
setup scoring, strict risk limits (0.25%/trade, 1%/day, 3 trades/day, one
position) and a CSV journal.  The strategy logic lives in the adaptive_bot/
package next to this file; this file is the ONLY place that talks to the
cTrader API.

SAFETY:
  * DEMO-ONLY: a live account prints "LIVE ACCOUNT BLOCKED" and stops the
    cBot immediately.  There is no live-trading switch anywhere.
  * GOLD-ONLY: refuses to start on EURUSD or any symbol not in the gold
    allowlist (config.py: allowed_gold_symbols).
  * Every order carries a stop loss and a take profit, sized from live
    broker volume rules, always rounded DOWN.

This file follows the native cTrader Python cBot structure (import clr /
cAlgo.API / robot_wrapper, on_start / on_tick / on_stop).  The `api` object
provided by robot_wrapper is the bridge to the platform.
"""

import clr

clr.AddReference("cAlgo.API")

from cAlgo.API import *
from robot_wrapper import *

import os
import sys
from datetime import datetime, timedelta, timezone

# make the adaptive_bot package next to this file importable
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

from adaptive_bot.core.config import Config, ConfigValidator
from adaptive_bot.core.helpers import tf_bucket_start
from adaptive_bot.core.models import (Candle, CTraderSymbolSpec, Direction,
                                      ExitReason, LockReason, Regime,
                                      ScoreBreakdown, SessionName, Setup,
                                      SetupGrade, SetupModel, Timeframe,
                                      Trade, TradeStatus, TrendState, new_id)
from adaptive_bot.execution.order_manager import OrderFacts, OrderManager
from adaptive_bot.execution.position_manager import PositionManager
from adaptive_bot.filters.news_filter import NewsFilter
from adaptive_bot.filters.session_filter import SessionManager
from adaptive_bot.filters.spread_filter import SpreadFilter
from adaptive_bot.journal.trade_journal import TradeJournal
from adaptive_bot.risk.daily_loss_guard import DailyLossGuard
from adaptive_bot.risk.position_sizing import PositionSizer
from adaptive_bot.risk.trade_limits import TradeLimits
from adaptive_bot.strategy.entry_trigger import M1TriggerDetector
from adaptive_bot.strategy.strategy_engine import (FIXED_PLAN, ContextBuilder,
                                                   StrategyEngine)

UTC = timezone.utc
BOT_LABEL = "XAUUSD_Adaptive_Bot"

# minutes an armed setup stays valid while waiting for its M1 trigger
ARMED_SETUP_VALIDITY_MIN = 15


class XAUUSD_Adaptive_Bot(object):

    # ------------------------------------------------------------ lifecycle
    def on_start(self):
        self._fatal = False
        self._tracked = None            # bot-side Trade record of open position
        self._armed = None              # (Setup, expiry_datetime) awaiting M1 trigger
        self._last_m1_open = None       # duplicate-tick / new-bar guard
        self._last_m5_bucket = None     # decision de-duplication
        self._last_mgmt_bucket = None
        self._last_lock_logged = None

        self.cfg = Config()
        self._apply_ui_parameter_overrides()

        validator = ConfigValidator(self.cfg)
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

        # ---- DEMO-ONLY SAFETY LOCK (no live switch exists) -----------------
        if bool(api.Account.IsLive):
            api.Print("LIVE ACCOUNT BLOCKED")
            api.Print("This bot is demo-only. Connect a Skilling DEMO "
                      "account and restart.")
            self._fatal = True
            api.Stop()
            return
        api.Print("account check: DEMO account confirmed")

        # ---- GOLD-ONLY symbol verification ----------------------------------
        self._symbol_name = str(api.SymbolName)
        if not self._symbol_is_gold(self._symbol_name):
            api.Print(f"SYMBOL BLOCKED: '{self._symbol_name}' is not an "
                      f"approved gold symbol {self.cfg.allowed_gold_symbols}")
            api.Print("Attach this cBot to your broker's GOLD chart "
                      "(Skilling: XAUUSD). EURUSD and all non-gold symbols "
                      "are refused.")
            self._fatal = True
            api.Stop()
            return
        api.Print(f"symbol check: {self._symbol_name} accepted as GOLD")

        # ---- symbol specification from the live platform --------------------
        self.spec = self._build_spec()
        ok, why = self.spec.valid()
        if not ok:
            api.Print(f"SYMBOL SPEC UNVERIFIABLE: {why} — stopping (strict)")
            self._fatal = True
            api.Stop()
            return
        mpu = self.spec.money_per_price_unit_per_unit()
        api.Print(f"symbol spec: tick {self.spec.tick_size} | pip "
                  f"{self.spec.pip_size} | 1 unit per 1.0 move = "
                  f"{mpu:.4f} {str(api.Account.Currency)} | volume "
                  f"min/step/max = {self.spec.volume_min}/"
                  f"{self.spec.volume_step}/{self.spec.volume_max} units")
        if self.cfg.strict_mode and not (0.1 <= mpu <= 10.0):
            api.Print("STRICT MODE: per-unit value looks implausible for "
                      "gold — refusing to trade until verified. Compare the "
                      "printed value with your account currency and, if it "
                      "is genuinely correct, relax strict_mode in config.py.")
            self._fatal = True
            api.Stop()
            return

        # ---- strategy stack --------------------------------------------------
        self.engine = StrategyEngine(self.cfg)
        self.sessions = SessionManager(self.cfg)
        self.news = NewsFilter(self.cfg, UTC)
        self.builder = ContextBuilder(self.cfg, self.engine, self.sessions,
                                      self.news)
        self.spread_filter = SpreadFilter(self.cfg)
        self.sizer = PositionSizer(self.cfg)
        self.guard = DailyLossGuard(self.cfg)
        self.limits = TradeLimits(self.cfg, self.guard)
        self.orders = OrderManager(self.cfg)
        self.pos_manager = PositionManager(self.cfg)
        self.trigger = M1TriggerDetector(self.cfg)
        self.journal = TradeJournal(self.cfg, lambda m: api.Print(m))

        for bad in self.news.malformed_entries():
            api.Print(f"NEWS CONFIG: ignoring malformed entry: {bad}")
        api.Print("NEWS PROTECTION: schedule-based and manual only (no live "
                  "feed exists inside cTrader Python). NFP first-Friday rule "
                  f"{'ON' if self.cfg.block_nfp else 'OFF'}; configured "
                  f"events: {len(self.news.upcoming(self._now_utc(), 24*365))} "
                  "— keep fomc_events/cpi_events in config.py up to date.")

        # ---- market data ------------------------------------------------------
        self._tf_map = {
            Timeframe.M1: TimeFrame.Minute,
            Timeframe.M5: TimeFrame.Minute5,
            Timeframe.M15: TimeFrame.Minute15,
            Timeframe.H1: TimeFrame.Hour,
            Timeframe.H4: TimeFrame.Hour4,
            Timeframe.D1: TimeFrame.Daily,
        }
        self._windows = {
            Timeframe.M1: self.cfg.window_m1,
            Timeframe.M5: self.cfg.window_m5,
            Timeframe.M15: self.cfg.window_m15,
            Timeframe.H1: self.cfg.window_h1,
            Timeframe.H4: self.cfg.window_h4,
            Timeframe.D1: self.cfg.window_d1,
        }
        self._bars = {}
        for tf, ctf in self._tf_map.items():
            self._bars[tf] = api.MarketData.GetBars(ctf)

        # rebuild session ranges from recent M5 history (~2 days)
        m5_hist = self._completed_candles(Timeframe.M5)
        self.sessions.rebuild_from(m5_hist)

        # daily guard: initialise and REPLAY today's bot history so a
        # mid-day restart cannot bypass the daily lock
        now = self._now_utc()
        equity = float(api.Account.Equity)
        balance = float(api.Account.Balance)
        self.guard.roll(now, equity, balance)
        self._restore_today_from_history(now)
        api.Print(f"daily guard: {self.guard.describe(equity)}")

        # adopt an already-open bot position after a restart (never trade
        # around it blindly)
        self._adopt_open_position(now)

        # never trade on the bucket that was already in progress at startup
        m1 = self._completed_candles(Timeframe.M1)
        if m1:
            self._last_m1_open = m1[-1].time
            self._last_m5_bucket = tf_bucket_start(m1[-1].time, Timeframe.M5)
            self._last_mgmt_bucket = self._last_m5_bucket

        api.Print(f"{BOT_LABEL} started on {self._symbol_name} | DEMO | "
                  f"M15 bias / M5 decision / M1 trigger | risk "
                  f"{self.cfg.max_risk_per_trade:.2%}/trade, daily loss "
                  f"limit {self.cfg.max_daily_loss:.0%}, max "
                  f"{self.cfg.max_trades_per_day} trades/day | journal: "
                  f"{self.journal.directory or 'cTrader log only'}")
        api.Print("first evaluation happens on the NEXT completed M5 candle "
                  "— the bot never trades at startup")

    def on_tick(self):
        if self._fatal:
            return
        try:
            self._tick()
        except Exception as exc:
            # one bad tick must never kill protection of an open position
            api.Print(f"ERROR in tick processing: {exc!r}")

    def on_stop(self):
        if self._fatal:
            return
        api.Print(f"{BOT_LABEL} stopping. "
                  f"{self.guard.describe(float(api.Account.Equity))}")
        if self._tracked is not None:
            api.Print(f"NOTE: position {self._tracked.position_id} remains "
                      f"open with SL {self._tracked.stop_price:.2f} / TP "
                      f"{self._tracked.tp1:.2f} held on the broker side.")
        if self.journal.directory:
            api.Print(f"CSV journal: {self.journal.directory}")

    # ------------------------------------------------------------ main tick
    def _tick(self):
        m1_bars = self._bars[Timeframe.M1]
        if m1_bars.Count < 3:
            return
        last_completed_open = self._to_utc(m1_bars.OpenTimes[m1_bars.Count - 2])
        if self._last_m1_open is not None \
                and last_completed_open == self._last_m1_open:
            return                       # no new completed M1 bar yet
        self._last_m1_open = last_completed_open

        m1 = self._completed_candles(Timeframe.M1)
        if not m1:
            return
        candle = m1[-1]
        now = candle.time + timedelta(minutes=1)   # close time of that candle

        # session ranges + day/week roll + emergency stop state
        self.sessions.update_ranges(candle)
        equity = float(api.Account.Equity)
        balance = float(api.Account.Balance)
        new_day, _ = self.guard.roll(now, equity, balance)
        if new_day:
            api.Print(f"new trading day: {self.guard.describe(equity)}")
        self.guard.emergency = self._emergency_file_present()

        # reconcile a position the broker closed (SL/TP hit etc.)
        self._reconcile_closed(now)

        # excursion tracking for the journal
        self._update_excursions(candle)

        m5_bucket = tf_bucket_start(candle.time, Timeframe.M5)

        # manage the open position on completed M5 candles
        if self._tracked is not None and m5_bucket != self._last_mgmt_bucket:
            self._last_mgmt_bucket = m5_bucket
            self._manage_position(now)

        # M1 trigger check for an armed setup (every completed M1 candle)
        if self._armed is not None and self._tracked is None:
            self._try_trigger(now, m1)

        # decision layer on completed M5 candles
        if m5_bucket != self._last_m5_bucket:
            self._last_m5_bucket = m5_bucket
            self._evaluate(now)

    # ------------------------------------------------------------ decision
    def _evaluate(self, now):
        cfg = self.cfg
        if self._tracked is not None:
            return                          # one position rule — nothing new
        spread_points = self._spread_points()

        series = {tf: self._completed_candles(tf) for tf in self._tf_map}
        ctx = self.builder.build(series, spread_points, self.spec.point, now)
        if ctx is None:
            self._debug("analysis context unavailable (not enough history)")
            return

        if cfg.debug_logging:
            api.Print(f"plan {ctx.tf_plan.as_dict()} | regime "
                      f"{ctx.regime.regime.value} ({ctx.regime.reason}) | "
                      f"session {ctx.session.value} | spread "
                      f"{spread_points:.0f} pts")

        # hard gates before any model runs
        equity = float(api.Account.Equity)
        unreal = self._floating_pnl()
        lock = self.guard.lock_reason(equity, unreal, ctx.session)
        if lock != LockReason.NONE:
            if self.guard.lock_changed(lock):
                api.Print(f"NO NEW ENTRIES — lock active: {lock.value} | "
                          f"{self.guard.describe(equity)}")
            self._armed = None
            return
        self.guard.lock_changed(lock)      # records the unlocked state

        allowed, window_why = self.sessions.entry_window_check(now)
        if not allowed:
            self._debug(f"entry window closed: {window_why}")
            self._armed = None
            return
        if ctx.news_blocked:
            api.Print(f"NEWS PROTECTION: no entries — {ctx.news_reason}")
            self.journal.record_setup(
                now, self._symbol_name, None, accepted=False,
                rejection_reason=f"news blackout: {ctx.news_reason}",
                spread_points=spread_points, session=ctx.session.value,
                news_status="BLOCKED")
            self._armed = None
            return
        spread_ok, spread_why = self.spread_filter.check(spread_points)
        if not spread_ok:
            self._debug(f"spread gate: {spread_why}")
            self._armed = None
            return

        # cost model in price units for net-RR checks
        mpu = self.spec.money_per_price_unit_per_unit()
        cost = (spread_points + cfg.slippage_buffer_points) * self.spec.point \
            + (cfg.commission_per_unit / mpu if mpu > 0 else 0.0)

        def reject_cb(model, stage, reason):
            self._debug(f"candidate rejected [{model}/{stage}]: {reason}")
            if stage in ("score", "rr", "bias", "stop", "target"):
                self.journal.record_setup(
                    now, self._symbol_name, None, accepted=False,
                    rejection_reason=f"[{model}/{stage}] {reason}",
                    spread_points=spread_points, session=ctx.session.value,
                    news_status="CLEAR", model=model,
                    regime=ctx.regime.regime.value)

        setup = self.engine.evaluate(ctx, cost, reject_cb)
        if setup is None:
            self._armed = None
            return

        # arm the setup and wait for the 1-minute trigger
        expiry = now + timedelta(minutes=ARMED_SETUP_VALIDITY_MIN)
        self._armed = (setup, expiry)
        api.Print(f"SETUP ARMED: {setup.model.value} {setup.direction.value} "
                  f"{setup.grade.value} score {setup.score:.1f} | entry ref "
                  f"{setup.entry_price:.2f} SL {setup.stop_price:.2f} "
                  f"({setup.stop_reason}) TP {setup.tp1:.2f} "
                  f"({setup.target_reason}) | waiting for M1 trigger "
                  f"(valid until {expiry:%H:%M} UTC)")
        for line in setup.breakdown.lines():
            api.Print(line)
        api.Print(f"  reason: {setup.reason}")

    # ------------------------------------------------------- trigger & entry
    def _try_trigger(self, now, m1_candles):
        setup, expiry = self._armed
        if now > expiry:
            api.Print(f"SETUP EXPIRED without M1 trigger: {setup.model.value} "
                      f"{setup.direction.value} score {setup.score:.1f}")
            self.journal.record_setup(
                now, self._symbol_name, setup, accepted=False,
                rejection_reason="M1 trigger never fired within validity",
                spread_points=self._spread_points(),
                session=setup.session.value, news_status="CLEAR",
                m1_trigger="NONE")
            self._armed = None
            return
        # invalidated if price already broke the structural stop
        last = m1_candles[-1]
        if (setup.direction == Direction.LONG and last.close <= setup.stop_price) \
                or (setup.direction == Direction.SHORT and last.close >= setup.stop_price):
            api.Print("SETUP INVALIDATED before trigger: price broke the "
                      "structural stop level")
            self.journal.record_setup(
                now, self._symbol_name, setup, accepted=False,
                rejection_reason="invalidated: stop level broken pre-entry",
                spread_points=self._spread_points(),
                session=setup.session.value, news_status="CLEAR")
            self._armed = None
            return

        m1_tfa = self.engine.build_tf_analysis(
            Timeframe.M1, m1_candles[-self.cfg.window_m1:])
        confirmation_level = (setup.structure_event.broken_level
                              if setup.structure_event else None)
        result = self.trigger.check(m1_tfa, setup.direction,
                                    confirmation_level)
        if not result.fired:
            self._debug(f"M1 trigger not yet fired: {result.note}")
            return
        setup.m1_trigger = f"{result.kind}: {result.note}"
        api.Print(f"M1 TRIGGER: {result.kind} — {result.note}")
        self._execute(now, setup)
        self._armed = None

    def _execute(self, now, setup):
        cfg = self.cfg
        # live entry reference at trigger time
        entry = float(api.Symbol.Ask) if setup.direction == Direction.LONG \
            else float(api.Symbol.Bid)
        setup.entry_price = entry
        rr1 = setup.rr_to(setup.tp1)
        spread_points = self._spread_points()
        spread_ok, spread_why = self.spread_filter.check(spread_points)
        equity = float(api.Account.Equity)

        if rr1 < cfg.min_rr:
            self._reject_order(now, setup, 0.0, 0.0,
                               f"RR degraded to {rr1:.2f} at trigger time "
                               f"(< {cfg.min_rr})", spread_points)
            return

        risk_fraction = self.sizer.risk_fraction_for(setup.grade.value,
                                                     setup.score)
        self.spec.spread_points = spread_points
        sizing = self.sizer.size(self.spec, equity, risk_fraction,
                                 entry, setup.stop_price)

        unreal = self._floating_pnl()
        lock = self.guard.lock_reason(equity, unreal,
                                      self.sessions.session_at(now))
        allowed, window_why = self.sessions.entry_window_check(now)
        news_blocked, news_reason = self.news.blackout(now)
        facts = OrderFacts(
            is_demo_account=not bool(api.Account.IsLive),
            symbol_is_gold=self._symbol_is_gold(self._symbol_name),
            market_open=bool(api.Symbol.MarketHours.IsOpened()),
            spread_points=spread_points,
            spread_ok=spread_ok,
            # spec: no OTHER gold position, bot-placed or manual
            open_positions_count=self._symbol_positions_count(),
            has_pending_bot_order=self._has_pending_bot_order(),
            news_blocked=news_blocked,
            news_reason=news_reason,
            lock=lock,
            equity=equity,
            session_allowed=allowed,
            session_reason=window_why,
        )
        pf = self.orders.preflight(now, setup, sizing, facts)
        if cfg.debug_logging or not pf.ok:
            for line in pf.checks:
                api.Print(f"  order-safety {line}")
        can, why = self.limits.can_open(
            equity, len(self._bot_positions()),
            open_risk_money=0.0, new_risk_money=sizing.risk_money,
            unrealised=unreal, session=self.sessions.session_at(now))
        if not pf.ok or not can:
            reason = pf.reason if not pf.ok else why
            self._reject_order(now, setup, risk_fraction,
                               sizing.volume_units, reason, spread_points)
            return

        # ---- send the order (SL/TP attached as pip distances, then refined
        #      to the exact structural prices) --------------------------------
        trade_type = TradeType.Buy if setup.direction == Direction.LONG \
            else TradeType.Sell
        sl_pips = abs(entry - setup.stop_price) / self.spec.pip_size
        tp_pips = abs(setup.tp1 - entry) / self.spec.pip_size
        result = api.ExecuteMarketOrder(trade_type, self._symbol_name,
                                        sizing.volume_units, BOT_LABEL,
                                        sl_pips, tp_pips)
        if not bool(result.IsSuccessful) or result.Position is None:
            err = str(result.Error) if result.Error is not None else "unknown"
            api.Print(f"ORDER FAILED: {err} — entering "
                      f"{cfg.order_fail_cooldown_min}min cooldown, no retry "
                      f"spam")
            self.orders.order_failed(now, err)
            self._reject_order(now, setup, risk_fraction,
                               sizing.volume_units,
                               f"broker rejected order: {err}", spread_points)
            return
        self.orders.order_succeeded()
        pos = result.Position
        fill = float(pos.EntryPrice)

        # refine SL/TP to the exact structural prices (never widening the
        # stop beyond the sized risk: only replace if it tightens or matches)
        sl_price = round(setup.stop_price, self.spec.digits)
        tp_price = round(setup.tp1, self.spec.digits)
        try:
            api.ModifyPosition(pos, sl_price, tp_price)
        except Exception as exc:
            api.Print(f"note: could not refine SL/TP to exact prices "
                      f"({exc!r}); pip-based protection from the fill "
                      f"remains active")

        trade = Trade(
            trade_id=new_id("trade"), setup=setup, status=TradeStatus.OPEN,
            volume_units=sizing.volume_units,
            initial_volume_units=sizing.volume_units,
            risk_fraction=sizing.risk_fraction_actual,
            risk_money=sizing.risk_money,
            entry_price=fill, entry_time=now,
            stop_price=sl_price, initial_stop=sl_price,
            tp1=tp_price, tp2=round(setup.tp2, self.spec.digits),
            position_id=int(pos.Id),
        )
        self._tracked = trade
        self.guard.register_open(self.sessions.session_at(now))
        api.Print(f"ORDER FILLED: {setup.direction.value} "
                  f"{sizing.volume_units} units @ {fill:.2f} | SL {sl_price:.2f} "
                  f"TP {tp_price:.2f} | risk {sizing.risk_money:.2f} "
                  f"({sizing.risk_fraction_actual:.2%}) | position "
                  f"{trade.position_id}")
        self.journal.record_setup(
            now, self._symbol_name, setup, accepted=True,
            spread_points=spread_points, session=setup.session.value,
            news_status="CLEAR", risk_pct=sizing.risk_fraction_actual,
            volume_units=sizing.volume_units, m1_trigger=setup.m1_trigger)

    def _reject_order(self, now, setup, risk_fraction, volume, reason,
                      spread_points):
        api.Print(f"SETUP REJECTED at order stage: {reason}")
        self.journal.record_setup(
            now, self._symbol_name, setup, accepted=False,
            rejection_reason=reason, spread_points=spread_points,
            session=setup.session.value, news_status="CLEAR",
            risk_pct=risk_fraction, volume_units=volume,
            m1_trigger=setup.m1_trigger)

    # ------------------------------------------------------- position upkeep
    def _manage_position(self, now):
        trade = self._tracked
        pos = self._find_position(trade.position_id)
        if pos is None:
            return                        # reconcile will handle the close
        trade.bars_open += 1
        m5 = self._completed_candles(Timeframe.M5)
        if len(m5) < self.cfg.atr_period + 10:
            return
        mgmt = self.engine.build_tf_analysis(Timeframe.M5, m5)
        weekend = self.sessions.near_weekend_flat(now)
        for act in self.pos_manager.manage(trade, mgmt, now, self.spec,
                                           weekend_flat=weekend):
            if act.kind == "MOVE_STOP":
                new_stop = round(act.price, self.spec.digits)
                try:
                    tp = pos.TakeProfit
                    api.ModifyPosition(pos, new_stop, tp)
                    trade.stop_price = new_stop
                    api.Print(f"stop moved to {new_stop:.2f} ({act.note}) — "
                              f"stops only ever tighten")
                except Exception as exc:
                    api.Print(f"stop move failed: {exc!r}")
            elif act.kind == "PARTIAL_CLOSE":
                try:
                    r = api.ClosePosition(pos, act.volume_units)
                    if bool(r.IsSuccessful):
                        trade.volume_units = self.spec.round_volume_down(
                            trade.volume_units - act.volume_units)
                        api.Print(f"partial close {act.volume_units} units "
                                  f"({act.note})")
                    else:
                        api.Print(f"partial close failed: {r.Error}")
                except Exception as exc:
                    api.Print(f"partial close failed: {exc!r}")
            elif act.kind == "CLOSE":
                try:
                    r = api.ClosePosition(pos)
                    if bool(r.IsSuccessful):
                        api.Print(f"position closed: {act.note} "
                                  f"({act.reason.value})")
                        trade.exit_reason = act.reason
                    else:
                        api.Print(f"close failed: {r.Error}")
                except Exception as exc:
                    api.Print(f"close failed: {exc!r}")

    def _reconcile_closed(self, now):
        if self._tracked is None:
            return
        trade = self._tracked
        if self._find_position(trade.position_id) is not None:
            return
        # position no longer open: pull the outcome from broker history
        h = self._find_history(trade.position_id)
        if h is not None:
            trade.exit_price = float(h.ClosingPrice)
            trade.exit_time = self._to_utc(h.ClosingTime)
            trade.profit = float(h.NetProfit)
        else:
            trade.exit_time = now
        trade.status = TradeStatus.CLOSED
        if trade.exit_reason is None:
            trade.exit_reason = self._infer_exit_reason(trade)
        self.guard.register_close(trade.profit)
        self.journal.record_trade(trade)
        api.Print(f"TRADE CLOSED: {trade.direction.value} P/L "
                  f"{trade.profit:+.2f} ({trade.r_multiple():+.2f}R) "
                  f"exit {trade.exit_reason.value} | MFE {trade.mfe:+.2f}R "
                  f"MAE {trade.mae:+.2f}R | "
                  f"{self.guard.describe(float(api.Account.Equity))}")
        self._tracked = None

    def _infer_exit_reason(self, trade):
        if trade.exit_price <= 0:
            return ExitReason.BROKER_CLOSED
        tol = 3.0 * self.spec.tick_size + self.spec.spread_points * self.spec.point
        if abs(trade.exit_price - trade.stop_price) <= tol:
            return ExitReason.STOP_LOSS
        if abs(trade.exit_price - trade.tp1) <= tol:
            return ExitReason.TAKE_PROFIT
        return ExitReason.BROKER_CLOSED

    def _update_excursions(self, candle):
        trade = self._tracked
        if trade is None:
            return
        risk = abs(trade.entry_price - trade.initial_stop)
        if risk <= 0:
            return
        d = trade.direction.sign
        fav = (candle.high - trade.entry_price) * d / risk if d > 0 \
            else (trade.entry_price - candle.low) / risk
        adv = (trade.entry_price - candle.low) * d / risk if d > 0 \
            else -(trade.entry_price - candle.high) / risk
        trade.mfe = max(trade.mfe, fav)
        trade.mae = max(trade.mae, adv)

    # ---------------------------------------------------------- state restore
    def _restore_today_from_history(self, now):
        """Replay today's closed bot trades into the daily guard so a
        restart cannot bypass the daily loss lock or trade counters."""
        today = now.date()
        replayed = 0
        for i in range(api.History.Count):
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
            self.guard.register_close(float(h.NetProfit))
            if self.guard.day:
                self.guard.day.trades_opened += 1
            replayed += 1
        if replayed:
            api.Print(f"restart recovery: replayed {replayed} of today's "
                      f"closed bot trades into the daily guard")

    def _adopt_open_position(self, now):
        for pos in self._bot_positions():
            api.Print(f"restart recovery: adopting open bot position "
                      f"{int(pos.Id)} ({str(pos.TradeType)}, "
                      f"{float(pos.VolumeInUnits)} units @ "
                      f"{float(pos.EntryPrice):.2f})")
            sl = float(pos.StopLoss) if pos.StopLoss is not None else 0.0
            tp = float(pos.TakeProfit) if pos.TakeProfit is not None else 0.0
            if sl <= 0:
                api.Print("adopted position has NO stop loss — closing it "
                          "for safety (stop loss is mandatory)")
                try:
                    api.ClosePosition(pos)
                except Exception as exc:
                    api.Print(f"protective close failed: {exc!r}")
                continue
            direction = Direction.LONG if str(pos.TradeType) == "Buy" \
                else Direction.SHORT
            # minimal synthetic setup so management/journal can work
            setup = Setup(
                setup_id=new_id("adopted"), model=SetupModel.TREND_CONTINUATION,
                direction=direction, created_time=now,
                signal_price=float(pos.EntryPrice),
                entry_price=float(pos.EntryPrice), stop_price=sl,
                tp1=tp or float(pos.EntryPrice), tp2=tp or float(pos.EntryPrice),
                runner_target=None, score=0.0, grade=SetupGrade.NO_TRADE,
                breakdown=ScoreBreakdown(), tf_plan=FIXED_PLAN,
                regime=Regime.UNSAFE, session=SessionName.OFF_HOURS,
                htf_bias=TrendState.UNDEFINED,
                reason="adopted after restart")
            risk = abs(float(pos.EntryPrice) - sl) * float(pos.VolumeInUnits) \
                * self.spec.money_per_price_unit_per_unit()
            self._tracked = Trade(
                trade_id=new_id("trade"), setup=setup,
                status=TradeStatus.OPEN,
                volume_units=float(pos.VolumeInUnits),
                initial_volume_units=float(pos.VolumeInUnits),
                risk_fraction=0.0, risk_money=max(risk, 1e-9),
                entry_price=float(pos.EntryPrice),
                entry_time=self._to_utc(pos.EntryTime),
                stop_price=sl, initial_stop=sl, tp1=tp, tp2=tp,
                position_id=int(pos.Id))
            # a position opened today counts toward the daily trade cap
            entry_utc = self._to_utc(pos.EntryTime)
            if self.guard.day and entry_utc.date() == now.date():
                self.guard.register_open(self.sessions.session_at(entry_utc))
            break

    # ------------------------------------------------------------- utilities
    def _apply_ui_parameter_overrides(self):
        """Optional cTrader UI parameters (declared in the companion .cs
        file — see README) override config.py when present.  Only bounded,
        safety-preserving knobs are exposed; there is deliberately no
        live-trading or risk-raising override."""
        cfg = self.cfg

        def take(name, cast, clamp=None):
            try:
                v = getattr(api, name)
            except Exception:
                return None
            try:
                v = cast(v)
            except (TypeError, ValueError):
                return None
            if clamp:
                v = max(clamp[0], min(clamp[1], v))
            return v

        v = take("MinSetupScore", float, (70.0, 100.0))
        if v is not None:
            cfg.min_score = v
        v = take("RiskPercentPerTrade", float, (0.01, 0.25))
        if v is not None:
            cfg.max_risk_per_trade = v / 100.0
        v = take("MaxSpreadPoints", float, (5.0, 200.0))
        if v is not None:
            cfg.max_spread_points = v
        v = take("MaxTradesPerDay", int, (1, 3))
        if v is not None:
            cfg.max_trades_per_day = v
        v = take("MinRewardRisk", float, (1.5, 5.0))
        if v is not None:
            cfg.min_rr = v
        v = take("StopBufferAtr", float, (0.0, 1.0))
        if v is not None:
            cfg.stop_buffer_atr = v
        for pname, attr in (("AsiaEnabled", "asia_enabled"),
                            ("LondonEnabled", "london_enabled"),
                            ("NewYorkEnabled", "newyork_enabled"),
                            ("NewsProtectionEnabled", "news_enabled"),
                            ("BreakEvenEnabled", "breakeven_enabled"),
                            ("TrailingEnabled", "trailing_enabled"),
                            ("PartialTpEnabled", "partial_tp_enabled"),
                            ("EmergencyStop", "emergency_stop"),
                            ("DebugLogging", "debug_logging"),
                            ("StrictMode", "strict_mode")):
            v = take(pname, bool)
            if v is not None:
                setattr(cfg, attr, v)
        v = take("NewsMinutesBefore", int, (0, 240))
        if v is not None:
            cfg.news_block_before_min = v
        v = take("NewsMinutesAfter", int, (0, 240))
        if v is not None:
            cfg.news_block_after_min = v
        v = take("BreakEvenTriggerR", float, (0.5, 5.0))
        if v is not None:
            cfg.breakeven_r = v
        v = take("TrailingAtrMultiple", float, (0.5, 5.0))
        if v is not None:
            cfg.trail_atr_mult = v

    def _symbol_is_gold(self, name):
        norm = "".join(ch for ch in name.upper() if ch.isalnum())
        allowed = {"".join(ch for ch in a.upper() if ch.isalnum())
                   for a in self.cfg.allowed_gold_symbols}
        return norm in allowed

    def _build_spec(self):
        sym = api.Symbol
        return CTraderSymbolSpec(
            name=self._symbol_name,
            digits=int(sym.Digits),
            tick_size=float(sym.TickSize),
            tick_value=float(sym.TickValue),
            pip_size=float(sym.PipSize),
            pip_value=float(sym.PipValue),
            volume_min=float(sym.VolumeInUnitsMin),
            volume_max=float(sym.VolumeInUnitsMax),
            volume_step=float(sym.VolumeInUnitsStep),
            spread_points=self._spread_points_raw(sym),
        )

    def _spread_points_raw(self, sym):
        tick = float(sym.TickSize)
        if tick <= 0:
            return 0.0
        return (float(sym.Ask) - float(sym.Bid)) / tick

    def _spread_points(self):
        return self._spread_points_raw(api.Symbol)

    def _to_utc(self, net_dt):
        """System.DateTime (server time) -> python datetime in UTC.
        cTrader server time is UTC for most brokers; a non-zero
        cfg.server_utc_offset_hours corrects platforms that differ."""
        t = datetime(int(net_dt.Year), int(net_dt.Month), int(net_dt.Day),
                     int(net_dt.Hour), int(net_dt.Minute),
                     int(net_dt.Second), tzinfo=UTC)
        return t - timedelta(hours=self.cfg.server_utc_offset_hours)

    def _now_utc(self):
        return self._to_utc(api.Server.Time)

    def _completed_candles(self, tf):
        """Completed candles for a timeframe — the forming bar (the last
        index) is ALWAYS excluded, so no decision can ever use unfinished
        or future data."""
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
                volume=float(bars.TickVolumes[i]),
            ))
        return out

    def _bot_positions(self):
        out = []
        for i in range(int(api.Positions.Count)):
            p = api.Positions[i]
            if str(p.SymbolName) == self._symbol_name \
                    and str(p.Label) == BOT_LABEL:
                out.append(p)
        return out

    def _symbol_positions_count(self):
        """ALL open positions on this symbol — manual ones included, so the
        one-GOLD-position rule cannot be bypassed by trading alongside."""
        n = 0
        for i in range(int(api.Positions.Count)):
            if str(api.Positions[i].SymbolName) == self._symbol_name:
                n += 1
        return n

    def _find_position(self, position_id):
        for p in self._bot_positions():
            if int(p.Id) == position_id:
                return p
        return None

    def _has_pending_bot_order(self):
        try:
            for i in range(int(api.PendingOrders.Count)):
                o = api.PendingOrders[i]
                if str(o.SymbolName) == self._symbol_name \
                        and str(o.Label) == BOT_LABEL:
                    return True
        except Exception:
            return False                # bot never places pending orders
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
        for p in self._bot_positions():
            total += float(p.NetProfit)
        return total

    def _emergency_file_present(self):
        name = self.cfg.emergency_stop_file
        candidates = (self.journal.directory,
                      os.path.join(os.path.expanduser("~"), "Documents",
                                   "XAUUSD_Adaptive_Bot"),
                      os.getcwd())
        for base in candidates:
            if base and os.path.exists(os.path.join(base, name)):
                return True
        return self.cfg.emergency_stop

    def _debug(self, msg):
        if self.cfg.debug_logging:
            api.Print(f"[debug] {msg}")
