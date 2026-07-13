"""Orchestrator — the conductor that makes the agents cooperate.

Each tick:
  1. Scout discovers fresh low-cap candidates
  2. Rug Checker vets every candidate (hard veto below threshold)
  3. News agent scores narrative fit; Whale Tracker scores smart money
  4. Every strategy evaluates every vetted candidate -> signals
  5. Risk engine sizes the best signals and opens positions
  6. Risk engine manages every open position (rug exit, stops, TP ladder)
"""
from __future__ import annotations

from ..agents.news_agent import NewsAgent
from ..agents.rug_checker import RugChecker
from ..agents.scout import Scout
from ..agents.whale_tracker import WhaleTracker
from ..config import StrategyParams
from ..models import Signal, TokenSnapshot, TradeRecord
from ..strategies.base import Context, Strategy
from ..strategies.copy_trade import CopyTrade
from ..strategies.dip_buyer import DipBuyer
from ..strategies.graduation_sniper import GraduationSniper
from ..strategies.momentum import MomentumBreakout
from .paper_broker import PaperBroker
from .portfolio import Portfolio
from .risk import RiskEngine


class Orchestrator:
    def __init__(self, params: StrategyParams, feed, portfolio: Portfolio | None = None,
                 verbose: bool = False):
        self.params = params
        self.feed = feed
        self.verbose = verbose
        self.scout = Scout(params, feed)
        self.rug_checker = RugChecker(params)
        self.news = NewsAgent(params, feed)
        self.whales = WhaleTracker(params)
        self.strategies: list[Strategy] = [
            GraduationSniper(params),
            MomentumBreakout(params),
            CopyTrade(params),
            DipBuyer(params),
        ]
        self.broker = PaperBroker()
        self.risk = RiskEngine(params.risk, self.broker)
        self.portfolio = portfolio or Portfolio(params.risk.starting_bankroll_usd)
        self.mature_watch: dict[str, TokenSnapshot] = {}  # dip-buyer universe

    # ------------------------------------------------------------------
    def tick(self, now: float) -> list[TradeRecord]:
        """Full cycle: discovery + entries, then position management.
        Live mode calls the two halves on separate clocks instead — see
        discover_and_enter / manage_positions."""
        self.news.update()
        self.discover_and_enter(now)
        return self.manage_positions(now)

    # ------------------------------------------------------------------
    def discover_and_enter(self, now: float) -> None:
        # --- discovery + evaluation ---
        candidates = self.scout.discover()
        # keep a rolling watchlist of survivors for the dip buyer
        for t in list(self.mature_watch.values()):
            fresh = self.feed.snapshot(t.address)
            if fresh is None or fresh.market_cap < self.params.dip_min_mcap * 0.2:
                self.mature_watch.pop(t.address, None)
            else:
                self.mature_watch[t.address] = fresh
        for t in candidates:
            if t.market_cap >= self.params.dip_min_mcap * 0.5:
                self.mature_watch[t.address] = t
        if len(self.mature_watch) > 300:
            for a in list(self.mature_watch)[:100]:
                self.mature_watch.pop(a, None)

        universe = {t.address: t for t in candidates}
        universe.update({a: t for a, t in self.mature_watch.items() if a not in universe})

        signals: list[tuple[Signal, TokenSnapshot]] = []
        for t in universe.values():
            if t.address in self.portfolio.positions:
                continue
            safety = self.rug_checker.check(t)
            ctx = Context(
                safety=safety,
                narrative_delta=self.news.narrative_score(t),
                smart_money=self.whales.smart_money_score(t),
            )
            for strat in self.strategies:
                sig = strat.evaluate(t, ctx)
                if sig:
                    signals.append((sig, t))

        # best-confidence-first, one position per token
        signals.sort(key=lambda st: st[0].confidence, reverse=True)
        opened_addrs: set[str] = set()
        for sig, t in signals:
            if sig.address in opened_addrs:
                continue
            pos = self.risk.size_and_open(sig, t, self.portfolio, now)
            if pos:
                opened_addrs.add(sig.address)
                if self.verbose:
                    print(f"  OPEN  [{sig.strategy:>17}] {t.symbol:<8} "
                          f"${pos.size_usd:>7.2f} @ mcap ${t.market_cap:,.0f} — {sig.reason}")

    # ------------------------------------------------------------------
    def manage_positions(self, now: float) -> list[TradeRecord]:
        """The fast path: re-check every OPEN position (price, rug score,
        spike/stop/ladder exits). Memecoins move in seconds — live mode
        runs this on a much faster clock than discovery."""
        closed: list[TradeRecord] = []
        marks: dict[str, TokenSnapshot] = {}
        for addr, pos in list(self.portfolio.positions.items()):
            snap = self.feed.snapshot(addr)
            if snap is None:
                continue
            marks[addr] = snap
            report = self.rug_checker.check(snap)
            rug_alert = report.score < 25 or snap.liquidity < 500.0
            rec = self.risk.manage(pos, snap, self.portfolio, now, rug_alert)
            if rec:
                closed.append(rec)
                # feed outcome back to the whale tracker: if smart wallets were
                # in this token, update their measured track record
                if snap.smart_wallet_buys_30m > 0 or rec.multiple > 1:
                    self.whales.record_wallet_trade(
                        f"cohort:{rec.strategy}", rec.multiple, rec.pnl_usd)
                # feed outcome back to deployer reputation: creators earn
                # trusted/blacklisted status from what their coins DO
                self.rug_checker.record_outcome(
                    snap.deployer,
                    rugged=(rec.exit_reason == "rug_exit" or rec.multiple < 0.35),
                    ran=(rec.multiple >= 2.0),
                )
                if self.verbose:
                    print(f"  CLOSE [{rec.exit_reason:>17}] {rec.symbol:<8} "
                          f"{rec.multiple:>5.2f}x  pnl ${rec.pnl_usd:>+8.2f}  "
                          f"held {rec.hold_minutes:.0f}m")

        self.portfolio.mark(marks)
        return closed

    # ------------------------------------------------------------------
    def liquidate_all(self, now: float, reason: str) -> list[TradeRecord]:
        """Market-dump every open position (day guard / end of day)."""
        closed: list[TradeRecord] = []
        for pos in list(self.portfolio.positions.values()):
            snap = self.feed.snapshot(pos.address)
            if snap is not None:
                closed.append(self.risk._close_all(pos, snap, self.portfolio, now, reason))
            else:
                self.portfolio.positions.pop(pos.address, None)
        return closed
