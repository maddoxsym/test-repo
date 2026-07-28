"""Configuration schema.

Every setting the operator can change lives here with a type, a bound, and a
default. Validation runs at startup and fails loudly with the exact field path,
so a typo in YAML never becomes a silent behaviour change mid-experiment.

Note what is *absent*: there is no field for a mainnet host, a "live" flag, or a
real-money toggle. That is deliberate — the demo restriction is structural, not
configurable.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..utils.timeutil import SUPPORTED_INTERVALS


class StrictModel(BaseModel):
    """Base: unknown keys are errors, not silently ignored typos."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ExperimentConfig(StrictModel):
    name: str = "OKX_DEMO_RESEARCH"
    research_duration_days: int = Field(14, ge=1, le=365)
    expected_demo_equity: float = Field(10_000.0, gt=0)
    outage_adjustment_policy: Literal["none", "extend_by_outage"] = "none"
    finalization_policy: Literal["close_at_end", "manage_to_exit"] = "close_at_end"
    auto_transition_to_champion: bool = True


class MarketConfig(StrictModel):
    """Which market to research.

    Note what is *absent*: there is no ``instId`` field. The tradable X-Perp
    is discovered at runtime from the exchange's instrument list (see
    ``exchange/instruments.py``) — a hardcoded instrument ID would silently
    break the day OKX renames or re-lists the contract.
    """

    base_currency: str = "BTC"
    settle_currency_preference: list[str] = ["USDT", "USDC", "USD"]
    timeframes: list[str] = ["1", "3", "5", "15", "30", "60", "240"]
    regime_timeframe: str = "60"
    regime_context_timeframe: str = "240"

    @field_validator("timeframes", "regime_timeframe", "regime_context_timeframe")
    @classmethod
    def _known_intervals(cls, value: Any) -> Any:
        items = value if isinstance(value, list) else [value]
        for item in items:
            if item not in SUPPORTED_INTERVALS:
                raise ValueError(
                    f"{item!r} is not a supported timeframe; valid: {', '.join(SUPPORTED_INTERVALS)}"
                )
        return value

    @field_validator("base_currency")
    @classmethod
    def _uppercase_ccy(cls, value: str) -> str:
        if value != value.upper():
            raise ValueError(f"currency must be uppercase, got {value!r}")
        return value

    @model_validator(mode="after")
    def _cross_checks(self) -> MarketConfig:
        if not self.settle_currency_preference:
            raise ValueError("settle_currency_preference cannot be empty")
        if self.regime_timeframe not in self.timeframes:
            raise ValueError(
                f"regime_timeframe {self.regime_timeframe!r} must be one of timeframes {self.timeframes}"
            )
        return self


class ExchangeConfig(StrictModel):
    request_timeout_seconds: float = Field(15.0, gt=0, le=120)
    max_retries: int = Field(4, ge=0, le=10)
    retry_backoff_base_seconds: float = Field(0.75, gt=0, le=10)
    instrument_refresh_minutes: int = Field(60, ge=1, le=1440)
    public_ws_enabled: bool = True
    private_ws_enabled: bool = True
    orderbook_depth: int = Field(50, ge=1, le=400)
    ws_ping_interval_seconds: float = Field(20.0, gt=0, le=25)
    ws_reconnect_max_backoff_seconds: float = Field(60.0, gt=0, le=600)


class StalenessConfig(StrictModel):
    ticker: float = Field(25.0, gt=0)
    kline: float = Field(180.0, gt=0)
    orderbook: float = Field(45.0, gt=0)


class DataConfig(StrictModel):
    candle_buffer: int = Field(1500, ge=200, le=20_000)
    history_days: int = Field(400, ge=7, le=3000)
    history_timeframes: list[str] = ["5", "15", "60", "240"]
    max_staleness_seconds: StalenessConfig = StalenessConfig()
    backfill_on_start: bool = True
    cache_dir: str = "data/history"

    @field_validator("history_timeframes")
    @classmethod
    def _known(cls, value: list[str]) -> list[str]:
        for item in value:
            if item not in SUPPORTED_INTERVALS:
                raise ValueError(f"{item!r} is not a supported timeframe")
        return value


