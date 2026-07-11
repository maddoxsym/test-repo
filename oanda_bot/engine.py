"""
Live trading engine: wires every assistant together and runs the loop
against the OANDA v20 API.

Each cycle:
    1. kill-switch check (create a file named KILL_SWITCH to halt safely)
    2. reconcile: detect trades OANDA closed (SL/TP hit) -> journal the
       outcome -> PerformanceCoach learns from it
    3. manage open trades (move stop to breakeven at +1R)
    4. daily gates (loss limit / profit target / trade caps)
    5. for each instrument with no open position and a NEW complete M5
       candle: convene the TradeCouncil; if it approves and the
       RiskManager sizes it, place a market order with SL/TP attached

Paper mode (--paper) runs the identical pipeline but simulates fills
locally instead of sending orders.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from .config import Config
from .council import TradeCouncil
from .indicators import Candle
from .journal import Journal, TradeRecord
from .learning import PerformanceCoach
from .news import NewsSentry
from .oanda import OandaClient, OandaError, PriceQuote
from .risk import RiskManager

log = logging.getLogger("engine")


class TradingEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = OandaClient(cfg.oanda)
        self.journal = Journal(cfg.engine.db_path)
        self.coach = PerformanceCoach(cfg.learning, self.journal)
        self.news = NewsSentry(cfg.news)
        self.council = TradeCouncil(cfg.council, self.coach, self.news)
        self.risk = RiskManager(cfg.risk, self.journal)
        self._last_decided_candle: dict[str, float] = {}
        self._stood_down_reason: Optional[str] = None

    # ------------------------------------------------------------ safety

    def preflight(self) -> None:
        problems = self.cfg.oanda.validate()
        if problems:
            raise SystemExit("cannot start:\n  " + "\n  ".join(problems)
                             + "\nCopy .env.example to .env and fill it in.")
        if self.cfg.oanda.environment == "live" and not self.cfg.risk.allow_live:
            raise SystemExit(
                "OANDA_ENV=live but risk.allow_live is False.\n"
                "This bot refuses to trade real money until you have run it "
                "on a practice account, reviewed its journal, and explicitly "
                "set RiskConfig.allow_live = True in oanda_bot/config.py.")
        account = self.client.account_summary()
        specs = self.client.instrument_specs(list(self.cfg.engine.instruments))
        missing = [i for i in self.cfg.engine.instruments if i not in specs]
        if missing:
            raise SystemExit(f"instruments not available on this account: {missing}")
        log.info("connected to OANDA %s | account %s | balance %s %s | "
                 "open trades on broker: %s",
                 self.cfg.oanda.environment, self.cfg.oanda.account_id,
                 account["balance"], account.get("currency", "?"),
                 account.get("openTradeCount", "?"))
        if account.get("currency", "USD") != "USD":
            log.warning("account currency is %s, not USD -- risk %% sizing "
                        "will be approximate", account.get("currency"))
        if self.cfg.engine.paper:
            log.info("PAPER MODE: decisions and journal only, no orders sent")
        log.info("assistants online: 4 strategy analysts, regime analyst, "
                 "news sentry, session analyst, spread watcher, risk manager, "
                 "performance coach")
        log.info("\n%s", self.coach.report())

    def _kill_switch(self) -> bool:
        if os.path.exists(self.cfg.engine.kill_switch_file):
            log.warning("KILL SWITCH file present -- halting (no new trades)")
            return True
        return False

    # --------------------------------------------------------- reconcile

    def reconcile_closed(self, quotes: dict[str, PriceQuote]) -> None:
        """Find journal-open trades the broker has closed, learn from them."""
        open_records = self.journal.open_trades()
        if not open_records:
            return
        try:
            broker_open_ids = {t["id"] for t in self.client.open_trades()} \
                if not self.cfg.engine.paper else set()
        except OandaError as exc:
            log.warning("could not fetch open trades: %s", exc)
            return

        for rec in open_records:
            if rec.paper:
                self._reconcile_paper(rec, quotes)
                continue
            if rec.broker_trade_id in broker_open_ids:
                continue
            try:
                t = self.client.get_trade(rec.broker_trade_id)
            except OandaError as exc:
                log.warning("could not fetch trade %s: %s", rec.broker_trade_id, exc)
                continue
            if t.get("state") == "OPEN":
                continue
            pnl = float(t.get("realizedPL", 0.0))
            close_price = float(t.get("averageClosePrice", rec.entry_price))
            closed = self.journal.record_close(rec.id, close_price, pnl)
            if closed:
                log.info("trade closed: %s %s %+0.2f USD (%+.2fR)",
                         closed.instrument,
                         "LONG" if closed.direction > 0 else "SHORT",
                         pnl, closed.r_multiple or 0)
                self.coach.learn_from_close(
                    closed, self.cfg.council.base_score_threshold)

    def _reconcile_paper(self, rec: TradeRecord,
                         quotes: dict[str, PriceQuote]) -> None:
        q = quotes.get(rec.instrument)
        if q is None:
            return
        price = q.bid if rec.direction > 0 else q.ask
        hit_sl = (price <= rec.stop_price if rec.direction > 0
                  else price >= rec.stop_price)
        hit_tp = (price >= rec.tp_price if rec.direction > 0
                  else price <= rec.tp_price)
        if not (hit_sl or hit_tp):
            return
        exit_price = rec.stop_price if hit_sl else rec.tp_price
        pnl = (exit_price - rec.entry_price) * rec.direction * abs(rec.units)
        closed = self.journal.record_close(rec.id, exit_price, pnl)
        if closed:
            log.info("[paper] trade closed: %s %+0.2f USD (%+.2fR)",
                     rec.instrument, pnl, closed.r_multiple or 0)
            self.coach.learn_from_close(
                closed, self.cfg.council.base_score_threshold)

    # ------------------------------------------------------- management

    def manage_open(self, quotes: dict[str, PriceQuote]) -> None:
        for rec in self.journal.open_trades():
            q = quotes.get(rec.instrument)
            if q is None:
                continue
            price = q.bid if rec.direction > 0 else q.ask
            new_stop = self.risk.breakeven_stop(
                rec.direction, rec.entry_price, rec.stop_price, price)
            if new_stop is None:
                continue
            if rec.paper:
                with self.journal.conn:
                    self.journal.conn.execute(
                        "UPDATE trades SET stop_price=? WHERE id=?",
                        (new_stop, rec.id))
                log.info("[paper] %s stop moved to breakeven %.5f",
                         rec.instrument, new_stop)
                continue
            try:
                self.client.set_stop_loss(rec.broker_trade_id,
                                          rec.instrument, new_stop)
                with self.journal.conn:
                    self.journal.conn.execute(
                        "UPDATE trades SET stop_price=? WHERE id=?",
                        (new_stop, rec.id))
                log.info("%s stop moved to breakeven %.5f",
                         rec.instrument, new_stop)
            except OandaError as exc:
                log.warning("failed to move stop for %s: %s",
                            rec.broker_trade_id, exc)

    # ------------------------------------------------------------ entry

    def consider_entry(self, instrument: str, balance: float,
                       quotes: dict[str, PriceQuote], now: float) -> None:
        open_here = [t for t in self.journal.open_trades()
                     if t.instrument == instrument]
        if open_here:
            return

        m5 = self.client.candles(instrument, self.cfg.engine.decision_granularity,
                                 self.cfg.engine.candle_count)
        if not m5:
            return
        # decide at most once per completed candle
        if self._last_decided_candle.get(instrument) == m5[-1].time:
            return
        self._last_decided_candle[instrument] = m5[-1].time

        h1 = self.client.candles(instrument, self.cfg.engine.regime_granularity, 200)
        q = quotes.get(instrument)
        spread = q.spread if q else None
        if q and not q.tradeable:
            log.info("%s not tradeable right now", instrument)
            return

        decision = self.council.evaluate(instrument, m5, h1, spread, now)
        log.info("\n%s", decision.explain())
        if not decision.approved:
            return

        price = (q.ask if decision.direction > 0 else q.bid) if q else m5[-1].close
        spec = self.client.instrument_specs([instrument]).get(instrument)
        if spec is None:
            log.warning("no instrument spec for %s", instrument)
            return
        sized = self.risk.size(decision.direction, price, decision.atr or 0.0,
                               balance, spec)
        if sized is None:
            log.info("risk manager declined to size the trade")
            return

        rec = TradeRecord(
            instrument=instrument, direction=decision.direction,
            units=abs(sized.units), entry_price=price,
            stop_price=sized.stop_price, tp_price=sized.tp_price,
            risk_usd=sized.risk_usd, atr=decision.atr,
            regime=decision.regime, confirmations=decision.confirmations,
            council_score=decision.score, opened_at=now,
            paper=self.cfg.engine.paper,
        )

        if self.cfg.engine.paper:
            rec.broker_trade_id = f"paper-{int(now)}-{instrument}"
            self.journal.record_open(rec)
            log.info("[paper] OPEN %s %s %.2f units @ %.5f sl %.5f tp %.5f "
                     "risk %.2f USD",
                     instrument, "LONG" if decision.direction > 0 else "SHORT",
                     abs(sized.units), price, sized.stop_price, sized.tp_price,
                     sized.risk_usd)
            return

        try:
            result = self.client.market_order(
                instrument, sized.units, sized.stop_price, sized.tp_price)
        except OandaError as exc:
            log.error("order rejected: %s", exc)
            return
        if not result["trade_id"]:
            log.warning("order did not open a trade (cancelled?)")
            return
        rec.broker_trade_id = result["trade_id"]
        if result["fill_price"]:
            rec.entry_price = result["fill_price"]
        self.journal.record_open(rec)
        log.info("OPEN %s %s %.2f units @ %.5f sl %.5f tp %.5f risk %.2f USD "
                 "(trade %s)",
                 instrument, "LONG" if decision.direction > 0 else "SHORT",
                 abs(sized.units), rec.entry_price, sized.stop_price,
                 sized.tp_price, sized.risk_usd, rec.broker_trade_id)

    # ------------------------------------------------------------- loop

    def run(self) -> None:
        self.preflight()
        log.info("engine started: %s every %ss",
                 ", ".join(self.cfg.engine.instruments),
                 self.cfg.engine.poll_seconds)
        while True:
            try:
                if self._kill_switch():
                    break
                now = time.time()
                try:
                    quotes = self.client.pricing(list(self.cfg.engine.instruments))
                except OandaError as exc:
                    log.warning("pricing fetch failed: %s", exc)
                    quotes = {}

                self.reconcile_closed(quotes)
                self.manage_open(quotes)

                balance = self.client.balance()
                open_count = len(self.journal.open_trades())
                gate = self.risk.daily_gate(balance, now, open_count)
                if gate:
                    if gate != self._stood_down_reason:
                        log.info("standing down: %s", gate)
                        if "loss limit" in gate:
                            self.coach.on_daily_loss_limit(
                                self.cfg.council.base_score_threshold)
                        self._stood_down_reason = gate
                else:
                    self._stood_down_reason = None
                    for instrument in self.cfg.engine.instruments:
                        self.consider_entry(instrument, balance, quotes, now)

                time.sleep(self.cfg.engine.poll_seconds)
            except KeyboardInterrupt:
                log.info("interrupted -- shutting down (open trades keep "
                         "their SL/TP on OANDA's side)")
                break
            except OandaError as exc:
                log.error("OANDA error in main loop: %s -- retrying in 30s", exc)
                time.sleep(30)
            except Exception:
                log.exception("unexpected error -- retrying in 30s")
                time.sleep(30)
