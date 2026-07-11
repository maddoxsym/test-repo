"""
Central configuration.

Credentials come from environment variables (or a local ``.env`` file that
is loaded automatically if present).  NEVER hard-code your token here.

    OANDA_API_TOKEN    your v20 API token
    OANDA_ACCOUNT_ID   e.g. 101-004-1234567-001
    OANDA_ENV          "practice" (default) or "live"

Everything else is a plain dataclass you can edit below.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# .env loader (stdlib only -- no python-dotenv dependency)
# --------------------------------------------------------------------------

def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


# --------------------------------------------------------------------------
# Credentials / environment
# --------------------------------------------------------------------------

@dataclass
class OandaConfig:
    api_token: str = ""
    account_id: str = ""
    environment: str = "practice"          # "practice" | "live"
    request_timeout: float = 15.0

    @property
    def rest_host(self) -> str:
        return ("https://api-fxtrade.oanda.com"
                if self.environment == "live"
                else "https://api-fxpractice.oanda.com")

    @classmethod
    def from_env(cls) -> "OandaConfig":
        load_dotenv()
        return cls(
            api_token=os.environ.get("OANDA_API_TOKEN", ""),
            account_id=os.environ.get("OANDA_ACCOUNT_ID", ""),
            environment=os.environ.get("OANDA_ENV", "practice").lower(),
        )

    def validate(self) -> list[str]:
        problems = []
        if not self.api_token:
            problems.append("OANDA_API_TOKEN is not set")
        if not self.account_id:
            problems.append("OANDA_ACCOUNT_ID is not set")
        if self.environment not in ("practice", "live"):
            problems.append(f"OANDA_ENV must be practice|live, got {self.environment!r}")
        return problems


# --------------------------------------------------------------------------
# Risk management
# --------------------------------------------------------------------------

@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5        # % of balance risked per trade
    max_open_trades: int = 2               # across all instruments
    max_trades_per_day: int = 6            # hard cap on new entries per UTC day
    daily_loss_limit_pct: float = 3.0      # stop opening trades for the day
    daily_profit_target_pct: float = 5.0   # optional "bank it" stop for the day
    stop_atr_mult: float = 1.5             # initial stop = ATR * this
    take_profit_r: float = 1.6             # take profit at this many R
    breakeven_at_r: float = 1.0            # move stop to entry once trade is +1R
    max_notional_leverage: float = 5.0     # notional exposure cap vs balance
    # live-account safety interlock: both must be true to trade real money
    allow_live: bool = False


# --------------------------------------------------------------------------
# News guard
# --------------------------------------------------------------------------

@dataclass
class NewsConfig:
    enabled: bool = True
    # free real-time economic calendar (Forex Factory weekly feed)
    feed_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    refresh_minutes: int = 10
    block_before_min: int = 45             # no new trades this long before an event
    block_after_min: int = 20              # ... and this long after
    impacts_blocked: tuple[str, ...] = ("High",)
    # if the feed cannot be fetched and the cache is stale, refuse new trades
    fail_closed: bool = True
    cache_stale_hours: float = 6.0


# --------------------------------------------------------------------------
# Trade council (the assistants)
# --------------------------------------------------------------------------

@dataclass
class CouncilConfig:
    min_confirmations: int = 2             # at least N strategy analysts must agree
    base_score_threshold: float = 1.2      # weighted-vote score required to trade
    out_of_regime_penalty: float = 0.5     # signal weight multiplier off-regime
    max_spread_atr_frac: float = 0.35      # veto if spread > 35% of M5 ATR
    # trading session filter (UTC hours, inclusive start / exclusive end)
    session_start_utc: int = 6
    session_end_utc: int = 21
    block_friday_after_utc: int = 20
    rollover_block: tuple[str, str] = ("21:50", "22:15")   # daily spread spike


# --------------------------------------------------------------------------
# Learning engine
# --------------------------------------------------------------------------

@dataclass
class LearningConfig:
    eta: float = 0.06                      # weight learning rate (per R of outcome)
    weight_min: float = 0.2
    weight_max: float = 3.0
    combo_min_trades: int = 10             # combo needs this much history to matter
    combo_veto_expectancy: float = -0.05   # veto combos proven to lose (R/trade)
    combo_boost_expectancy: float = 0.20   # relax threshold for proven combos
    combo_boost_factor: float = 0.9
    threshold_min: float = 0.8
    threshold_max: float = 2.5
    threshold_step_up: float = 0.10        # get pickier after a losing day
    threshold_step_down: float = 0.05      # relax slowly after sustained edge
    recent_window: int = 10                # trades considered "recent form"


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

@dataclass
class EngineConfig:
    instruments: tuple[str, ...] = ("XAU_USD", "EUR_USD")
    decision_granularity: str = "M5"       # timeframe trades are decided on
    regime_granularity: str = "H1"         # timeframe the regime analyst uses
    candle_count: int = 400
    poll_seconds: int = 20
    db_path: str = "bot_data.sqlite3"
    log_path: str = "logs/bot.log"
    kill_switch_file: str = "KILL_SWITCH"  # create this file to halt the bot
    paper: bool = False                    # True = decide + journal, never send orders


@dataclass
class Config:
    oanda: OandaConfig = field(default_factory=OandaConfig.from_env)
    risk: RiskConfig = field(default_factory=RiskConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    council: CouncilConfig = field(default_factory=CouncilConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)


def load_config() -> Config:
    return Config()