class LeverageConfig(StrictModel):
    """Bounds for the DYNAMIC_LEVERAGE_ENGINE.

    The engine chooses leverage per entry from confidence, volatility, regime,
    and drawdown — but always inside these bounds, and the 10x ceiling is a
    hard schema constraint, not merely a default.
    """

    min_leverage: float = Field(1.0, ge=1.0, le=10.0)
    max_leverage: float = Field(10.0, ge=1.0, le=10.0)
    base_leverage: float = Field(2.0, ge=1.0, le=10.0)
    high_vol_leverage_cap: float = Field(3.0, ge=1.0, le=10.0)
    defensive_leverage_cap: float = Field(2.0, ge=1.0, le=10.0)
    # Liquidation protection: the estimated liquidation distance must be at
    # least this multiple of the stop distance, or the entry is refused.
    liq_buffer_stop_ratio: float = Field(3.0, ge=1.5, le=20.0)
    # Margin protection: initial margin for a position may not exceed this
    # fraction of the available balance.
    margin_utilization_cap: float = Field(0.5, gt=0, le=1.0)

    @model_validator(mode="after")
    def _ordering(self) -> LeverageConfig:
        if self.min_leverage > self.max_leverage:
            raise ValueError("min_leverage cannot exceed max_leverage")
        if not (self.min_leverage <= self.base_leverage <= self.max_leverage):
            raise ValueError("base_leverage must lie within [min_leverage, max_leverage]")
        return self


class RiskConfig(StrictModel):
    # Present for transparency; may not be changed — this system never falls
    # back to cross margin, silently or otherwise.
    margin_mode: Literal["isolated"] = "isolated"
    leverage: LeverageConfig = LeverageConfig()
    normal_risk_pct: float = Field(0.0075, gt=0, le=0.05)
    min_risk_pct: float = Field(0.005, gt=0, le=0.05)
    max_risk_pct: float = Field(0.02, gt=0, le=0.05)
    max_notional_pct_equity: float = Field(0.95, gt=0, le=1.0)
    max_concurrent_demo_positions: int = Field(1, ge=1, le=5)
    min_stop_distance_pct: float = Field(0.0008, gt=0, le=0.1)
    min_stop_distance_atr_mult: float = Field(0.15, gt=0, le=5)
    max_stop_distance_pct: float = Field(0.15, gt=0, le=0.9)
    daily_loss_limit_pct: float = Field(0.10, gt=0, le=1.0)
    drawdown_derisk_threshold_pct: float = Field(0.08, gt=0, le=1.0)
    drawdown_derisk_factor: float = Field(0.5, gt=0, le=1.0)
    confidence_size_floor: float = Field(0.6, gt=0, le=1.0)
    confidence_size_ceiling: float = Field(1.4, ge=1.0, le=3.0)

    @model_validator(mode="after")
    def _ordering(self) -> RiskConfig:
        if not (self.min_risk_pct <= self.normal_risk_pct <= self.max_risk_pct):
            raise ValueError(
                "risk percentages must satisfy min_risk_pct <= normal_risk_pct <= max_risk_pct "
                f"(got {self.min_risk_pct}, {self.normal_risk_pct}, {self.max_risk_pct})"
            )
        if self.min_stop_distance_pct >= self.max_stop_distance_pct:
            raise ValueError("min_stop_distance_pct must be below max_stop_distance_pct")
        return self


class ShadowConfig(StrictModel):
    initial_equity: float = Field(10_000.0, gt=0)
    risk_pct: float = Field(0.01, gt=0, le=0.5)
    max_concurrent_per_strategy: int = Field(1, ge=1, le=10)
    alternative_sizing_models: list[str] = [
        "fixed_fractional",
        "volatility_target",
        "confidence_scaled",
    ]
    fee_rate_taker: float = Field(0.00055, ge=0, le=0.01)
    fee_rate_maker: float = Field(0.0002, ge=0, le=0.01)
    slippage_bps: float = Field(2.0, ge=0, le=500)


