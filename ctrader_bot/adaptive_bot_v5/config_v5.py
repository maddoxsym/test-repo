"""
V5 configuration.

Every policy knob of the research system lives here, and V5ConfigValidator
refuses to start when any absolute rail has been loosened.

The V5 rails are STRICTER than V4's, never looser:
    risk/trade      V4 allowed up to 0.75%   -> V5 hard ceiling 0.25%
    real trades/day V4 allowed 12            -> V5 hard ceiling 4
    daily loss      1.7% combined            -> unchanged (1.7%)
    weekly drawdown 5%                       -> unchanged (5%)
    entries         any single signal         -> mandatory MTF sequence
                                                 + minimum confluence score
    stops           could be arbitrarily tight -> minimum ATR-based distance

V5Config also carries the detector parameter names used by the shared
adaptive_bot detectors (structure, liquidity, supply/demand, FVG, order
blocks, regime, sessions, news, spread, sizing, daily guard) so those
proven modules accept it directly without modification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class V5Config:
    # ---- symbol lock (GOLD only) -------------------------------------------
    allowed_gold_symbols: Tuple[str, ...] = (
        "GOLD", "XAUUSD", "XAU/USD", "XAUUSD.", "GOLD.", "XAUUSDM",
        "XAUUSD-STD", "GOLD-STD", "XAUUSD.PRO", "XAUUSD.RAW", "GOLDUSD")

    # ---- ABSOLUTE risk rails (validator refuses anything looser) ----------
    max_risk_per_trade: float = 0.0025        # 0.25% HARD ceiling (V4: 0.75%)
    min_risk_per_trade: float = 0.0005
    max_daily_loss: float = 0.017             # realised+floating => day lock
    max_weekly_drawdown: float = 0.05
    max_real_trades_per_day: int = 4           # HARD ceiling (V4: 12)
    max_real_trades_per_session: int = 3
    cooldown_after_losses: int = 3
    loss_cooldown_hours: float = 4.0
    max_positions: int = 1                     # one real GOLD position, ever
    emergency_stop: bool = False
    emergency_stop_file: str = "EMERGENCY_STOP.txt"

    # base-class knobs (the shared DailyLossGuard reads these names)
    max_trades_per_day: int = 4                # mirrors max_real_trades_per_day
    max_trades_per_session: int = 3
    max_consecutive_losses: int = 999          # V5 cooldown handles streaks
    daily_profit_hard_stop: float = 9.99       # never force/limit a profit day

    # ---- evidence-graduated risk (slow graduation, reduction-only) ---------
    # "Confidence and risk must increase slowly as more evidence is collected"
    risk_tier_probe: float = 0.0010            # 0.10%  n >= min_shadow_for_real
    risk_tier_early: float = 0.0015            # 0.15%  n >= 12
    risk_tier_confirmed: float = 0.0020        # 0.20%  n >= 25
    risk_tier_established: float = 0.0025      # 0.25%  n >= 40
    tier_early_min_n: int = 12
    tier_confirmed_min_n: int = 25
    tier_established_min_n: int = 40
    tier_early_min_expectancy: float = 0.05    # shrunk net-R per trade
    tier_confirmed_min_expectancy: float = 0.12
    tier_established_min_expectancy: float = 0.20
    tier_min_real_trades_confirmed: int = 3    # real evidence before 0.20%
    tier_min_real_trades_established: int = 8

    # ---- execution costs ---------------------------------------------------
    slippage_buffer_points: float = 10.0       # entry+exit allowance, points
    commission_per_unit: float = 0.0           # Skilling gold is spread-only
    max_spread_points: float = 60.0
    normal_spread_points: float = 35.0

    # ---- reward:risk -------------------------------------------------------
    min_net_rr: float = 2.0                    # main target, after all costs
    min_blended_rr: float = 1.55               # after the TP1 partial is taken
    preferred_rr: float = 2.5
    allow_runner_beyond_rr: float = 6.0        # structure may justify more

    # ---- stop-loss construction -------------------------------------------
    stop_structure_buffer_atr: float = 0.18    # beyond invalidation level
    stop_spread_buffer_mult: float = 1.5       # x spread, added to the buffer
    min_stop_atr_frac: float = 0.90            # >= 0.90 x M5 ATR (GOLD noise)
    max_stop_atr_frac: float = 4.50            # sanity ceiling
    min_stop_points: float = 90.0              # absolute floor in points

    # ---- targets -----------------------------------------------------------
    tp1_mode: str = "STRUCTURE_OR_R"           # STRUCTURE_OR_R | R_ONLY
    tp1_min_r: float = 1.0
    tp1_max_r: float = 1.5
    target_block_buffer_atr: float = 0.35      # opposing structure clearance
    min_room_atr: float = 1.20                 # room to opposing liquidity

    # ---- partial profit ----------------------------------------------------
    partial_enabled: bool = True
    partial_fraction: float = 0.45             # 40-50% at TP1
    partial_min_fraction: float = 0.30
    partial_max_fraction: float = 0.55

    # ---- breakeven (justified only) ---------------------------------------
    be_enabled: bool = True
    be_min_r: float = 1.0                      # floor, NOT a trigger by itself
    be_buffer_spread_mult: float = 1.5         # + commission allowance
    be_require_justification: bool = True      # never on a bare 1R touch

    # ---- trailing stop (strong trends only) --------------------------------
    trail_enabled: bool = True
    trail_min_r: float = 1.5                   # never before this
    trail_swing_atr_buffer: float = 0.35       # beyond the protected swing
    trail_m15_min_r: float = 3.0               # large runners use M15 swings
    strong_trend_min_factors: int = 6          # of 9 measured factors
    strong_trend_min_bos: int = 2
    strong_trend_min_efficiency: float = 0.32
    strong_trend_atr_pct_low: float = 0.30
    strong_trend_atr_pct_high: float = 0.97
    strong_trend_opposing_zone_atr: float = 1.5

    # ---- early exit on structure change -----------------------------------
    early_exit_enabled: bool = True
    early_exit_min_score: int = 3              # one noisy candle can't reach it
    early_exit_full_score: int = 5             # below this: partial + tighten
    early_exit_min_bars: int = 2               # never on the entry bar
    early_exit_partial_fraction: float = 0.50

    # ---- confluence engine -------------------------------------------------
    min_confluence: int = 58                   # of 100, global floor
    family_min_confluence: Dict[str, int] = field(default_factory=lambda: {
        "LIQUIDITY_REVERSAL": 66,
        "TREND_CONTINUATION": 60,
        "SESSION_LIQUIDITY": 66,
        "BREAKOUT_RETEST": 64,
        "HTF_ZONE_REVERSAL": 66,
        "FVG_CONTINUATION": 62,
    })

    # ---- signal freshness (fixes V4's stale pending fills) -----------------
    max_signal_age_minutes: int = 12
    max_entry_slippage_atr: float = 0.60
    entry_requires_retest: bool = True

    # ---- research clock (ACTIVE trading days) ------------------------------
    research_days: int = 30                    # active trading days
    active_day_min_minutes: int = 60           # observed market minutes/day
    heartbeat_minutes: int = 5
    debug_logging: bool = True

    # ---- population / learning --------------------------------------------
    population_seed: int = 20260730
    max_population: int = 24                   # 6 families x small variants
    max_variants_per_family: int = 4
    min_shadow_trades_for_real: int = 5         # experimental threshold only
    min_family_trades_for_real: int = 8         # family-level evidence too
    min_regime_trades: int = 3
    shrinkage_k: float = 8.0                    # net-R shrunk toward zero
    prior_sigma_r: float = 1.0                  # assumed R spread when n is
                                                # tiny, for the lower bound
    uncertainty_z: float = 1.0                  # z of the ranking lower bound
    exploration_c: float = 0.28                 # UCB exploration constant
    dd_penalty: float = 0.04
    instability_penalty: float = 0.02
    complexity_penalty: float = 0.01
    retire_min_trades: int = 12
    retire_expectancy: float = -0.12
    bench_recent_n: int = 5
    bench_recent_sum_r: float = -2.5
    spawn_per_day: int = 2
    spawn_parent_min_n: int = 10
    virtual_equity: float = 10_000.0
    virtual_risk: float = 0.0020                 # same basis for every shadow
    shadow_post_watch_bars: int = 60
    adapt_min_n: int = 10
    adapt_rate_threshold: float = 0.40

    # ---- news protection (manual/schedule only — no live feed exists) ------
    news_enabled: bool = True
    news_block_before_min: int = 45
    news_block_after_min: int = 30
    block_nfp: bool = True
    nfp_hour_utc: int = 12
    nfp_minute_utc: int = 30
    block_fomc: bool = True
    block_cpi: bool = True
    block_speeches: bool = True
    fomc_events: Tuple[str, ...] = ()          # "YYYY-MM-DDTHH:MM" UTC
    cpi_events: Tuple[str, ...] = ()
    speech_events: Tuple[str, ...] = ()
    manual_blackouts: Tuple[str, ...] = ()
    require_news_calendar: bool = False        # True = fail closed if empty

    # ---- sessions (UTC) ----------------------------------------------------
    server_utc_offset_hours: int = 0
    asia_enabled: bool = False                 # real entries: London/NY only
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

    # ---- detector parameters (names shared with the reused detectors) ------
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

    # ---- data windows (completed candles only) ----------------------------
    window_m1: int = 900
    window_m5: int = 700
    window_m15: int = 500
    window_m30: int = 400
    window_h1: int = 360
    min_candles_required: int = 60

    # ---- persistence -------------------------------------------------------
    state_dir: str = ""      # "" = <home>/Documents/XAUUSD_Adaptive_Bot_V5
    state_schema: int = 5


class V5ConfigValidator:
    """Refuses any configuration that loosens an absolute rail."""

    def __init__(self, cfg: V5Config):
        self.cfg = cfg
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate(self) -> bool:
        c = self.cfg
        err = self.errors.append
        warn = self.warnings.append

        # --- capital protection --------------------------------------------
        if not 0 < c.max_risk_per_trade <= 0.0025:
            err("max_risk_per_trade must be in (0, 0.0025] — 0.25% is the V5 "
                "absolute ceiling for demo research")
        if not 0 < c.max_daily_loss <= 0.017:
            err("max_daily_loss must be in (0, 0.017] (1.7% combined)")
        if not 0 < c.max_weekly_drawdown <= 0.05:
            err("max_weekly_drawdown must be in (0, 0.05]")
        if c.max_positions != 1:
            err("max_positions must be exactly 1 (one real GOLD position)")
        if not 1 <= c.max_real_trades_per_day <= 4:
            err("max_real_trades_per_day must be in [1, 4]")
        if c.max_trades_per_day != c.max_real_trades_per_day:
            err("max_trades_per_day must mirror max_real_trades_per_day so "
                "the shared daily guard enforces the same ceiling")
        if c.cooldown_after_losses < 1 or c.loss_cooldown_hours <= 0:
            err("consecutive-loss cooldown must be configured")
        if not (0 < c.min_risk_per_trade < c.max_risk_per_trade):
            err("need 0 < min_risk_per_trade < max_risk_per_trade")

        # --- risk tiers must be monotone and inside the ceiling ------------
        tiers = [("risk_tier_probe", c.risk_tier_probe),
                 ("risk_tier_early", c.risk_tier_early),
                 ("risk_tier_confirmed", c.risk_tier_confirmed),
                 ("risk_tier_established", c.risk_tier_established)]
        for name, value in tiers:
            if not 0 < value <= c.max_risk_per_trade:
                err(f"{name} must be in (0, max_risk_per_trade]")
        for (n0, v0), (n1, v1) in zip(tiers, tiers[1:]):
            if v1 < v0:
                err(f"{n1} must not be below {n0} — risk graduates upward "
                    f"with evidence only")
        if not (c.tier_early_min_n < c.tier_confirmed_min_n
                < c.tier_established_min_n):
            err("risk tier sample thresholds must increase strictly")

        # --- reward:risk ----------------------------------------------------
        if c.min_net_rr < 2.0:
            err("min_net_rr below 2.0 violates the research target policy")
        if c.min_blended_rr < 1.4:
            err("min_blended_rr below 1.4 makes the partial-profit policy "
                "self-defeating")
        if not 0 < c.tp1_min_r <= c.tp1_max_r:
            err("need 0 < tp1_min_r <= tp1_max_r")

        # --- stop-loss integrity -------------------------------------------
        if c.min_stop_atr_frac <= 0:
            err("min_stop_atr_frac must be > 0 — GOLD M5 stops need an "
                "explicit volatility floor")
        if c.min_stop_atr_frac >= c.max_stop_atr_frac:
            err("min_stop_atr_frac must be below max_stop_atr_frac")
        if c.min_stop_points <= 0:
            err("min_stop_points must be > 0")
        if c.stop_structure_buffer_atr <= 0:
            err("stop_structure_buffer_atr must be > 0")

        # --- partials -------------------------------------------------------
        if not (c.partial_min_fraction <= c.partial_fraction
                <= c.partial_max_fraction):
            err("partial_fraction must lie inside "
                "[partial_min_fraction, partial_max_fraction]")
        if c.partial_max_fraction >= 1.0:
            err("partial_max_fraction must be < 1.0 (a runner must remain)")

        # --- breakeven / trailing / early exit ------------------------------
        if c.be_enabled and not c.be_require_justification:
            err("be_require_justification must stay True — moving to "
                "breakeven on a bare 1R touch is the behaviour V5 exists to "
                "fix")
        if c.be_min_r <= 0:
            err("be_min_r must be > 0")
        if c.trail_enabled and c.trail_min_r < 1.0:
            err("trail_min_r must be >= 1.0 — trailing before 1R converts "
                "normal noise into stop-outs")
        if c.trail_enabled and c.strong_trend_min_factors < 4:
            err("strong_trend_min_factors < 4 would let the trailing stop "
                "run in ranges")
        if c.trail_enabled and c.strong_trend_min_bos < 2:
            err("strong_trend_min_bos must be >= 2 (repeated BOS)")
        if c.early_exit_enabled and c.early_exit_min_score < 2:
            err("early_exit_min_score < 2 would allow a single noisy candle "
                "to close the position")
        if c.early_exit_enabled \
                and c.early_exit_full_score < c.early_exit_min_score:
            err("early_exit_full_score must be >= early_exit_min_score")

        # --- confluence -----------------------------------------------------
        if not 0 < c.min_confluence <= 100:
            err("min_confluence must be in (0, 100]")
        if c.min_confluence < 50:
            err("min_confluence below 50 defeats the purpose of V5 — "
                "standalone signals are exactly what V4 got wrong")
        for fam, thr in c.family_min_confluence.items():
            if thr < c.min_confluence:
                err(f"family_min_confluence[{fam}]={thr} is below the global "
                    f"floor {c.min_confluence}")

        # --- signal freshness ------------------------------------------------
        if c.max_signal_age_minutes < 1:
            err("max_signal_age_minutes must be >= 1")
        if c.max_signal_age_minutes > 60:
            err("max_signal_age_minutes above 60 re-introduces V4's stale "
                "pending-fill defect")
        if c.max_entry_slippage_atr <= 0:
            err("max_entry_slippage_atr must be > 0 to reject gapped fills")

        # --- research / population ------------------------------------------
        if c.research_days < 1:
            err("research_days must be >= 1")
        if c.active_day_min_minutes < 1:
            err("active_day_min_minutes must be >= 1")
        if c.max_population < 6:
            err("max_population must allow at least one variant per family")
        if c.max_variants_per_family < 1:
            err("max_variants_per_family must be >= 1")
        if c.virtual_risk <= 0 or c.virtual_risk > 0.01:
            err("virtual_risk must be in (0, 1%]")
        if c.min_shadow_trades_for_real < 5:
            err("min_shadow_trades_for_real must be >= 5")
        if c.heartbeat_minutes < 1:
            err("heartbeat_minutes must be >= 1")

        # --- honesty warnings -----------------------------------------------
        if c.news_enabled and not (c.fomc_events or c.cpi_events):
            warn("NEWS DATES MISSING: no FOMC/CPI dates are configured, so "
                 "only the NFP first-Friday rule protects you. There is NO "
                 "live news feed in this bot. Add this month's dates to "
                 "fomc_events / cpi_events, or set require_news_calendar = "
                 "True to fail closed.")
        if not c.debug_logging:
            warn("debug_logging is OFF — the research spec asks for it ON")
        if c.asia_enabled:
            warn("asia_enabled = True: Asia real entries are allowed. Asian "
                 "GOLD liquidity is thin; shadow research covers Asia "
                 "regardless of this setting.")
        return not self.errors

    def report(self) -> str:
        lines = [f"CONFIG ERROR: {e}" for e in self.errors]
        lines += [f"CONFIG WARNING: {w}" for w in self.warnings]
        return "\n".join(lines) if lines else "config OK"
