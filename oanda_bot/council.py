"""
TradeCouncil -- where all the assistants meet and a trade either earns its
confirmations or dies.  NOTHING is ever forced: if the analysts disagree,
the answer is "no trade".

Voting assistants (strategy analysts, weighted by learned track record):
    trend_rider, range_fader, breakout_hunter, momentum_surfer

Non-voting assistants (each holds an absolute veto):
    MarketRegimeAnalyst  -- classifies the market, discounts off-regime votes
    NewsSentry           -- blocks trades around real-time high-impact news
    SessionAnalyst       -- blocks dead/illiquid hours and Friday-late entries
    SpreadWatcher        -- blocks entries when the spread is too expensive
    PerformanceCoach     -- vetoes strategy combos with a proven losing record

A trade is approved only when:
    * >= min_confirmations strategies agree on the SAME direction
    * their weighted vote score clears the (adaptive) threshold
    * no assistant vetoes
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .config import CouncilConfig
from .indicators import Candle, atr
from .learning import PerformanceCoach
from .news import NewsSentry
from .regime import MarketRegimeAnalyst, RegimeView
from .strategies import ALL_STRATEGIES, Signal, Strategy

log = logging.getLogger("council")


@dataclass
class Decision:
    approved: bool
    instrument: str
    direction: int = 0                  # +1 long / -1 short
    score: float = 0.0
    threshold: float = 0.0
    confirmations: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    regime: str = ""
    atr: Optional[float] = None
    vetoes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def explain(self) -> str:
        head = (f"{self.instrument}: "
                f"{'APPROVED ' + ('LONG' if self.direction > 0 else 'SHORT') if self.approved else 'no trade'}"
                f" (score {self.score:.2f} / threshold {self.threshold:.2f},"
                f" regime {self.regime})")
        parts = [head]
        for s in self.signals:
            parts.append(f"  vote {s.strategy}: "
                         f"{'LONG' if s.direction > 0 else 'SHORT'} "
                         f"conf {s.confidence:.2f} -- {s.reason}")
        for v in self.vetoes:
            parts.append(f"  VETO: {v}")
        for n in self.notes:
            parts.append(f"  note: {n}")
        return "\n".join(parts)


class SessionAnalyst:
    """Keeps the bot out of dead hours, rollover spread spikes and
    late-Friday positions that would gap over the weekend."""

    def __init__(self, cfg: CouncilConfig):
        self.cfg = cfg

    def check(self, now_ts: float) -> Optional[str]:
        now = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        if now.weekday() == 5 or (now.weekday() == 6 and now.hour < 21):
            return "market closed (weekend)"
        if now.weekday() == 4 and now.hour >= self.cfg.block_friday_after_utc:
            return f"late Friday ({now.hour}:00 UTC) -- weekend gap risk"
        if not (self.cfg.session_start_utc <= now.hour < self.cfg.session_end_utc):
            return (f"outside trading session "
                    f"({self.cfg.session_start_utc}:00-{self.cfg.session_end_utc}:00 UTC)")
        start_s, end_s = self.cfg.rollover_block
        hm = now.strftime("%H:%M")
        if start_s <= hm <= end_s:
            return "daily rollover window (spread spike)"
        return None


class SpreadWatcher:
    """Refuses entries when the spread eats too much of the expected move."""

    def __init__(self, cfg: CouncilConfig):
        self.cfg = cfg

    def check(self, spread: Optional[float], cur_atr: Optional[float]) -> Optional[str]:
        if spread is None or cur_atr is None or cur_atr <= 0:
            return "no live price / ATR available"
        frac = spread / cur_atr
        if frac > self.cfg.max_spread_atr_frac:
            return (f"spread {spread:.5f} is {frac:.0%} of ATR "
                    f"(max {self.cfg.max_spread_atr_frac:.0%})")
        return None


class TradeCouncil:
    def __init__(self, cfg: CouncilConfig, coach: PerformanceCoach,
                 news: NewsSentry,
                 strategies: Optional[list[Strategy]] = None):
        self.cfg = cfg
        self.coach = coach
        self.news = news
        self.session = SessionAnalyst(cfg)
        self.spread_watch = SpreadWatcher(cfg)
        self.regime_analyst = MarketRegimeAnalyst()
        self.strategies = strategies if strategies is not None else ALL_STRATEGIES

    def evaluate(self, instrument: str, m5: list[Candle], h1: list[Candle],
                 spread: Optional[float], now_ts: float,
                 check_news: bool = True) -> Decision:
        d = Decision(approved=False, instrument=instrument)

        regime_view: RegimeView = self.regime_analyst.classify(h1)
        d.regime = regime_view.regime
        d.notes.append(f"regime analyst: {regime_view.regime} ({regime_view.detail})")

        atr_series = atr(m5, 14)
        d.atr = atr_series[-1] if atr_series else None

        # hard vetoes first -- no point voting if we can't trade
        session_veto = self.session.check(now_ts)
        if session_veto:
            d.vetoes.append(f"session analyst: {session_veto}")
        spread_veto = self.spread_watch.check(spread, d.atr)
        if spread_veto:
            d.vetoes.append(f"spread watcher: {spread_veto}")
        if check_news:
            verdict = self.news.check(instrument, now_ts)
            if verdict.blocked:
                d.vetoes.append(f"news sentry: {verdict.reason}")

        # collect strategy votes
        weights = self.coach.weights()
        long_score = short_score = 0.0
        for strat in self.strategies:
            sig = strat.evaluate(m5, h1)
            if sig is None:
                continue
            d.signals.append(sig)
            w = weights.get(sig.strategy, 1.0)
            regime_mult = (1.0 if regime_view.regime in sig.preferred_regimes
                           else self.cfg.out_of_regime_penalty)
            contribution = w * sig.confidence * regime_mult
            if sig.direction > 0:
                long_score += contribution
            else:
                short_score += contribution

        d.threshold = self.coach.score_threshold(self.cfg.base_score_threshold)
        direction = 1 if long_score > short_score else -1
        d.score = max(long_score, short_score) - min(long_score, short_score)
        agreeing = [s for s in d.signals if s.direction == direction]
        opposing = [s for s in d.signals if s.direction != direction]
        d.confirmations = [s.strategy for s in agreeing]

        if not agreeing:
            d.notes.append("no strategy proposed a trade")
            return d
        if opposing:
            d.notes.append(
                "conflicting votes from "
                + ", ".join(s.strategy for s in opposing))
        if len(agreeing) < self.cfg.min_confirmations:
            d.notes.append(
                f"only {len(agreeing)} confirmation(s); "
                f"{self.cfg.min_confirmations} required")
            return d

        # PerformanceCoach: has this exact combo earned trust or lost it?
        combo = self.coach.combo_verdict(d.confirmations, regime_view.regime)
        d.notes.append(f"performance coach: {combo.reason}")
        if combo.veto:
            d.vetoes.append(f"performance coach: {combo.reason}")
        effective_threshold = d.threshold
        if combo.boost:
            effective_threshold *= self.coach.cfg.combo_boost_factor
            d.notes.append(
                f"threshold relaxed to {effective_threshold:.2f} for proven combo")

        if d.score < effective_threshold:
            d.notes.append(
                f"score {d.score:.2f} below threshold {effective_threshold:.2f}")
            return d
        if d.vetoes:
            return d

        d.approved = True
        d.direction = direction
        return d
