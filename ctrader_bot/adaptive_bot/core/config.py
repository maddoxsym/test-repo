"""
Configuration — every tunable of the bot with SAFE defaults.

Edit this file to configure the bot; the values here are the single source
of truth.  Optionally, the main cBot exposes the most important knobs as
cTrader UI parameters (declared in the auto-generated .cs file, see
README.md): when a UI parameter with the matching name exists it OVERRIDES
the value here.

There are NO credentials in this file and no live-trading switch anywhere:
the bot verifies at startup that the connected account is a DEMO account
and stops immediately otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class Config:
    # ---- symbol -----------------------------------------------------------
    # The bot refuses to run on anything that is not in this gold allowlist.
    # Skilling's gold symbol is usually "XAUUSD"; some brokers use "GOLD".
    allowed_gold_symbols: Tuple[str, ...] = (
        "GOLD", "XAUUSD", "XAU/USD", "XAUUSD.", "GOLD.", "XAUUSDM",
        "XAUUSD-STD", "GOLD-STD", "XAUUSD.PRO", "XAUUSD.RAW")

    # ---- risk policy (fractions of equity, e.g. 0.0025 == 0.25%) ----------
    max_risk_per_trade: float = 0.0025        # 0.25% — HARD ceiling per trade
    max_daily_loss: float = 0.01              # 1% realised+floating => day lock
    max_weekly_drawdown: float = 0.05         # extra guard beyond the spec
    max_consecutive_losses: int = 3           # pause after a losing streak
    max_positions: int = 1                    # one open GOLD position, ever
    max_trades_per_day: int = 3
    max_trades_per_session: int = 2
    daily_profit_hard_stop: float = 0.03      # bank a +3% day, stop entries

    # ---- reward:risk -------------------------------------------------------
    min_rr: float = 1.5                       # minimum net reward-to-risk
    preferred_rr: float = 2.0                 # target quality scoring anchor
    preferred_rr_cap: float = 4.0             # don't project fantasy targets

    # ---- costs & execution quality -----------------------------------------
    max_spread_points: float = 60.0           # 60 pts = $0.60 on gold; reject above
    normal_spread_points: float = 35.0        # regime abnormal-spread check
    slippage_buffer_points: float = 15.0      # assumed in sizing
    commission_per_unit: float = 0.0          # Skilling gold is spread-only by
                                              # default; set if your account differs

    # ---- structure / detection parameters ----------------------------------
    swing_left: int = 2
    swing_right: int = 2
    external_swing_left: int = 3              # for H4/D1 swing detection
    external_swing_right: int = 3
    atr_period: int = 14
    displacement_atr_mult: float = 1.2
    displacement_body_ratio: float = 0.55
    bos_use_close: bool = True                # close-based breaks (no wick BOS)
    eq_level_atr_tol: float = 0.12            # equal highs/lows tolerance
    zone_base_max_candles: int = 5
    zone_base_body_atr: float = 0.60
    zone_max_touches: int = 2                 # more touches -> stale zone
    zone_max_age_candles: int = 400
    fvg_min_size_atr: float = 0.15
    ob_require_structure_link: bool = True
    premium_discount_buffer: float = 0.05     # 45-55% counts as equilibrium
    extension_max_atr: float = 3.0            # "don't chase" rule
    retest_max_candles: int = 12
    range_edge_fraction: float = 0.25
    min_stop_atr: float = 0.35                # reject stops inside noise
    max_stop_atr: float = 3.5                 # reject stops absurdly wide
    stop_buffer_atr: float = 0.25             # buffer beyond structure

    # ---- scoring / gating ---------------------------------------------------
    min_score: float = 70.0                   # minimum setup score (of 100)
    countertrend_min_score: float = 80.0      # reversals need more proof
    asia_min_score: float = 85.0              # Asia = low liquidity, be picky
    allow_reversals: bool = False             # trades against 15m bias OFF
    require_m1_trigger: bool = True           # 1-minute trigger gate
    m1_trigger_max_age: int = 5               # trigger must be this recent (M1 bars)

    # ---- sessions (SERVER-TIME hours; see README about timezone) -----------
    # cTrader Python cBots receive Server.Time. For most cTrader brokers the
    # server time is UTC — VERIFY against your Skilling demo and adjust
    # server_utc_offset_hours if the platform clock differs from UTC.
    server_utc_offset_hours: int = 0
    asia_enabled: bool = True                 # only with asia_min_score quality
    london_enabled: bool = True
    newyork_enabled: bool = True
    asia_start: int = 0
    asia_end: int = 7
    london_start: int = 7
    london_end: int = 16
    ny_start: int = 12
    ny_end: int = 21
    avoid_rollover: Tuple[int, int] = (21, 23)    # no entries in this window
    friday_last_entry_hour: int = 15
    friday_flat_hour: int = 20                # close positions before weekend
    monday_first_entry_hour: int = 2
    weekend_hold_allowed: bool = False

    # ---- news protection (manual windows ONLY — see news_filter.py) --------
    # cTrader Python cBots have no reliable built-in economic calendar and
    # this bot performs NO network calls. News protection is therefore
    # schedule-based and manual: enter upcoming events below. NFP can be
    # auto-blocked via its regular first-Friday schedule.
    news_enabled: bool = True
    news_block_before_min: int = 30
    news_block_after_min: int = 30
    block_nfp: bool = True                    # first Friday of month (see below)
    nfp_hour_utc: int = 12                    # 12:30 UTC release stub hour
    nfp_minute_utc: int = 30
    block_fomc: bool = True                   # uses fomc_events list below
    block_cpi: bool = True                    # uses cpi_events list below
    block_speeches: bool = True               # uses speech_events list below
    # Event lists: "YYYY-MM-DDTHH:MM" in UTC. THESE MUST BE MAINTAINED BY
    # YOU — the bot never invents events. Examples (commented):
    #   fomc_events: ("2026-07-29T18:00",)
    fomc_events: Tuple[str, ...] = ()
    cpi_events: Tuple[str, ...] = ()
    speech_events: Tuple[str, ...] = ()
    # Free-form extra blackouts: "YYYY-MM-DDTHH:MM/YYYY-MM-DDTHH:MM" (UTC)
    manual_blackouts: Tuple[str, ...] = ()

    # ---- trade management (all OFF by default until tested) ----------------
    breakeven_enabled: bool = False
    breakeven_r: float = 1.0                  # move to BE only at >= +1R
    breakeven_needs_structure: bool = True    # and a confirmed swing in profit
    trailing_enabled: bool = False
    trail_atr_mult: float = 1.6
    partial_tp_enabled: bool = False
    partial_r: float = 1.7
    partial_fraction: float = 0.40
    time_exit_hours: float = 30.0             # safety exit for stale trades
    max_bars_no_progress: int = 60

    # ---- operations ---------------------------------------------------------
    emergency_stop: bool = False              # config kill switch (no entries)
    emergency_stop_file: str = "EMERGENCY_STOP.txt"   # file kill switch
    debug_logging: bool = False
    strict_mode: bool = True                  # anything unverifiable => no trade
    journal_enabled: bool = True
    journal_dir: str = ""                     # "" = <home>/Documents/XAUUSD_Adaptive_Bot
    order_fail_cooldown_min: int = 15         # no re-tries spam after a failure

    # ---- analysis windows (completed candles per timeframe) -----------------
    window_m1: int = 700
    window_m5: int = 600
    window_m15: int = 450
    window_h1: int = 320
    window_h4: int = 260
    window_d1: int = 220
    min_candles_required: int = 120


class ConfigValidator:
    """Validates a Config before the bot is allowed to do anything."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate(self) -> bool:
        c = self.cfg
        err = self.errors.append
        warn = self.warnings.append

        if not 0 < c.max_risk_per_trade <= 0.0025:
            err("max_risk_per_trade must be in (0, 0.0025] — 0.25% is the "
                "hard ceiling for this demo-only bot and cannot be raised")
        if not 0 < c.max_daily_loss <= 0.01:
            err("max_daily_loss must be in (0, 0.01] — 1% is the hard ceiling")
        if not 0 < c.max_weekly_drawdown <= 0.20:
            err("max_weekly_drawdown must be in (0, 0.20]")
        if c.max_consecutive_losses < 1:
            err("max_consecutive_losses must be >= 1")
        if c.max_positions != 1:
            err("max_positions must be exactly 1 (one GOLD position rule)")
        if c.max_trades_per_day < 1 or c.max_trades_per_day > 3:
            err("max_trades_per_day must be 1..3")
        if c.max_trades_per_session < 1:
            err("max_trades_per_session must be >= 1")
        if c.min_rr < 1.5:
            err("min_rr below 1.5 is not acceptable")
        if c.min_score < 70:
            err("min_score below 70 violates the no-trade threshold")
        if c.countertrend_min_score < c.min_score:
            err("countertrend_min_score must be >= min_score")
        if c.swing_left < 1 or c.swing_right < 1:
            err("swing detection needs at least 1 candle each side")
        if c.atr_period < 5:
            err("atr_period too small to be meaningful")
        if c.max_spread_points <= 0:
            err("max_spread_points must be positive")
        if c.stop_buffer_atr < 0:
            err("stop_buffer_atr cannot be negative")
        if not (0 < c.min_stop_atr < c.max_stop_atr):
            err("need 0 < min_stop_atr < max_stop_atr")
        if c.breakeven_r < 0.5:
            err("breakeven_r below 0.5R moves to break-even too early")
        if c.partial_fraction <= 0 or c.partial_fraction >= 1:
            err("partial_fraction must be inside (0, 1)")
        for name in ("asia", "london", "ny"):
            s = getattr(c, f"{name}_start")
            e = getattr(c, f"{name}_end")
            if not (0 <= s < 24 and 0 < e <= 24 and s < e):
                err(f"session hours invalid for {name}: {s}-{e}")
        if not (c.asia_enabled or c.london_enabled or c.newyork_enabled):
            warn("all sessions disabled — the bot will never trade")
        if c.allow_reversals:
            warn("allow_reversals=True — counter-bias trades enabled "
                 f"(min score {c.countertrend_min_score})")
        if not c.news_enabled:
            warn("news protection disabled — not recommended")
        if c.news_enabled and not (c.fomc_events or c.cpi_events
                                   or c.manual_blackouts):
            warn("news protection is manual/schedule-based and no FOMC/CPI "
                 "dates are configured — only the NFP first-Friday rule and "
                 "spread locks protect you; add this month's dates to "
                 "fomc_events / cpi_events in config.py")
        if c.breakeven_enabled or c.trailing_enabled or c.partial_tp_enabled:
            warn("management features enabled before being tested — the spec "
                 "recommends leaving them OFF until backtests pass")
        return not self.errors

    def report(self) -> str:
        lines = [f"CONFIG ERROR: {e}" for e in self.errors]
        lines += [f"CONFIG WARNING: {w}" for w in self.warnings]
        return "\n".join(lines) if lines else "config OK"
