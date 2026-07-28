"""Orchestrator bootstrap and dry-run loop, driven by a mocked exchange.

The orchestrator is the largest integration surface in the system, so it gets
tested directly rather than only through its parts. A mock REST client serves
realistic candles and instrument data, which lets the whole startup sequence run
offline: migrations → client → verification → capability discovery → balance →
backfill → market-data check → registry → layer construction.

Two behaviours matter most and are asserted explicitly:

* dry run **never** starts the 14-day timer
* without verification, the demo layer stays disabled
"""

from __future__ import annotations

import math

import pytest

from btcbot.config.loader import Credentials, LoadedConfig
from btcbot.config.schema import AppConfig
from btcbot.exchange.models import (
    AccountConfig,
    Candle,
    InstrumentSpec,
    InstType,
    PositionMode,
    Ticker,
    WalletBalance,
)
from btcbot.utils.timeutil import now_ms
from tests.integration.test_dry_run_pipeline import realistic_series


def _credentials() -> Credentials:
    return Credentials(api_key="k" * 20, api_secret="s" * 20, passphrase="p" * 12)

pytestmark = pytest.mark.integration


class MockRestClient:
    """Serves deterministic market data; records what was asked for."""

    def __init__(self, instrument: InstrumentSpec, *, equity: float = 10_000.0) -> None:
        self.base_url = "https://eea.okx.com"
        self._instrument = instrument
        self._equity = equity
        self.consecutive_errors = 0
        self.clock_offset_ms = 12
        self.clock_synced = True
        self.closed = False
        self.calls: list[str] = []
        self._series: dict[str, list[Candle]] = {
            timeframe: realistic_series(timeframe, seed=500 + index, bars=700)
            for index, timeframe in enumerate(("1", "3", "5", "15", "30", "60", "240"))
        }

    @property
    def has_credentials(self) -> bool:
        return True

    def demo_header_enforced(self) -> bool:
        return True

    def clock_drift_exceeds(self, max_drift_ms: int) -> bool:
        return abs(self.clock_offset_ms) > max_drift_ms

    async def sync_clock(self) -> int:
        self.calls.append("sync_clock")
        return self.clock_offset_ms

    async def get_instruments(self, inst_type=InstType.SWAP, *, inst_id=None):
        self.calls.append(f"instruments:{inst_type.value}")
        if inst_type is InstType.SWAP:
            return [self._instrument]
        return []

    async def get_ticker(self, inst_id: str) -> Ticker:
        self.calls.append("ticker")
        last = self._series["1"][-1].close
        return Ticker(
            inst_id=inst_id, last_price=last, bid_price=last - 0.5, ask_price=last + 0.5,
            volume_24h=12_000.0, turnover_24h=6e8, price_24h_pct=0.01, ts_ms=now_ms(),
        )

    async def get_klines(self, inst_id, interval, *, start_ms=None, end_ms=None, limit=300):
        self.calls.append(f"klines:{interval}")
        candles = self._series.get(interval, [])
        if start_ms is not None:
            candles = [c for c in candles if c.open_ms >= start_ms]
        if end_ms is not None:
            candles = [c for c in candles if c.open_ms <= end_ms]
        return candles[-limit:]

    async def get_wallet_balance(self) -> WalletBalance:
        self.calls.append("balance")
        return WalletBalance(
            total_equity=self._equity,
            total_available=self._equity,
            unrealized_pnl=0.0,
            coins={"USDT": {"eq": self._equity, "availEq": self._equity,
                            "eqUsd": self._equity}},
            ts_ms=now_ms(),
        )

    async def get_account_config(self) -> AccountConfig:
        self.calls.append("account_config")
        return AccountConfig(
            uid="demo-123", account_level="2", position_mode=PositionMode.NET,
            raw={"uid": "demo-123", "acctLv": "2", "posMode": "net_mode"},
        )

    async def get_positions(self, inst_id=None):
        return []

    async def get_open_orders(self, inst_id):
        return []

    async def get_executions(self, inst_id, *, limit=100, start_ms=None):
        return []

    async def get_funding_bills(self, *, limit=100):
        return []

    async def cancel_all(self, inst_id):
        return {"cancelled": 0, "failed": []}

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def loaded_config(tmp_path) -> LoadedConfig:
    config = AppConfig(
        market={
            "timeframes": ["5", "15", "60", "240"],
            "regime_timeframe": "60",
            "regime_context_timeframe": "240",
        },
        data={
            "history_timeframes": ["5", "15", "60", "240"],
            "candle_buffer": 600,
            "backfill_on_start": True,
            "cache_dir": str(tmp_path / "history"),
        },
        database={"path": str(tmp_path / "boot.db"), "backup_dir": str(tmp_path / "backups")},
        logging={"file": None, "json_file": None, "console": False},
        dashboard={"enabled": False},
        reporting={
            "output_dir": str(tmp_path / "reports"),
            "export_dir": str(tmp_path / "reports" / "exports"),
        },
        news={"enabled": False, "providers": []},
        # The live-environment negative control needs real outbound network
        # access to eea.okx.com; offline it correctly reports "inconclusive"
        # and blocks trading. That behaviour is covered by test_safety_lock.py,
        # so it is switched off here to keep this test about bootstrap wiring.
        safety={"mainnet_negative_control": False},
        # Keep the strategy set small so the test stays quick.
        strategies={"enabled": ["ema_trend_cross_15m", "donchian_breakout_15m",
                                "vwap_reversion_5m", "ema_adx_trend_1h"]},
    )
    return LoadedConfig(
        config=config, config_hash="boothash00000000",
        source_path=tmp_path / "cfg.yaml", raw={},
    )


