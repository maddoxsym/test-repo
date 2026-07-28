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
from btcbot.exchange.models import Candle, Capability, InstrumentSpec, InstType  # noqa: E402


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


def make_perp_instrument(
    *,
    inst_id: str = "BTC-USDT-SWAP",
    ct_val: str = "0.01",
    lot_size: str = "0.1",
    min_size: str = "0.1",
    tick_size: str = "0.1",
    max_leverage: str = "100",
    settle_ccy: str = "USDT",
) -> InstrumentSpec:
    """A linear BTC perpetual matching OKX's instruments response shape.

    ``inst_id`` and the contract parameters are *fixture inputs*, mirroring
    what runtime discovery would return — production code never hardcodes them.
    """
    return InstrumentSpec(
        inst_id=inst_id,
        inst_type=InstType.SWAP,
        base_ccy="BTC",
        quote_ccy="USDT",
        settle_ccy=settle_ccy,
        ct_type="linear",
        ct_val=Decimal(ct_val),
        ct_val_ccy="BTC",
        ct_mult=Decimal("1"),
        state="live",
        tick_size=Decimal(tick_size),
        lot_size=Decimal(lot_size),
        min_size=Decimal(min_size),
        max_lmt_size=Decimal("100000"),
        max_mkt_size=Decimal("12000"),
        max_leverage=Decimal(max_leverage),
        capabilities=frozenset(
            {
                Capability.LONG,
                Capability.SHORT,
                Capability.LEVERAGE,
                Capability.MARKET_ORDER,
                Capability.LIMIT_ORDER,
                Capability.REDUCE_ONLY,
                Capability.ATTACHED_TPSL,
            }
        ),
    )


@pytest.fixture
def perp_instrument() -> InstrumentSpec:
    """The default linear BTC X-Perp used across execution/sizing tests."""
    return make_perp_instrument()


@pytest.fixture
def linear_instrument() -> InstrumentSpec:
    """Alias fixture — a discovered linear perpetual with native short support."""
    return make_perp_instrument()


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