class AllocatorConfig(StrictModel):
    method: Literal["thompson", "ucb", "confidence_weighted"] = "thompson"
    min_observations_before_exploitation: int = Field(12, ge=1, le=500)
    forced_exploration_ratio: float = Field(0.25, ge=0, le=1)
    cooldown_seconds_per_strategy: int = Field(900, ge=0, le=86_400)
    global_cooldown_seconds: int = Field(120, ge=0, le=86_400)
    max_demo_orders_per_hour: int = Field(12, ge=1, le=500)
    max_demo_orders_per_day: int = Field(120, ge=1, le=5000)
    min_signal_confidence: float = Field(0.45, ge=0, le=1)
    historical_prior_weight: float = Field(0.35, ge=0, le=1)
    shadow_prior_weight: float = Field(0.45, ge=0, le=1)
    regime_suitability_weight: float = Field(0.2, ge=0, le=1)


class BacktestingConfig(StrictModel):
    fee_rate_taker: float = Field(0.00055, ge=0, le=0.01)
    fee_rate_maker: float = Field(0.0002, ge=0, le=0.01)
    slippage_bps: float = Field(2.0, ge=0, le=500)
    spread_bps: float = Field(1.0, ge=0, le=500)
    latency_ms: int = Field(250, ge=0, le=10_000)
    partial_fill_probability: float = Field(0.15, ge=0, le=1)
    stress_multipliers: list[float] = [1.0, 2.0, 4.0]
    train_fraction: float = Field(0.5, gt=0, lt=1)
    validation_fraction: float = Field(0.2, gt=0, lt=1)
    embargo_bars: int = Field(20, ge=0, le=1000)
    walk_forward_windows: int = Field(6, ge=2, le=50)
    walk_forward_mode: Literal["rolling", "anchored"] = "rolling"
    min_trades_for_confidence: int = Field(30, ge=1, le=1000)

    @model_validator(mode="after")
    def _splits_leave_oos(self) -> BacktestingConfig:
        if self.train_fraction + self.validation_fraction >= 1.0:
            raise ValueError(
                "train_fraction + validation_fraction must leave room for out-of-sample data "
                f"(got {self.train_fraction} + {self.validation_fraction})"
            )
        return self


class RegimeConfig(StrictModel):
    adx_trend_threshold: float = Field(22.0, gt=0, le=100)
    adx_strong_trend_threshold: float = Field(32.0, gt=0, le=100)
    atr_lookback: int = Field(14, ge=2, le=500)
    realized_vol_lookback: int = Field(48, ge=5, le=1000)
    vol_expansion_ratio: float = Field(1.35, gt=1.0, le=10)
    vol_contraction_ratio: float = Field(0.7, gt=0, lt=1.0)
    range_compression_percentile: float = Field(25.0, ge=0, le=100)
    range_expansion_percentile: float = Field(75.0, ge=0, le=100)
    slope_lookback: int = Field(20, ge=2, le=500)
    persistence_lookback: int = Field(30, ge=5, le=500)
    min_confidence: float = Field(0.4, ge=0, le=1)

    @model_validator(mode="after")
    def _thresholds(self) -> RegimeConfig:
        if self.adx_strong_trend_threshold <= self.adx_trend_threshold:
            raise ValueError("adx_strong_trend_threshold must exceed adx_trend_threshold")
        if self.range_compression_percentile >= self.range_expansion_percentile:
            raise ValueError("range_compression_percentile must be below range_expansion_percentile")
        return self


class NewsProviderConfig(StrictModel):
    name: str
    enabled: bool = True
    feeds: list[str] = []


class NewsConfig(StrictModel):
    enabled: bool = True
    poll_interval_seconds: int = Field(300, ge=30, le=86_400)
    providers: list[NewsProviderConfig] = []
    max_age_minutes: int = Field(240, ge=1, le=10_080)
    high_impact_pause_minutes: int = Field(20, ge=0, le=1440)
    high_impact_size_factor: float = Field(0.5, gt=0, le=1)
    degraded_confidence_factor: float = Field(0.9, gt=0, le=1)
    adaptive_influence: bool = True
    influence_floor: float = Field(0.0, ge=0, le=1)
    influence_ceiling: float = Field(1.0, ge=0, le=1)

    @model_validator(mode="after")
    def _influence_band(self) -> NewsConfig:
        if self.influence_floor > self.influence_ceiling:
            raise ValueError("influence_floor cannot exceed influence_ceiling")
        return self


