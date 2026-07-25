"""Shared test fixtures."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

# Make src/ importable without requiring an editable install.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from btcbot.config.schema import (  # noqa: E402
    AppConfig,
    BacktestingConfig,
    RegimeConfig,
    RiskConfig,
    ScoringConfig,
)
from btcbot.database.db import Database  # noqa: E402
from btcbot.database.migrations import run_migrations  # noqa: E402
from btcbot.database.repositories import Repositories  # noqa: E402
from btcbot.exchange.models import Candle, Capability, Category, InstrumentSpec  # noqa: E402


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig()


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig()


@pytest.fixture
def regime_config() -> RegimeConfig:
    return RegimeConfig()


@pytest.fixture
def backtest_config() -> BacktestingConfig:
    return BacktestingConfig()


@pytest.fixture
def scoring_config() -> ScoringConfig:
    return ScoringConfig()


@pytest.fixture
def spot_instrument() -> InstrumentSpec:
    """A BTCUSDT spot instrument matching Bybit's documented response shape."""
    return InstrumentSpec(
        symbol="BTCUSDT",
        category=Category.SPOT,
        base_coin="BTC",
        quote_coin="USDT",
        status="Trading",
        tick_size=Decimal("0.1"),
        qty_step=Decimal("0.000001"),
        min_order_qty=Decimal("0.000011"),
        max_order_qty=Decimal("83"),
        min_order_amt=Decimal("5"),
        max_order_amt=Decimal("8000000"),
        max_market_order_qty=Decimal("41.5"),
        base_precision=Decimal("0.000001"),
        capabilities=frozenset(
            {
                Capability.LONG,
                Capability.MARKET_ORDER,
                Capability.LIMIT_ORDER,
                Capability.ATTACHED_TPSL,
            }
        ),
        margin_trading="utaOnly",
    )


@pytest.fixture
def linear_instrument() -> InstrumentSpec:
    """A derivatives instrument — used to prove short support is discovered, not assumed."""
    return InstrumentSpec(
        symbol="BTCUSDT",
        category=Category.LINEAR,
        base_coin="BTC",
        quote_coin="USDT",
        status="Trading",
        tick_size=Decimal("0.10"),
        qty_step=Decimal("0.001"),
        min_order_qty=Decimal("0.001"),
        max_order_qty=Decimal("1190"),
        min_order_amt=None,
        max_order_amt=None,
        max_market_order_qty=Decimal("500"),
        base_precision=None,
        capabilities=frozenset(
            {
                Capability.LONG,
                Capability.SHORT,
                Capability.LEVERAGE,
                Capability.MARKET_ORDER,
                Capability.LIMIT_ORDER,
                Capability.REDUCE_ONLY,
            }
        ),
    )


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    run_migrations(db)
    yield db
    db.close()


@pytest.fixture
def repos(temp_db: Database) -> Repositories:
    return Repositories(temp_db)


BASE_MS = 1_700_000_000_000  # fixed epoch so every fixture is reproducible


def make_candles(
    closes: list[float],
    *,
    timeframe: str = "5",
    start_ms: int = BASE_MS,
    high_pad: float = 0.0,
    low_pad: float = 0.0,
    volume: float = 100.0,
) -> list[Candle]:
    """Build a closed-candle series from a list of closes."""
    from btcbot.utils.timeutil import interval_ms

    step = interval_ms(timeframe)
    candles: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_price = previous
        high = max(open_price, close) + high_pad
        low = min(open_price, close) - low_pad
        candles.append(
            Candle(
                open_ms=start_ms + index * step,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                turnover=volume * close,
                timeframe=timeframe,
                confirmed=True,
            )
        )
        previous = close
    return candles


@pytest.fixture
def trending_candles() -> list[Candle]:
    """A clean uptrend — used by regime and trend-strategy tests."""
    closes = [30_000.0 + i * 45.0 for i in range(400)]
    return make_candles(closes, timeframe="60", high_pad=20.0, low_pad=20.0)


@pytest.fixture
def ranging_candles() -> list[Candle]:
    """A tight oscillation with no drift."""
    import math

    closes = [30_000.0 + 120.0 * math.sin(i / 5.0) for i in range(400)]
    return make_candles(closes, timeframe="60", high_pad=15.0, low_pad=15.0)
