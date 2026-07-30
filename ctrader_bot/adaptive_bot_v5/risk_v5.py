"""
V5 guards, risk sizing and the pre-order safety checklist.

V5Guard extends the proven DailyLossGuard with:
  * the 1.7% COMBINED (realised + floating) daily lock,
  * the 5% weekly drawdown lock,
  * a cooldown after 3 consecutive real losses whose expiry timestamp is
    persisted, so restarting the platform cannot serve the cooldown early,
  * the hard ceiling of 4 real trades per day (the base class enforces it
    from cfg.max_trades_per_day, which the validator forces to mirror
    cfg.max_real_trades_per_day).

RiskEngine turns learning evidence into a risk fraction.  Risk only ever
graduates upward with EVIDENCE, and every situational adjustment is a
REDUCTION.  There is no path that increases risk because the last trade lost:
no martingale, no grid, no averaging down, no recovery sizing.  The final
value is additionally capped by the remaining daily and weekly headroom, so a
single trade can never breach the 1.7% / 5% ceilings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from adaptive_bot.core.models import LockReason, SessionName
from adaptive_bot.risk.daily_loss_guard import DailyLossGuard, DayState


class V5Guard(DailyLossGuard):

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cooldown_until: Optional[datetime] = None
        self.lock_events: List[str] = []

    def register_close(self, profit: float,
                       now: Optional[datetime] = None) -> None:
        super().register_close(profit)
        if now is not None and profit < 0 \
                and self.consecutive_losses >= self.cfg.cooldown_after_losses:
            self.cooldown_until = now + timedelta(
                hours=self.cfg.loss_cooldown_hours)
            self.lock_events.append(
                f"{now.isoformat()} cooldown until "
                f"{self.cooldown_until.isoformat()} after "
                f"{self.consecutive_losses} consecutive losses")

    def cooldown_active(self, now: Optional[datetime]) -> bool:
        return (now is not None and self.cooldown_until is not None
                and now < self.cooldown_until)

    def lock_reason(self, equity: float, unrealised: float = 0.0,
                    session: Optional[SessionName] = None,
                    now: Optional[datetime] = None) -> LockReason:
        base = super().lock_reason(equity, unrealised, session)
        if base != LockReason.NONE:
            return base
        if now is not None and self.cooldown_until is not None:
            if now < self.cooldown_until:
                return LockReason.CONSECUTIVE_LOSSES
            # cooldown served: clear it and reset the streak
            self.cooldown_until = None
            self.consecutive_losses = 0
        return LockReason.NONE

    def daily_combined_pct(self, floating: float) -> float:
        if not self.day or self.day.start_equity <= 0:
            return 0.0
        return (self.day.realised + min(0.0, floating)) / self.day.start_equity

    def weekly_dd_pct(self, equity: float) -> float:
        if not self.week or self.week.start_equity <= 0:
            return 0.0
        low = min(self.week.min_equity, equity)
        return max(0.0, (self.week.start_equity - low) / self.week.start_equity)

    # -------------------------------------------------------------- state io
    def snapshot(self) -> dict:
        out = {
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until.isoformat()
            if self.cooldown_until else "",
            "lock_events": self.lock_events[-40:],
        }
        if self.day:
            out["day"] = dict(self.day.__dict__)
        if self.week:
            out["week"] = dict(self.week.__dict__)
        return out

    def restore(self, snap: dict, now: datetime) -> None:
        """Restore only for the CURRENT period, and never in a less
        restrictive direction than what broker history already produced."""
        day = snap.get("day")
        if day and self.day and day.get("day") == self.day.day:
            merged = DayState(**{k: day[k] for k in day
                                 if k in DayState.__dataclass_fields__})
            self.day.realised = min(self.day.realised, merged.realised)
            self.day.trades_opened = max(self.day.trades_opened,
                                         merged.trades_opened)
            self.day.losses = max(self.day.losses, merged.losses)
            self.day.wins = max(self.day.wins, merged.wins)
            if merged.min_equity:
                self.day.min_equity = min(self.day.min_equity or
                                          merged.min_equity,
                                          merged.min_equity)
            for key, value in merged.session_trades.items():
                self.day.session_trades[key] = max(
                    self.day.session_trades.get(key, 0), value)
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
        raw = snap.get("cooldown_until", "")
        if raw:
            try:
                until = datetime.fromisoformat(raw)
            except ValueError:
                until = None
            if until is not None and until > now:
                self.cooldown_until = until if self.cooldown_until is None \
                    else max(self.cooldown_until, until)
        self.lock_events = list(snap.get("lock_events", [])) + self.lock_events


@dataclass
class RiskDecision:
    fraction: float
    tier_reason: str
    notes: List[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.fraction <= 0.0


class RiskEngine:
    """Evidence tier, then reductions only, then hard headroom caps."""

    def __init__(self, cfg):
        self.cfg = cfg

    def choose(self, tier_fraction: float, tier_reason: str, *,
               equity: float, day_start_equity: float,
               daily_pl_combined: float, weekly_dd_frac: float,
               consecutive_losses: int, spread_points: float,
               atr_percentile: float, recent_strategy_r: float,
               confluence_score: float,
               confluence_threshold: float) -> RiskDecision:
        cfg = self.cfg
        risk = min(tier_fraction, cfg.max_risk_per_trade)
        notes = [f"tier {risk:.2%} — {tier_reason}"]

        def cut(mult: float, why: str) -> None:
            nonlocal risk
            risk *= mult
            notes.append(f"reduce x{mult:.2f}: {why}")

        # every adjustment below can only REDUCE risk
        if consecutive_losses >= 1:
            cut(0.75 ** consecutive_losses,
                f"{consecutive_losses} consecutive losses")
        if weekly_dd_frac >= 0.5 * cfg.max_weekly_drawdown:
            cut(0.6, f"weekly drawdown {weekly_dd_frac:.2%} is over half the "
                     f"{cfg.max_weekly_drawdown:.1%} limit")
        if day_start_equity > 0 and daily_pl_combined < 0:
            used = abs(daily_pl_combined) / day_start_equity
            if used >= 0.5 * cfg.max_daily_loss:
                cut(0.7, f"already {used:.2%} down today, over half the "
                         f"{cfg.max_daily_loss:.1%} daily limit")
        if spread_points > cfg.normal_spread_points:
            cut(0.7, f"spread {spread_points:.0f} pts above the "
                     f"{cfg.normal_spread_points:.0f} pt normal band")
        if atr_percentile >= 0.90 or atr_percentile <= 0.05:
            cut(0.8, f"abnormal volatility (ATR percentile "
                     f"{atr_percentile:.2f})")
        if recent_strategy_r < 0:
            cut(0.8, f"strategy recent form {recent_strategy_r:+.2f}R")
        if confluence_score < confluence_threshold + 6:
            cut(0.85, f"confluence {confluence_score:.0f} is only just over "
                      f"the {confluence_threshold:.0f} threshold")

        # ---- hard headroom: no single trade may breach the ceilings --------
        if day_start_equity > 0:
            headroom = (cfg.max_daily_loss
                        + daily_pl_combined / day_start_equity) * 0.9
            if headroom <= 0:
                notes.append("no daily loss headroom left")
                return RiskDecision(0.0, tier_reason, notes)
            if risk > headroom:
                notes.append(f"capped by daily headroom {headroom:.3%}")
                risk = headroom
        weekly_room = (cfg.max_weekly_drawdown - weekly_dd_frac) * 0.9
        if weekly_room <= 0:
            notes.append("no weekly drawdown headroom left")
            return RiskDecision(0.0, tier_reason, notes)
        if risk > weekly_room:
            notes.append(f"capped by weekly headroom {weekly_room:.3%}")
            risk = weekly_room

        risk = min(risk, cfg.max_risk_per_trade)
        if risk < cfg.min_risk_per_trade:
            notes.append(f"risk {risk:.4%} below the minimum useful "
                         f"{cfg.min_risk_per_trade:.2%} — no trade")
            return RiskDecision(0.0, tier_reason, notes)
        notes.append(f"final risk {risk:.3%} "
                     f"(hard ceiling {cfg.max_risk_per_trade:.2%})")
        return RiskDecision(risk, tier_reason, notes)


@dataclass
class OrderFacts:
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
    cooldown_active: bool
    real_trades_today: int
    emergency: bool


@dataclass
class Preflight:
    ok: bool
    checks: List[str] = field(default_factory=list)
    reason: str = ""


def order_preflight(cfg, candidate, fill: float, volume_units: float,
                    risk_money: float, sizing_rejected: bool,
                    sizing_reason: str, stop_distance: float, atr: float,
                    point: float, confluence_threshold: float,
                    facts: OrderFacts) -> Preflight:
    """The complete pre-order checklist. Every line is logged."""
    checks: List[str] = []
    failures: List[str] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append(f"{'PASS' if passed else 'FAIL'} — {name}: {detail}")
        if not passed:
            failures.append(f"{name}: {detail}")

    d = candidate.direction.sign
    check("demo account", facts.is_demo_account,
          "demo" if facts.is_demo_account else "LIVE ACCOUNT — refused")
    check("gold symbol", facts.symbol_is_gold, "verified gold instrument")
    check("emergency stop", not facts.emergency,
          "clear" if not facts.emergency else "EMERGENCY_STOP file present")
    check("research window", not facts.research_over,
          "active" if not facts.research_over
          else "research period complete — no new entries")
    check("market open", facts.market_open,
          "open" if facts.market_open else "closed")
    check("spread", facts.spread_ok,
          f"{facts.spread_points:.0f} pts (max {cfg.max_spread_points:.0f})")
    check("stop on the correct side", (candidate.stop - fill) * d < 0,
          f"stop {candidate.stop:.2f} vs fill {fill:.2f}")
    check("target on the correct side", (candidate.target - fill) * d > 0,
          f"target {candidate.target:.2f}")
    check("TP1 between fill and target",
          (candidate.tp1 - fill) * d > 0
          and (candidate.target - candidate.tp1) * d > 0,
          f"TP1 {candidate.tp1:.2f}")
    min_stop = max(cfg.min_stop_atr_frac * atr, cfg.min_stop_points * point)
    check("minimum stop distance", stop_distance >= min_stop - 1e-9,
          f"{stop_distance:.2f} vs minimum {min_stop:.2f} "
          f"({cfg.min_stop_atr_frac:.2f} x M5 ATR {atr:.2f}, floor "
          f"{cfg.min_stop_points:.0f} pts)")
    check("net reward:risk", candidate.target_r >= cfg.min_net_rr * 0.9,
          f"{candidate.target_r:.2f}R net (floor {cfg.min_net_rr:.1f})")
    check("blended reward:risk", candidate.blended_rr >= cfg.min_blended_rr,
          f"{candidate.blended_rr:.2f}R after the TP1 partial "
          f"(floor {cfg.min_blended_rr:.2f})")
    check("confluence threshold",
          candidate.confluence.score >= confluence_threshold,
          f"{candidate.confluence.score:.0f} vs {confluence_threshold:.0f}")
    check("volume valid", not sizing_rejected and volume_units > 0,
          f"{volume_units:g} units" if not sizing_rejected else sizing_reason)
    check("risk below the ceiling",
          facts.equity > 0 and not sizing_rejected
          and risk_money <= facts.equity * cfg.max_risk_per_trade * 1.0001,
          f"{risk_money:.2f} "
          f"({(risk_money / facts.equity if facts.equity else 0):.3%}, max "
          f"{cfg.max_risk_per_trade:.2%})")
    check("no risk lock", facts.lock == LockReason.NONE,
          "clear" if facts.lock == LockReason.NONE
          else f"lock: {facts.lock.value}")
    check("no loss cooldown", not facts.cooldown_active,
          "clear" if not facts.cooldown_active
          else "post-loss cooldown active")
    check("daily real-trade cap",
          facts.real_trades_today < cfg.max_real_trades_per_day,
          f"{facts.real_trades_today}/{cfg.max_real_trades_per_day} today")
    check("one position rule", facts.positions_on_symbol == 0,
          f"{facts.positions_on_symbol} open on symbol")
    check("no duplicate pending order", not facts.has_pending_bot_order,
          "none" if not facts.has_pending_bot_order else "pending order exists")
    check("news clear", not facts.news_blocked,
          "clear" if not facts.news_blocked else facts.news_reason)
    check("session allowed", facts.session_allowed, facts.session_reason)

    return Preflight(ok=not failures, checks=checks,
                     reason=failures[0] if failures else "")