def _patch_client(monkeypatch, instrument, **kwargs) -> MockRestClient:
    mock = MockRestClient(instrument, **kwargs)
    monkeypatch.setattr(
        "btcbot.app.orchestrator.OkxDemoClient", lambda **_: mock
    )
    return mock


class TestBootstrap:
    async def test_bootstrap_completes_every_precondition(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        from btcbot.app.orchestrator import Orchestrator

        mock = _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(
            loaded_config, _credentials()
        )
        try:
            preconditions = await orchestrator.bootstrap()

            assert preconditions.config_valid
            assert preconditions.migrations_applied
            assert preconditions.demo_authenticated
            assert preconditions.demo_verified
            assert preconditions.market_data_ready
            assert preconditions.all_met, preconditions.unmet()

            # The real balance was read, not invented.
            assert math.isclose(orchestrator._equity, 10_000.0)   # noqa: SLF001
            assert "balance" in mock.calls

            # Instrument discovery ran and found the X-Perp — shorts native.
            capabilities = orchestrator.discovery.capabilities
            assert capabilities.primary.inst_type is InstType.SWAP
            assert capabilities.supports_short_on_exchange
            assert orchestrator._inst_id == capabilities.primary.inst_id  # noqa: SLF001

            # Backfill populated the live series.
            for timeframe in ("5", "15", "60", "240"):
                assert orchestrator.store.bars_available(timeframe) > 200

            assert len(orchestrator.registry) == 4
        finally:
            await orchestrator.shutdown()

    async def test_bootstrap_without_credentials_leaves_demo_unverified(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(loaded_config, None)
        try:
            preconditions = await orchestrator.bootstrap()
            assert not preconditions.demo_verified
            assert not preconditions.all_met
            assert "demo environment verification" in preconditions.unmet()
            assert orchestrator.guard.orders_permitted() is False
        finally:
            await orchestrator.shutdown()

    async def test_actual_balance_is_used_even_when_it_differs(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        """The brief: never fake the value; use the real demo balance."""
        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument, equity=7_432.19)
        orchestrator = Orchestrator(
            loaded_config, _credentials()
        )
        try:
            await orchestrator.bootstrap()
            assert math.isclose(orchestrator._equity, 7_432.19)   # noqa: SLF001
            assert loaded_config.config.experiment.expected_demo_equity == 10_000.0
        finally:
            await orchestrator.shutdown()

    async def test_strategies_are_registered_in_the_database(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(
            loaded_config, _credentials()
        )
        try:
            await orchestrator.bootstrap()
            stored = orchestrator.repos.strategies.all_strategies()
            assert len(stored) == 4
            for row in stored:
                assert row["hypothesis"]
                assert row["exit_mechanisms"]
            versions = orchestrator.repos.strategies.version_map()
            assert len(versions) == 4
        finally:
            await orchestrator.shutdown()


class TestDryRunDoesNotStartTheTimer:
    async def test_dry_run_never_creates_an_experiment(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        """The single most important dry-run guarantee."""
        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(
            loaded_config,
            _credentials(),
            dry_run=True,
        )
        # Skip the websocket layer; this test is about the timer, not streaming.
        monkeypatch.setattr(orchestrator, "_start_streams", _noop)
        monkeypatch.setattr(orchestrator, "_bar_worker", _noop)

        try:
            await orchestrator.bootstrap()
            orchestrator.experiment = _experiment_manager(orchestrator, loaded_config)
            # _run_dry_run() shuts the engine down when it finishes, so the
            # database is inspected afterwards through a fresh connection —
            # which also proves nothing was persisted.
            await orchestrator._run_dry_run(duration_seconds=0)   # noqa: SLF001
            assert orchestrator.experiment.state is None
        finally:
            await orchestrator.shutdown()

        from btcbot.database.db import Database
        from btcbot.database.repositories import Repositories

        with Database(loaded_config.config.database.path) as reopened:
            assert Repositories(reopened).experiments.find_active() is None, (
                "dry run started the 14-day timer"
            )

    async def test_dry_run_observes_market_data_and_regime(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(
            loaded_config,
            _credentials(),
            dry_run=True,
        )
        monkeypatch.setattr(orchestrator, "_start_streams", _noop)
        monkeypatch.setattr(orchestrator, "_bar_worker", _noop)

        try:
            await orchestrator.bootstrap()
            orchestrator.experiment = _experiment_manager(orchestrator, loaded_config)
            await orchestrator._run_dry_run(duration_seconds=6)   # noqa: SLF001

            assert orchestrator.store.ticker is not None
            assert orchestrator.store.last_price > 0
            assert orchestrator._current_regime is not None       # noqa: SLF001
            assert 0.0 <= orchestrator._current_regime.confidence <= 1.0
        finally:
            await orchestrator.shutdown()

    async def test_dashboard_state_is_available_during_dry_run(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        import json

        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(
            loaded_config,
            _credentials(),
            dry_run=True,
        )
        try:
            await orchestrator.bootstrap()
            state = orchestrator.dashboard_state()
            assert state["system"]["dry_run"] is True
            assert state["experiment"] is None
            assert state["market"]["last_price"] > 0
            # The instrument panel reflects the discovered contract spec.
            assert state["instrument"]["inst_id"] == orchestrator._inst_id  # noqa: SLF001
            assert state["instrument"]["margin_mode"] == "isolated"
            assert state["demo_account"]["risk_state"]["state"]
            json.dumps(state, default=str)
        finally:
            await orchestrator.shutdown()


class TestResearchModeGating:
    async def test_research_refuses_to_start_unverified(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        """No credentials ⇒ no experiment, and a clear refusal."""
        from btcbot.app.orchestrator import Orchestrator
        from btcbot.utils.errors import DemoVerificationError

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(loaded_config, None)
        try:
            with pytest.raises(DemoVerificationError) as exc:
                await orchestrator.start()
            assert "preconditions not met" in str(exc.value)
            assert orchestrator.repos.experiments.find_active() is None
        finally:
            await orchestrator.shutdown()

    async def test_shutdown_is_idempotent(self, monkeypatch, loaded_config, perp_instrument):
        from btcbot.app.orchestrator import Orchestrator

        mock = _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(
            loaded_config, _credentials()
        )
        await orchestrator.bootstrap()
        orchestrator._running = True     # noqa: SLF001
        await orchestrator.shutdown()
        await orchestrator.shutdown()    # must not raise
        assert mock.closed


async def _noop(*args, **kwargs) -> None:
    return None


def _experiment_manager(orchestrator, loaded_config):
    from btcbot.app.experiment import ExperimentManager

    return ExperimentManager(
        orchestrator.repos.experiments,
        orchestrator.repos.system,
        loaded_config.config,
        config_hash=loaded_config.config_hash,
    )
