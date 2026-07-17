"""
V4 risk engine and guards.

* V4Guard extends the proven DailyLossGuard with the V4 rails:
    - daily lock at 1.7% COMBINED (realised + floating) loss,
    - weekly 5% drawdown lock (both from the base class, configured here),
    - a COOLDOWN after 3 consecutive real losses (instead of locking the
      whole day): no new entries for loss_cooldown_hours, then the streak
      counter resets. The cooldown timestamp is persisted, so restarting
      cannot bypass it.
* AdaptiveRisk chooses the per-trade risk fraction from evidence tiers and
  applies only *reductions* (drawdown, streaks, spread, volatility,
  remaining daily/weekly headroom). Nothing can raise risk above 0.75%,
  and risk NEVER increases because the previous trade lost.
* order_preflight is the full pre-order safety checklist for V4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from adaptive_bot.core.models import LockReason, SessionName
from adaptive_bot.risk.daily_loss_guard import DailyLossGuard
from .strategy_space import Signal


class V4Guard(DailyLossGuard):

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cooldown_until: Optional[datetime] = None

    def register_close(self, profit: float,
                       now: Optional[datetime] = None) -> None:
        super().register_close(profit)
        if now is not None and profit < 0 and \
                self.consecutive_losses >= self.cfg.cooldown_after_losses:
            self.cooldown_until = now + timedelta(
                hours=self.cfg.loss_cooldown_hours)

    def lock_reason(self, equity: float, unrealised: float = 0.0,
                    session: Optional[SessionName] = None,
                    now: Optional[datetime] = None) -> LockReason:
        base = super().lock_reason(equity, unrealised, session)
        if base != LockReason.NONE:
            return base
        if now is not None and self.cooldown_until is not None:
            if now < self.cooldown_until:
                return LockReason.CONSECUTIVE_LOSSES
            # cooldown served: reset the streak, allow trading again
            self.cooldown_until = None
            self.consecutive_losses = 0
        return LockReason.NONE

    # -- persistence -----------------------------------------------------
    def snapshot(self) -> dict:
        d = {
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until.isoformat()
            if self.cooldown_until else "",
        }
        if self.day:
            d["day"] = {k: v for k, v in self.day.__dict__.items()}
        if self.week:
            d["week"] = {k: v for k, v in self.week.__dict__.items()}
        return d

    def restore(self, snap: dict, now: datetime) -> None:
        """Restore ONLY when the persisted period matches the current one,
        and never in a less restrictive direction than what the broker
        history already produced."""
        from adaptive_bot.risk.daily_loss_guard import DayState
        day = snap.get("day")
        if day and self.day and day.get("day") == self.day.day:
            merged = DayState(**{k: day[k] for k in day
                                 if k in DayState.__dataclass_fields__})
            # keep the WORSE (more restrictive) of persisted vs replayed
            self.day.realised = min(self.day.realised, merged.realised)
            self.day.trades_opened = max(self.day.trades_opened,
                                         merged.trades_opened)
            self.day.losses = max(self.day.losses, merged.losses)
            self.day.wins = max(self.day.wins, merged.wins)
            for k, v in merged.session_trades.items():
                self.day.session_trades[k] = max(
                    self.day.session_trades.get(k, 0), v)
        week = snap.get("week")
        if week and self.week and week.get("week") == self.week.week:
            self.week.realised = min(self.week.realised,
                                     float(week.get("realised", 0.0)))
            self.week.start_equity = float(week.get("start_equity",
                                                    self.week.start_equity))
            self.week.min_equity = min(self.week.min_equity,
                                       float(week.get("min_equity",
                                                      self.week.min_equity)))
        self.consecutive_losses = max(self.consecutive_losses,
                                      int(snap.get("consecutive_losses", 0)))
        cd = snap.get("cooldown_until", "")
        if cd:
            try:
                t = datetime.fromisoformat(cd)
                if t > now:
                    self.cooldown_until = t
            except ValueError:
                self.cooldown_until = None


class AdaptiveRisk:
    """Chooses the per-trade risk fraction. Reduction-only adjustments."""

    def __init__(self, cfg):
        self.cfg = cfg

    def tier(self, score: float, n: int) -> Tuple[float, float]:
        c = self.cfg
        if n >= c.tier_top_min_n and score >= c.tier_top_min_score:
            return c.risk_tier_top
        if n >= c.tier_strong_min_n and score >= c.tier_strong_min_score:
            return c.risk_tier_strong
        if n >= c.tier_moderate_min_n and score >= c.tier_moderate_min_score:
            return c.risk_tier_moderate
        return c.risk_tier_experimental

    def choose(self, score: float, n: int, *,
               equity: float, day_start_equity: float,
               daily_pl_combined: float, weekly_dd_frac: float,
               consecutive_losses: int, spread_points: float,
               atr_percentile: float, recent_strategy_r: float
               ) -> Tuple[float, List[str]]:
        """Returns (risk_fraction, notes). 0.0 means 'do not trade'."""
        c = self.cfg
        lo, hi = self.tier(score, n)
        risk = hi
        notes = [f"tier [{lo:.2%}..{hi:.2%}] from score {score:+.3f} n={n}"]

        def cut(mult: float, why: str):
            nonlocal risk
            new = risk * mult
            notes.append(f"reduce x{mult:.2f}: {why}")
            risk = new

        if consecutive_losses >= 1:
            cut(0.75 ** consecutive_losses,
                f"{consecutive_losses} consecutive losses")
        if weekly_dd_frac >= 0.025:
            cut(0.6, f"weekly drawdown {weekly_dd_frac:.1%} elevated")
        if spread_points > c.normal_spread_points:
            cut(0.7, f"spread {spread_points:.0f} pts above normal")
        if atr_percentile >= 0.90 or atr_percentile <= 0.05:
            cut(0.8, f"abnormal volatility (ATR pct {atr_percentile:.2f})")
        if recent_strategy_r < 0:
            cut(0.8, f"strategy recent form {recent_strategy_r:+.1f}R")
        risk = max(min(risk, hi), 0.0)

        # remaining DAILY headroom: never allow a loss to breach 1.7%
        if day_start_equity > 0:
            headroom = c.max_daily_loss + (daily_pl_combined
                                           / day_start_equity)
            headroom *= 0.9                       # safety margin
            if headroom <= 0:
                return 0.0, notes + ["no daily loss headroom left"]
            if risk > headroom:
                notes.append(f"capped by daily headroom {headroom:.2%}")
                risk = headroom
        # remaining WEEKLY headroom
        weekly_room = (c.max_weekly_drawdown - weekly_dd_frac) * 0.9
        if weekly_room <= 0:
            return 0.0, notes + ["no weekly drawdown headroom left"]
        if risk > weekly_room:
            notes.append(f"capped by weekly headroom {weekly_room:.2%}")
            risk = weekly_room

        risk = min(risk, c.max_risk_per_trade)
        if risk < c.min_risk_per_trade:
            return 0.0, notes + [
                f"risk {risk:.3%} below minimum useful {c.min_risk_per_trade:.2%}"]
        return risk, notes


@dataclass
class V4OrderFacts:
    is_demo_account: bool
    symbol_is_gold: bool
    market_open: bool
    spread_points: float
    spread_ok: bool
    positions_on_symbol: int
    has_pending_bot_order: bool
    news_blocked: bool
    news_reason: str
    lock: LockReason
    equity: float
    session_allowed: bool
    session_reason: str
    research_over: bool


@dataclass
class V4Preflight:
    ok: bool
    checks: List[str] = field(default_factory=list)
    reason: str = ""


def order_preflight(cfg, now: datetime, sig: Signal, entry: float,
                    volume_units: float, risk_money: float,
                    sizing_rejected: bool, sizing_reason: str,
                    facts: V4OrderFacts,
                    cooldown_active: bool) -> V4Preflight:
    checks: List[str] = []
    failures: List[str] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append(f"{'PASS' if passed else 'FAIL'} — {name}: {detail}")
        if not passed:
            failures.append(f"{name}: {detail}")

    check("demo account", facts.is_demo_account,
          "demo" if facts.is_demo_account else "LIVE ACCOUNT — refused")
    check("gold symbol", facts.symbol_is_gold, "verified gold")
    check("research window", not facts.research_over,
          "active" if not facts.research_over
          else "14-day research complete — no new entries")
    check("market open", facts.market_open,
          "open" if facts.market_open else "closed")
    check("spread", facts.spread_ok,
          f"{facts.spread_points:.0f} pts (max {cfg.max_spread_points:.0f})")
    d = sig.direction.sign
    sl_ok = (sig.stop - entry) * d < 0
    tp_ok = (sig.target - entry) * d > 0
    check("stop loss present & on correct side", sl_ok,
          f"SL {sig.stop:.2f} vs entry {entry:.2f}")
    check("take profit present & on correct side", tp_ok,
          f"TP {sig.target:.2f}")
    net_rr = sig.rr(entry)
    check("reward:risk", net_rr >= cfg.min_net_rr * 0.9,
          f"{net_rr:.2f}R at fill reference (floor {cfg.min_net_rr:.1f})")
    check("volume valid", not sizing_rejected and volume_units > 0,
          f"{volume_units} units" if not sizing_rejected else sizing_reason)
    check("risk below maximum", facts.equity > 0 and not sizing_rejected
          and risk_money <= facts.equity * cfg.max_risk_per_trade * 1.0001,
          f"{risk_money:.2f} ({(risk_money / facts.equity if facts.equity else 0):.2%},"
          f" max {cfg.max_risk_per_trade:.2%})")
    check("no risk lock", facts.lock == LockReason.NONE,
          "clear" if facts.lock == LockReason.NONE
          else f"lock: {facts.lock.value}")
    check("no loss cooldown", not cooldown_active,
          "clear" if not cooldown_active else "post-loss cooldown active")
    check("one position rule", facts.positions_on_symbol == 0,
          f"{facts.positions_on_symbol} open on symbol")
    check("no duplicate pending order", not facts.has_pending_bot_order,
          "none" if not facts.has_pending_bot_order else "pending exists")
    check("news clear", not facts.news_blocked,
          "clear" if not facts.news_blocked else facts.news_reason)
    check("session allowed", facts.session_allowed, facts.session_reason)

    return V4Preflight(ok=not failures, checks=checks,
                       reason=failures[0] if failures else "")