class LearningConfig(StrictModel):
    enabled: bool = True
    evaluation_interval_minutes: int = Field(60, ge=1, le=10_080)
    min_trades_for_parameter_update: int = Field(40, ge=5, le=10_000)
    min_trades_for_high_confidence: int = Field(30, ge=5, le=10_000)
    candidate_generation_interval_hours: int = Field(12, ge=1, le=720)
    max_candidates_per_strategy: int = Field(3, ge=1, le=50)
    promotion_min_expectancy_improvement: float = Field(0.08, ge=0, le=10)
    promotion_min_new_trades: int = Field(25, ge=1, le=10_000)
    promotion_max_drawdown_ratio: float = Field(1.25, gt=0, le=10)
    promotion_min_profit_factor: float = Field(1.1, gt=0, le=100)
    parameter_stability_neighbours: int = Field(2, ge=1, le=10)
    parameter_stability_min_ratio: float = Field(0.6, ge=0, le=1)
    calibration_bins: int = Field(5, ge=2, le=20)


class ScoringWeights(StrictModel):
    oos_expectancy: float = Field(0.14, ge=0, le=1)
    walk_forward_consistency: float = Field(0.11, ge=0, le=1)
    shadow_expectancy: float = Field(0.12, ge=0, le=1)
    demo_performance: float = Field(0.14, ge=0, le=1)
    profit_factor: float = Field(0.08, ge=0, le=1)
    risk_adjusted_return: float = Field(0.09, ge=0, le=1)
    max_drawdown: float = Field(0.08, ge=0, le=1)
    observations: float = Field(0.07, ge=0, le=1)
    regime_stability: float = Field(0.06, ge=0, le=1)
    parameter_stability: float = Field(0.04, ge=0, le=1)
    cost_robustness: float = Field(0.05, ge=0, le=1)
    consistency: float = Field(0.02, ge=0, le=1)

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()

    @model_validator(mode="after")
    def _non_zero(self) -> ScoringWeights:
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("scoring weights cannot all be zero")
        return self


class ScoringConfig(StrictModel):
    weights: ScoringWeights = ScoringWeights()
    bootstrap_samples: int = Field(2000, ge=100, le=100_000)
    bootstrap_confidence: float = Field(0.9, gt=0.5, lt=1.0)
    min_trades_full_credit: int = Field(40, ge=2, le=10_000)
    min_trades_any_credit: int = Field(8, ge=1, le=10_000)
    single_winner_concentration_threshold: float = Field(0.4, gt=0, le=1)
    period_concentration_threshold: float = Field(0.6, gt=0, le=1)

    @model_validator(mode="after")
    def _sample_ordering(self) -> ScoringConfig:
        if self.min_trades_any_credit >= self.min_trades_full_credit:
            raise ValueError("min_trades_any_credit must be below min_trades_full_credit")
        return self


class ChampionConfig(StrictModel):
    min_score_gap_for_single_champion: float = Field(3.0, ge=0, le=100)
    min_demo_trades: int = Field(10, ge=0, le=10_000)
    min_total_observations: int = Field(40, ge=1, le=100_000)
    ensemble_min_regimes: int = Field(2, ge=1, le=11)
    ensemble_advantage_threshold: float = Field(2.5, ge=0, le=100)
    challenger_min_new_trades: int = Field(25, ge=1, le=10_000)
    challenger_min_expectancy_improvement: float = Field(0.1, ge=0, le=10)
    challenger_evaluation_interval_hours: int = Field(6, ge=1, le=720)
    decay_rolling_window: int = Field(30, ge=5, le=1000)
    decay_expectancy_drop_threshold: float = Field(0.5, gt=0, le=1)
    decay_min_trades: int = Field(25, ge=5, le=10_000)


