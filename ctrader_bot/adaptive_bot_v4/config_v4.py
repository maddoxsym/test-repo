"""
V4 configuration — every policy knob of the 14-day research system.

The absolute safety rails live here and are enforced by the validator:
demo-only and gold-only are checked in the main cBot; risk is hard-capped
at 0.75% per trade, 1.7% combined daily loss, 5% weekly drawdown, a
cooldown after 3 consecutive losses, one real position, volume rounded
down, and there is no live-trading switch anywhere.

V4Config also carries the detector parameters (same names as V3's Config)
so the reused adaptive_bot detectors, session manager, news filter,
spread filter, position sizer and daily guard all accept it directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class V4Config:
    # ---- symbol lock --------------------------------------------------------
    allowed_gold_symbols: Tuple[str, ...] = (
        "GOLD", "XAUUSD", "XAU/USD", "XAUUSD.", "GOLD.", "XAUUSDM",
        "XAUUSD-STD", "GOLD-STD", "XAUUSD.PRO", "XAUUSD.RAW")

    # ---- ABSOLUTE risk rails (validator refuses anything looser) ------------
    max_risk_per_trade: float = 0.0075        # 0.75% hard ceiling
    min_risk_per_trade: float = 0.0005        # below this a trade is pointless
    max_daily_loss: float = 0.017             # 1.7% realised+floating => day lock
    max_weekly_drawdown: float = 0.05         # 5% week lock
    cooldown_after_losses: int = 3            # consecutive real losses ...
    loss_cooldown_hours: float = 4.0          # ... pause new entries this long
    max_positions: int = 1                    # one real GOLD position, ever
    max_trades_per_day: int = 12              # runaway protection, not a target
    max_trades_per_session: int = 8
    max_consecutive_losses: int = 999         # base-guard check disabled; the
                                              # V4 cooldown handles streaks
    daily_profit_hard_stop: float = 9.99      # never force/limit a profit day
    emergency_stop: bool = False
    emergency_stop_file: str = "EMERGENCY_STOP.txt"

    # ---- adaptive risk tiers (evidence-based; 0.75% is absolute) ------------
    risk_tier_experimental: Tuple[float, float] = (0.0010, 0.0025)
    risk_tier_moderate: Tuple[float, float] = (0.0025, 0.0045)
    risk_tier_strong: Tuple[float, float] = (0.0045, 0.0060)
    risk_tier_top: Tuple[float, float] = (0.0060, 0.0075)
    tier_moderate_min_n: int = 10             # shadow+real trades needed
    tier_strong_min_n: int = 15
    tier_top_min_n: int = 20
    tier_moderate_min_score: float = 0.10     # ranking score thresholds
    tier_strong_min_score: float = 0.25
    tier_top_min_score: float = 0.40

    # ---- costs & spread ------------------------------------------------------
    slippage_buffer_points: float = 10.0
    commission_per_unit: float = 0.0          # Skilling gold is spread-only
    max_spread_points: float = 60.0
    normal_spread_points: float = 35.0        # above this = "elevated" reducer

    # ---- reward:risk ----------------------------------------------------------
    min_net_rr: float = 2.0                   # after spread/slippage/commission
    preferred_rr: float = 2.5

    # ---- research clock --------------------------------------------------------
    research_days: int = 14
    heartbeat_minutes: int = 5
    debug_logging: bool = True

    # ---- population / learning -------------------------------------------------
    population_seed: int = 20260113           # deterministic strategy seeding
    max_population: int = 40
    min_shadow_trades_for_real: int = 5       # evidence before real money (demo)
    min_regime_trades: int = 3
    shrinkage_k: float = 6.0                  # expectancy shrunk toward 0
    exploration_c: float = 0.30               # UCB exploration constant
    dd_penalty: float = 0.04                  # per R of strategy max drawdown
    instability_penalty: float = 0.02         # per unit of R std-dev
    complexity_penalty: float = 0.01          # per mutation applied
    retire_min_trades: int = 10
    retire_expectancy: float = -0.15          # shrunk R/trade => retired
    bench_recent_n: int = 5
    bench_recent_sum_r: float = -3.0          # last-5 sum R => benched 1 day
    spawn_per_day: int = 3                    # bounded daily variant creation
    spawn_parent_min_n: int = 6
    virtual_equity: float = 10_000.0          # every shadow book starts here
    virtual_risk: float = 0.0035              # same risk basis for all shadows
    shadow_post_watch_bars: int = 60          # stop-too-tight post-mortem (M1)
    adapt_min_n: int = 8                      # evidence before a param adapts
    adapt_rate_threshold: float = 0.40        # e.g. 40% stop-too-tight => widen

    # ---- news protection (manual/schedule-based — same honest model as V3) ---
    news_enabled: bool = True
    news_block_before_min: int = 45
    news_block_after_min: int = 30
    block_nfp: bool = True
    nfp_hour_utc: int = 12
    nfp_minute_utc: int = 30
    block_fomc: bool = True
    block_cpi: bool = True
    block_speeches: bool = True
    fomc_events: Tuple[str, ...] = ()         # "YYYY-MM-DDTHH:MM" UTC — MAINTAIN!
    cpi_events: Tuple[str, ...] = ()
    speech_events: Tuple[str, ...] = ()
    manual_blackouts: Tuple[str, ...] = ()
    require_news_calendar: bool = False       # True = fail closed with no dates

    # ---- sessions (UTC; verify server clock once, set offset) -----------------
    server_utc_offset_hours: int = 0
    asia_enabled: bool = True                 # REAL entries; shadows always learn
    london_enabled: bool = True
    newyork_enabled: bool = True
    asia_start: int = 0
    asia_end: int = 7
    london_start: int = 7
    london_end: int = 16
    ny_start: int = 12
    ny_end: int = 21
    avoid_rollover: Tuple[int, int] = (21, 23)
    friday_last_entry_hour: int = 15
    friday_flat_hour: int = 20
    monday_first_entry_hour: int = 2
    weekend_hold_allowed: bool = False

    # ---- detector parameters (names shared with V3 so its detectors work) ----
    swing_left: int = 2
    swing_right: int = 2
    external_swing_left: int = 3
    external_swing_right: int = 3
    atr_period: int = 14
    displacement_atr_mult: float = 1.2
    displacement_body_ratio: float = 0.55
    bos_use_close: bool = True
    eq_level_atr_tol: float = 0.12
    zone_base_max_candles: int = 5
    zone_base_body_atr: float = 0.60
    zone_max_touches: int = 2
    zone_max_age_candles: int = 400
    fvg_min_size_atr: float = 0.15
    ob_require_structure_link: bool = True
    premium_discount_buffer: float = 0.05

    # ---- data windows (completed candles) -------------------------------------
    window_m1: int = 900
    window_m5: int = 600
    window_m15: int = 450
    window_m30: int = 350
    window_h1: int = 320
    min_candles_required: int = 60

    # ---- persistence ------------------------------------------------------------
    state_dir: str = ""                       # "" = <home>/Documents/XAUUSD_Adaptive_Bot_V4


class V4ConfigValidator:
    """Refuses any configuration that loosens the absolute rails."""

    def __init__(self, cfg: V4Config):
        self.cfg = cfg
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate(self) -> bool:
        c = self.cfg
        err = self.errors.append
        warn = self.warnings.append
        if not 0 < c.max_risk_per_trade <= 0.0075:
            err("max_risk_per_trade must be in (0, 0.0075] — 0.75% is the "
                "absolute ceiling for the 14-day experiment")
        if not 0 < c.max_daily_loss <= 0.017:
            err("max_daily_loss must be in (0, 0.017] — 1.7% combined is the "
                "absolute daily ceiling")
        if not 0 < c.max_weekly_drawdown <= 0.05:
            err("max_weekly_drawdown must be in (0, 0.05]")
        if c.max_positions != 1:
            err("max_positions must be exactly 1 (one real GOLD position)")
        if c.cooldown_after_losses < 1 or c.loss_cooldown_hours <= 0:
            err("loss cooldown must be configured (3 losses default)")
        if c.min_net_rr < 2.0:
            err("min_net_rr below 2.0 violates the experiment's target policy")
        if not (0 < c.min_risk_per_trade < c.max_risk_per_trade):
            err("need 0 < min_risk_per_trade < max_risk_per_trade")
        for name in ("risk_tier_experimental", "risk_tier_moderate",
                     "risk_tier_strong", "risk_tier_top"):
            lo, hi = getattr(c, name)
            if not (0 < lo <= hi <= c.max_risk_per_trade):
                err(f"{name} must fit inside (0, max_risk_per_trade]")
        if c.research_days < 1:
            err("research_days must be >= 1")
        if c.max_population < 8:
            err("max_population too small for meaningful comparison")
        if c.virtual_risk <= 0 or c.virtual_risk > 0.01:
            err("virtual_risk must be in (0, 1%]")
        if c.max_trades_per_day < 1:
            err("max_trades_per_day must be >= 1")
        if c.heartbeat_minutes < 1:
            err("heartbeat_minutes must be >= 1")
        if c.news_enabled and not (c.fomc_events or c.cpi_events):
            warn("NEWS DATES MISSING: no FOMC/CPI dates configured — only the "
                 "NFP first-Friday rule protects you. Add this month's dates "
                 "to fomc_events / cpi_events (or set require_news_calendar "
                 "= True to fail closed).")
        if not c.debug_logging:
            warn("debug_logging is OFF — the research spec asks for it ON")
        return not self.errors

    def report(self) -> str:
        lines = [f"CONFIG ERROR: {e}" for e in self.errors]
        lines += [f"CONFIG WARNING: {w}" for w in self.warnings]
        return "\n".join(lines) if lines else "config OK"