class SafetyConfig(StrictModel):
    mainnet_negative_control: bool = True
    reverify_interval_minutes: int = Field(60, ge=1, le=1440)
    # Clock drift: OKX rejects requests whose timestamp strays too far from
    # server time. Beyond this measured drift, authenticated trading pauses.
    max_clock_drift_ms: int = Field(5000, ge=500, le=30_000)
    clock_resync_interval_minutes: int = Field(10, ge=1, le=1440)
    # Liquidation-risk breaker: flatten and pause when a live position's
    # margin ratio falls to this multiple of the exchange's maintenance level.
    liquidation_margin_ratio_floor: float = Field(3.0, ge=1.1, le=100.0)
    max_consecutive_api_errors: int = Field(12, ge=1, le=1000)
    max_orders_per_minute: int = Field(6, ge=1, le=120)
    price_sanity_min: float = Field(1000.0, gt=0)
    price_sanity_max: float = Field(5_000_000.0, gt=0)
    price_jump_reject_pct: float = Field(0.25, gt=0, le=10)
    safe_mode_cooldown_seconds: int = Field(300, ge=0, le=86_400)
    require_demo_verification_for_orders: bool = True

    @model_validator(mode="after")
    def _price_band(self) -> SafetyConfig:
        if self.price_sanity_min >= self.price_sanity_max:
            raise ValueError("price_sanity_min must be below price_sanity_max")
        return self

    @field_validator("require_demo_verification_for_orders")
    @classmethod
    def _cannot_disable_demo_requirement(cls, value: bool) -> bool:
        # Present in config for transparency, but it may not be turned off:
        # the brief requires that no flag can bypass the safety lock.
        if value is not True:
            raise ValueError(
                "require_demo_verification_for_orders cannot be disabled — the demo safety "
                "lock is not bypassable by configuration"
            )
        return value


class DatabaseConfig(StrictModel):
    path: str = "data/btcbot.db"
    backup_dir: str = "data/backups"
    backup_interval_hours: int = Field(6, ge=1, le=720)
    busy_timeout_ms: int = Field(10_000, ge=100, le=120_000)


class LoggingConfig(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    console: bool = True
    file: str | None = "logs/btcbot.log"
    json_file: str | None = "logs/events.jsonl"
    max_bytes: int = Field(20_971_520, ge=1024)
    backup_count: int = Field(10, ge=0, le=100)


class DashboardConfig(StrictModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(8787, ge=1, le=65_535)
    refresh_seconds: int = Field(5, ge=1, le=300)


class ReportingConfig(StrictModel):
    output_dir: str = "reports"
    export_dir: str = "reports/exports"
    daily_report_hour_utc: int = Field(0, ge=0, le=23)


class NotificationsConfig(StrictModel):
    enabled: bool = False
    console: bool = True
    telegram: bool = False
    discord: bool = False


class StrategiesConfig(StrictModel):
    enabled: list[str] = []
    disabled: list[str] = []
    overrides: dict[str, dict[str, Any]] = {}


class AppConfig(StrictModel):
    """The fully-resolved, validated configuration."""

    experiment: ExperimentConfig = ExperimentConfig()
    market: MarketConfig = MarketConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    data: DataConfig = DataConfig()
    risk: RiskConfig = RiskConfig()
    shadow: ShadowConfig = ShadowConfig()
    allocator: AllocatorConfig = AllocatorConfig()
    backtesting: BacktestingConfig = BacktestingConfig()
    regime: RegimeConfig = RegimeConfig()
    news: NewsConfig = NewsConfig()
    learning: LearningConfig = LearningConfig()
    scoring: ScoringConfig = ScoringConfig()
    champion: ChampionConfig = ChampionConfig()
    safety: SafetyConfig = SafetyConfig()
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()
    dashboard: DashboardConfig = DashboardConfig()
    reporting: ReportingConfig = ReportingConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    strategies: StrategiesConfig = StrategiesConfig()

    @model_validator(mode="after")
    def _cross_section(self) -> AppConfig:
        if self.market.regime_context_timeframe not in self.market.timeframes:
            raise ValueError(
                "market.regime_context_timeframe must appear in market.timeframes"
            )
        missing = [tf for tf in self.data.history_timeframes if tf not in self.market.timeframes]
        if missing:
            raise ValueError(
                f"data.history_timeframes contains timeframes absent from market.timeframes: {missing}"
            )
        overlap = set(self.strategies.enabled) & set(self.strategies.disabled)
        if overlap:
            raise ValueError(f"strategies listed as both enabled and disabled: {sorted(overlap)}")
        return self
